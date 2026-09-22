"""Read-only customer XLS/XLSX intake. No OCR, formulas, ERP calls or guessed IDs.

The caller must keep the original upload and require an explicit mapping review
before handing the normalised rows to the existing order workflow.
"""
import hashlib
import io
import math
import posixpath
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree as ET
from pathlib import Path
from html.parser import HTMLParser

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
FIELDS = {
    'name': ('商品名称', ['商品名称','商品名','物品名称','物品名','品名','材料名称','名称','产品名称']),
    'quantity': ('数量', ['数量','需求数量','采购数量','申请数量','订购数量','订货数量','销售数量']),
    'spec': ('规格型号', ['规格型号','规格/型号','规格','型号','产品规格']),
    'unit': ('单位', ['计量单位','单位','销售单位','采购单位']),
    'sourcePrice': ('参考单价', ['单价','参考价格','参考单价','京东售价','医院单价','含税单价']),
    'sourceAmount': ('参考金额', ['金额','总金额','合计金额','价税合计']),
    'sourceCode': ('来源商品编码', ['系统编码','商品编码','用友商品编码','内部编码','编码','编号']),
    'deliveryLocation': ('送货地点', ['送货地点','下单科室','需求科室','收货地址','送货地址']),
    'sourceDate': ('原需求日期', ['下单日期','申请日期','需求日期','订货日期']),
    'memo': ('需求备注', ['备注','需求备注','说明']),
}


def normal(value):
    return re.sub(r'[\s*＊:：]', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def column_index(value):
    n = 0
    for char in value: n = n * 26 + ord(char) - 64
    return n


def column_name(n):
    result = ''
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def customer_excel_format(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 10_000_000:
        raise ValueError('请上传不超过10MB的xls或xlsx文件')
    if raw.startswith(b'PK\x03\x04'): return 'xlsx'
    if raw.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'): return 'xls'
    try:
        prefix = html_text(raw)[:8192].lstrip()
        if prefix.startswith('<') and re.search(r'<table\b', prefix, re.I): return 'html_xls'
    except ValueError: pass
    raise ValueError('需要Excel文件或含table的HTML导出表格，请用Excel另存后导入')


def html_text(raw):
    encodings = ['utf-16'] if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig','gb18030']
    for encoding in encodings:
        try: return raw.decode(encoding)
        except UnicodeError: continue
    raise ValueError('无法识别HTML表格字符编码，请另存为xlsx')


class HtmlTables(HTMLParser):
    """Extract cell text only. HTML is never rendered and URLs are never opened."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.current, self.row, self.parts = [], None, None, None
        self.colspan, self.ignored, self.text_size = 1, [], 0

    def finish_cell(self):
        if self.parts is None: return
        value = ''.join(self.parts).strip()
        if len(value) > 4096: raise ValueError('单元格文字超过4096字')
        self.row.extend([value]+['']*(self.colspan-1))
        if len(self.row)>128: raise ValueError('HTML表格超过128列')
        self.parts = None

    def finish_row(self):
        self.finish_cell()
        if self.row is not None:
            self.current.append(self.row)
            if len(self.current)>10000: raise ValueError('HTML表格超过10000行')
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','iframe','object','template','noscript'):
            self.ignored.append(tag); return
        if self.ignored: return
        if tag=='table':
            if self.current is not None: raise ValueError('暂不支持嵌套HTML表格，请另存为xlsx')
            self.current=[]
        elif tag=='tr' and self.current is not None:
            self.finish_row();self.row=[]
        elif tag in ('td','th') and self.current is not None:
            if self.row is None: self.row=[]
            self.finish_cell()
            attributes=dict(attrs)
            if attributes.get('rowspan','1')!='1': raise ValueError('HTML表格包含跨行合并单元格，请取消合并或另存为xlsx')
            try: self.colspan=int(attributes.get('colspan','1'))
            except (ValueError,TypeError): raise ValueError('HTML表格合并列数无效') from None
            if not 1<=self.colspan<=128: raise ValueError('HTML表格合并列数超限')
            self.parts=[]
        elif tag=='br' and self.parts is not None: self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.ignored:
            if tag==self.ignored[-1]: self.ignored.pop()
            return
        if tag in ('td','th'): self.finish_cell()
        elif tag=='tr' and self.current is not None: self.finish_row()
        elif tag=='table' and self.current is not None:
            self.finish_row();self.tables.append(self.current);self.current=None
            if len(self.tables)>100: raise ValueError('HTML表格数量超过100个')

    def handle_data(self, value):
        if not self.ignored and self.parts is not None:
            self.text_size += len(value)
            if self.text_size>4_000_000: raise ValueError('HTML表格文字内容超限')
            self.parts.append(value)


class HtmlReader:
    def __init__(self, raw):
        parser=HtmlTables()
        parser.feed(html_text(raw));parser.close()
        if parser.current is not None: raise ValueError('HTML表格缺少结束标签，请重新导出')
        self.tables=parser.tables
        if not self.tables: raise ValueError('文件中没有可读取的HTML表格')
        self.sheets=[ET.Element('sheet',name=f'表格{i+1}') for i in range(len(self.tables))]

    def table(self, chosen):
        rows=self.tables[self.sheets.index(chosen)]
        return [(r+1,{column_name(c+1):v for c,v in enumerate(row)}, {})
            for r,row in enumerate(rows) if any(row)]


def xls_library():
    reader_path = str(Path(__file__).resolve().parent / 'local-state' / 'reader-deps')
    if reader_path not in sys.path: sys.path.insert(0, reader_path)
    try:
        import xlrd
        return xlrd
    except ImportError as exc:
        raise ValueError('本机xls读取组件xlrd尚未安装，请联系维护人员') from exc


class XlsReader:
    """xlrd reads BIFF values only; it never opens Excel or executes VBA/formulas."""
    def __init__(self, raw):
        self.lib = xls_library()
        try:
            self.book = self.lib.open_workbook(file_contents=raw, on_demand=True,
                formatting_info=True, logfile=io.StringIO())
        except Exception as exc:
            raise ValueError('无法读取此xls；文件可能损坏、加密或并非Excel工作簿') from exc
        names = self.book.sheet_names()
        if not 1 <= len(names) <= 100: raise ValueError('工作表数量超出允许范围')
        self.sheets = [ET.Element('sheet', name=name) for name in names]

    def table(self, chosen):
        try:
            sheet = self.book.sheet_by_name(chosen.get('name'))
        except Exception as exc:
            raise ValueError('无法读取xls工作表；请检查文件是否损坏或加密') from exc
        if sheet.nrows > 10000: raise ValueError('工作表超过10000行，请按订单拆分')
        if sheet.visibility: chosen.set('state', 'hidden')
        result = []
        for r in range(sheet.nrows):
            values, notes = {}, {}
            for c in range(sheet.ncols):
                cell = sheet.cell(r,c)
                value, warnings = '', []
                if cell.ctype in (self.lib.XL_CELL_EMPTY, self.lib.XL_CELL_BLANK): continue
                if c >= 128: raise ValueError('有效内容超过128列，请精简客户表格')
                if cell.ctype == self.lib.XL_CELL_TEXT: value = cell.value
                elif cell.ctype == self.lib.XL_CELL_BOOLEAN: value = 'TRUE' if cell.value else 'FALSE'
                elif cell.ctype == self.lib.XL_CELL_ERROR:
                    warnings.append('原单元格为Excel错误，请核对并补填')
                elif cell.ctype == self.lib.XL_CELL_DATE:
                    try:
                        if self.book.datemode == 0 and int(cell.value) == 60: raise ValueError()
                        value = self.lib.xldate_as_datetime(cell.value, self.book.datemode).isoformat(sep=' ').removesuffix(' 00:00:00')
                    except (ValueError, OverflowError): warnings.append('日期格式异常，请核对并补填')
                elif cell.ctype == self.lib.XL_CELL_NUMBER:
                    if not math.isfinite(cell.value): warnings.append('数字格式异常，请核对并补填')
                    else:
                        value = str(int(cell.value)) if cell.value.is_integer() else str(cell.value)
                        fmt = self.book.format_map[self.book.xf_list[sheet.cell_xf_index(r,c)].format_key].format_str
                        if cell.value.is_integer() and re.fullmatch('0+', fmt): value = value.zfill(min(len(fmt),4096))
                if len(value) > 4096: raise ValueError('单元格文字超过4096字，请精简客户表格')
                if value or warnings:
                    col = column_name(c+1)
                    values[col], notes[col] = value.strip(), warnings
            if values: result.append((r+1,values,notes))
        return result


class WorkbookReader:
    def __init__(self, raw):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 10_000_000:
            raise ValueError('请上传不超过10MB的xlsx文件')
        self.archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = self.archive.infolist()
        names = [x.filename for x in entries]
        if len(entries) > 2000 or sum(x.file_size for x in entries) > 40_000_000:
            raise ValueError('Excel解压大小超限')
        if len(set(names)) != len(names): raise ValueError('Excel包含重复文件条目')
        if any(x.startswith('/') or '\\' in x or '..' in x.split('/') for x in names):
            raise ValueError('Excel内部路径无效')
        if any('vbaproject' in x.casefold() for x in names): raise ValueError('不支持含宏的工作簿，请另存为xlsx')
        book = self.xml('xl/workbook.xml')
        self.sheets = book.findall('m:sheets/m:sheet', NS)
        if not 1 <= len(self.sheets) <= 100: raise ValueError('工作表数量超出允许范围')
        self.rels = {r.get('Id'): r for r in self.xml('xl/_rels/workbook.xml.rels')}
        props = book.find('m:workbookPr', NS)
        self.epoch1904 = props is not None and props.get('date1904') in ('1','true')
        self.strings = []
        if 'xl/sharedStrings.xml' in names:
            self.strings = [''.join(t.text or '' for t in si.findall('.//m:t', NS))
                for si in self.xml('xl/sharedStrings.xml').findall('m:si', NS)]
        self.styles, self.formats = [], {}
        if 'xl/styles.xml' in names:
            styles = self.xml('xl/styles.xml')
            self.formats = {int(x.get('numFmtId')): x.get('formatCode','') for x in styles.findall('m:numFmts/m:numFmt', NS)}
            self.styles = [int(x.get('numFmtId','0')) for x in styles.findall('m:cellXfs/m:xf', NS)]

    def xml(self, path):
        raw = self.archive.read(path)
        if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw.replace(b'\x00', b''), re.I):
            raise ValueError('不支持包含XML实体的工作簿')
        return ET.fromstring(raw)

    def cell(self, cell):
        kind, notes = cell.get('t'), []
        cached = cell.find('m:v', NS)
        value = cached.text or '' if cached is not None else ''
        formula = cell.find('m:f', NS) is not None
        if kind == 's': value = self.strings[int(value)]
        elif kind == 'inlineStr': value = ''.join(x.text or '' for x in cell.findall('m:is//m:t', NS))
        elif kind == 'b': value = 'TRUE' if value == '1' else 'FALSE'
        elif kind == 'e':
            notes.append('原单元格为Excel错误：' + value)
            value = ''
        if formula:
            if not value: notes.append('公式没有缓存值，请在Excel中计算后保存或手工补填')
            else: notes.append('仅读取公式缓存值，未重新计算，请核对')
        if len(value) > 4096: raise ValueError('单元格文字超过4096字，请精简客户表格')
        style = int(cell.get('s','0'))
        fmt_id = self.styles[style] if style < len(self.styles) else 0
        fmt = self.formats.get(fmt_id, '')
        numeric = kind in (None, 'n') and value != ''
        if numeric:
            try:
                numeric_value = Decimal(value)
                if not numeric_value.is_finite() or not -30 <= numeric_value.adjusted() <= 30 or numeric_value.as_tuple().exponent < -30:
                    return value.strip(), notes + ['数字超出安全解析范围，请核对原单元格']
            except InvalidOperation:
                return value.strip(), notes + ['数字单元格格式无效，请核对']
        if numeric and re.fullmatch(r'0+', fmt):
            number = Decimal(value)
            if number == number.to_integral_value(): value = str(int(number)).zfill(len(fmt))
        # Return calendar dates, never an unlabelled Excel serial date.
        if numeric and (fmt_id in (14,15,16,17,22) or (re.search(r'[yd]', fmt, re.I) and not '[' in fmt)):
            try:
                serial = Decimal(value)
                if not 0 <= serial <= 2958465: raise ValueError()
                if not self.epoch1904 and int(serial) == 60:
                    raise ValueError('Excel1900闰年日期无效')
                epoch = datetime(1904,1,1) if self.epoch1904 else datetime(1899,12,31) if serial < 60 else datetime(1899,12,30)
                value = (epoch + timedelta(days=float(serial))).isoformat(sep=' ')
                value = value.removesuffix(' 00:00:00')
            except (ValueError, OverflowError, InvalidOperation): notes.append('日期格式异常，请核对')
        return value.strip(), notes

    def table(self, chosen):
        rel = self.rels[chosen.get(RID)]
        if rel.get('TargetMode') == 'External': raise ValueError('不支持外部工作表')
        target = rel.get('Target','')
        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        if not path.startswith('xl/') or '..' in path or '\\' in path: raise ValueError('工作表路径无效')
        result = []
        seen = set()
        for row in self.xml(path).findall('m:sheetData/m:row', NS):
            row_no = int(row.get('r','0'))
            if not 1 <= row_no <= 10000: raise ValueError('工作表超过10000行，请按订单拆分')
            if row_no in seen: raise ValueError('工作表行号重复')
            seen.add(row_no)
            values, notes = {}, {}
            for cell in row.findall('m:c', NS):
                address = cell.get('r','')
                match = re.fullmatch(r'([A-Z]+)([0-9]+)', address)
                if not match or int(match[2]) != row_no: raise ValueError('单元格地址无效')
                col = match[1]
                value, warnings = self.cell(cell)
                if not value and not warnings: continue
                if column_index(col) > 128: raise ValueError('有效内容超过128列，请精简客户表格')
                if col in values: raise ValueError('工作表单元格重复')
                values[col], notes[col] = value, warnings
            if values: result.append((row_no, values, notes))
        return sorted(result, key=lambda item: item[0])


def inspect_customer_excel(raw, sheet=None, header_row=None, mapping=None):
    try:
        file_format = customer_excel_format(raw)
        reader = HtmlReader(raw) if file_format == 'html_xls' else XlsReader(raw) if file_format == 'xls' else WorkbookReader(raw)
        names = [s.get('name') for s in reader.sheets]
        out = {'sheetNames': names, 'sheet': None, 'headerRow': None, 'columns': [], 'mapping': {},
            'rows': [], 'warnings': [], 'rowWarnings': [], 'errors': [], 'ready': False,
            'fields': [{'key': k, 'label': v[0], 'required': k in ('name','quantity')} for k,v in FIELDS.items()],
            'fileHash': hashlib.sha256(raw).hexdigest(), 'rowCount': 0, 'format': file_format}
        if file_format == 'xls':
            out['warnings'].append('xls仅读取已保存的单元格值，不执行宏或重算公式；请确认原表已计算并保存，缺值需补填。')
        if file_format == 'html_xls':
            out['warnings'].append('此xls实际为HTML表格导出，仅读取单元格文本；不执行脚本、不访问外链，合并列不重复填值。')
        if sheet is None and len(names) > 1:
            return {**out, 'warnings': out['warnings'] + ['请选择要导入的工作表；不会自动合并多个工作表。']}
        selected = sheet if sheet is not None else names[0]
        if not isinstance(selected, str) or selected not in names: raise ValueError('所选工作表不存在')
        chosen = next(x for x in reader.sheets if x.get('name') == selected)
        table = reader.table(chosen)
        out['sheet'] = selected
        if chosen.get('state') in ('hidden','veryHidden'): out['warnings'].append('所选工作表在原文件中隐藏，请核对来源。')
        aliases = {k: {normal(v) for v in desc[1]} for k,desc in FIELDS.items()}
        def guesses(cells):
            return {key: [col for col,value in cells.items() if normal(value) in options] for key, options in aliases.items()}
        if header_row is not None and (type(header_row) is not int or not 1 <= header_row <= 10000):
            raise ValueError('表头行必须为1至10000的整数')
        if header_row is None:
            possible = [(r,c,n) for r,c,n in table[:50] if guesses(c)['name'] and guesses(c)['quantity']]
            if possible: header_row = possible[0][0]
        out['preview'] = [{'row': r, 'cells': c} for r,c,_ in table[:10]]
        if header_row is None:
            return {**out, 'errors': ['未识别到名称和数量表头，请指定表头行并选择列对应关系。']}
        header = next((c for r,c,_ in table if r == header_row), None)
        if header is None: raise ValueError('指定表头行没有内容')
        out['headerRow'] = header_row
        used = set(header)
        for r,c,_ in table:
            if r > header_row: used.update(c)
        out['columns'] = [{'key': col, 'label': header.get(col) or '（空表头）'} for col in sorted(used, key=column_index)]
        guesses_ = guesses(header)
        if mapping is None:
            mapping = {key: columns[0] if len(columns) == 1 else '' for key,columns in guesses_.items()}
            for key,columns in guesses_.items():
                if len(columns) > 1: out['warnings'].append(f'{FIELDS[key][0]}有多个可能列，请手动选择。')
        if not isinstance(mapping, dict) or set(mapping) - set(FIELDS): raise ValueError('列映射包含不支持的字段')
        mapping = {key: mapping.get(key) or '' for key in FIELDS}
        if any(not isinstance(col,str) or (col and col not in used) for col in mapping.values()): raise ValueError('映射列不存在')
        selected_cols = [c for c in mapping.values() if c]
        if len(set(selected_cols)) != len(selected_cols): raise ValueError('同一列不能映射到多个字段')
        out['mapping'] = mapping
        for key in ('name','quantity'):
            if not mapping[key]: out['errors'].append(f'请选择{FIELDS[key][0]}对应的列')
        if out['errors']: return out
        skipped = []
        for row_no, cells, notes in table:
            if row_no <= header_row: continue
            record = {key: cells.get(col,'') for key,col in mapping.items()}
            if not any(record.values()) and not any(notes.get(c) for c in selected_cols): continue
            if normal(record['name']) in ('合计','总计','小计','本页合计'):
                skipped.append(row_no); continue
            if normal(record['name']) in aliases['name'] and normal(record['quantity']) in aliases['quantity']:
                skipped.append(row_no); continue
            warnings = []
            if not record['name'] and not record['quantity']:
                out['errors'].append(f'第{row_no}行名称与数量均空但存在其他内容，请检查是否为页脚或补齐原表，避免遗漏明细。')
            for key,col in mapping.items():
                warnings.extend(f'{FIELDS[key][0]}：{n}' for n in notes.get(col,[]))
            if not record['name']: warnings.append('缺少商品名称，需补填')
            if not record['unit']: warnings.append('缺少单位，可在商品匹配后补齐并核对数量')
            if not record['spec']: warnings.append('缺少规格型号，商品匹配时需核对')
            numeric_values = {}
            for key,label in [('quantity','数量'),('sourcePrice','参考单价'),('sourceAmount','参考金额')]:
                value = record[key]
                if not value:
                    if key == 'quantity': warnings.append('缺少数量，需补填')
                    continue
                cleaned = unicodedata.normalize('NFKC',value).strip()
                # Only well-formed thousands separators are removed.
                if re.fullmatch(r'[+-]?\d{1,3}(,\d{3})+(\.\d+)?', cleaned): cleaned = cleaned.replace(',','')
                try:
                    number = Decimal(cleaned)
                    if not number.is_finite() or number < 0 or (key == 'quantity' and number == 0) or number > 1_000_000_000 or number.as_tuple().exponent < -30:
                        raise InvalidOperation()
                    record[key] = format(number, 'f')
                    numeric_values[key] = number
                except InvalidOperation: warnings.append(f'{label}不是有效的'+('正数' if key == 'quantity' else '非负数')+'，保留原文待修改')
            if len(numeric_values) == 3:
                expected = (numeric_values['quantity'] * numeric_values['sourcePrice']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                actual = numeric_values['sourceAmount'].quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                if expected != actual: warnings.append(f'原表数量×参考单价={expected}，与参考金额{actual}不符，未修改原数值')
            record.update(sourceRow=str(row_no), sourceSheet=selected)
            out['rows'].append(record)
            if warnings: out['rowWarnings'].append({'row': row_no, 'warnings': warnings})
        if skipped: out['warnings'].append('已排除合计/重复表头行：' + '、'.join(map(str,skipped)))
        out['warnings'].append('来源编码不作为用友内部ID；来源单价和金额仅作参考，未确认本次成交价。')
        out['rowCount'] = len(out['rows'])
        if not out['rows']: out['errors'].append('表头之后未找到商品明细')
        if len(out['rows']) > 200:
            out['errors'].append(f'共有{len(out["rows"])}条明细，单次最多200条，请按客户或订单拆分；未导入任何订单。')
            out['rows'] = out['rows'][:200]
        out['ready'] = not out['errors']
        return out
    except (zipfile.BadZipFile, KeyError, IndexError, ET.ParseError, TypeError, OverflowError, RuntimeError) as exc:
        raise ValueError('无法读取此Excel，请检查文件是否损坏、加密或格式不符') from exc

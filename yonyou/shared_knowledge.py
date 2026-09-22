"""Validated, immutable shared catalogue uploads; no macros, formulas or ERP writes."""
import base64
import hashlib
import json
import re
import threading
import time
import uuid
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from customer_excel import customer_excel_format, XlsReader, WorkbookReader, HtmlReader, normal, column_index
from local_catalog import INDEX, LABEL, norm, search_catalog

FIELDS = {
    'code': ('商品编码', ['系统编码','商品编码','用友商品编码','编码']),
    'name': ('商品名称', ['材料名称','商品名称','产品名称','名称']),
    'spec': ('规格型号', ['规格型号','规格','型号']),
    'unit': ('计量单位', ['计量单位','单位']),
    'price': ('默认参考价', ['贵昌（95折）','贵昌95折','默认参考价','含税单价','单价','参考单价']),
    'discount': ('折扣', ['折扣','折扣率','折扣系数','优惠折扣']),
}

CLEAN_ALIASES = {
    'code': ['产品编码','物料编码','物品编码','存货编码','商品编号','产品编号','货号','编号','内部编码'],
    'name': ['商品名','品名','物品名称','物料名称','存货名称','货品名称'],
    'spec': ['规格/型号','产品规格','商品规格','包装规格'],
    'unit': ['销售单位','采购单位','基本单位','商品单位'],
    'price': ['参考价','参考价格','报价','含税价格','含税售价','销售单价','贵昌价','贵昌参考价',
              '原价','折前价','折前单价','目录价','折后价','折后单价','成交价','成交单价','实付价','优惠后价格'],
    'discount': [],
}


def _aliases(key, auto):
    return {normal(v) for v in FIELDS[key][1] + (CLEAN_ALIASES[key] if auto else [])}


def _header(table, start, depth):
    levels = [values for n, values, _ in table if start <= n < start + depth]
    columns = sorted({col for values in levels for col in values}, key=column_index)
    return {col: ' / '.join(dict.fromkeys(str(values.get(col, '')).strip() for values in levels if str(values.get(col, '')).strip())) for col in columns}


def _mapping(headers, auto):
    result = {}
    for key in FIELDS:
        aliases = _aliases(key, auto)
        matches = [col for col, value in headers.items() if normal(value) in aliases or
                   (auto and any(normal(part) in aliases for part in value.split(' / ')))]
        result[key] = matches[0] if matches and (len(matches) == 1 or not auto) else ''
    # A composite header that identifies two business fields needs manual choice.
    duplicate = {col for col in result.values() if col and list(result.values()).count(col) > 1}
    return {key: '' if col in duplicate else col for key, col in result.items()}


def _clean_value(key, value):
    value = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip()
    if key == 'code' and re.fullmatch(r'\[[A-Za-z0-9_-]{1,80}\]', value): value = value[1:-1]
    if key == 'price':
        value = re.sub(r'^\s*[¥￥]\s*', '', value)
        value = re.sub(r'\s*元\s*$', '', value).strip()
        # Do not turn ambiguous comma decimals or malformed grouping into a price.
        if re.fullmatch(r'[+-]?\d{1,3}(,\d{3})+(\.\d+)?', value): value = value.replace(',', '')
    return value


def _looks_like_data(values, mapping):
    code = _clean_value('code', str(values.get(mapping.get('code'), '')))
    name = _clean_value('name', str(values.get(mapping.get('name'), '')))
    unit = _clean_value('unit', str(values.get(mapping.get('unit'), '')))
    price = _clean_value('price', str(values.get(mapping.get('price'), '')))
    numeric_price = bool(re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', price))
    product_code = bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code)) and normal(code) not in _aliases('code', True)
    named_product = name and normal(name) not in _aliases('name', True)
    return numeric_price or (named_product and unit and normal(unit) not in _aliases('unit', True)) or (product_code and (any(c.isdigit() for c in code) or named_product))


def _has_header_word(values):
    return any(normal(value) in _aliases(key, True) for value in values.values() for key in FIELDS)


def _discount(value):
    value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))
    if value in ('不打折', '无折扣'): return Decimal(1)
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(折|%)?', value)
    if not match: raise InvalidOperation()
    number, suffix = Decimal(match[1]), match[2]
    if suffix == '%': number /= 100
    elif suffix == '折': number /= 10 if number <= 10 else 100
    if not 0 < number <= 1: raise InvalidOperation()
    return number


def _price_mode(mode, mapping, headers):
    if mode != 'auto': return mode
    price_header = normal(headers.get(mapping.get('price'), ''))
    if any(word in price_header for word in ('折后', '成交', '实付', '优惠后')): return 'as_is'
    discount_present = bool(mapping.get('discount')) or any(
        any(normal(part) in _aliases('discount', True) for part in value.split(' / ')) for value in headers.values())
    if not discount_present: return 'as_is'
    if any(word in price_header for word in ('原价', '折前', '目录价')): return 'table_discount'
    return 'ambiguous'


def _changes(data, row_ids):
    edits, excluded, included = data.get('edits', {}), data.get('excludedRows', []), data.get('includedRows', [])
    if not isinstance(edits, dict) or len(edits) > 10000: raise ValueError('人工修正数据无效')
    if any(not isinstance(rows, list) or len(rows) > 10000 for rows in (excluded, included)):
        raise ValueError('排除或恢复行数据无效')
    if len(json.dumps([edits, excluded, included], ensure_ascii=False).encode('utf-8')) > 1_500_000:
        raise ValueError('人工整理内容过多，请分表处理')
    for rows in (excluded, included):
        if any(type(n) is not int or n not in row_ids for n in rows) or len(rows) != len(set(rows)):
            raise ValueError('排除或恢复行号无效')
    if set(excluded) & set(included): raise ValueError('同一行不能同时排除和恢复')
    for number, change in edits.items():
        if not isinstance(number, str) or not re.fullmatch(r'[1-9][0-9]{0,4}', number) or int(number) not in row_ids:
            raise ValueError('人工修正行号无效')
        if not isinstance(change, dict) or set(change) - FIELDS.keys(): raise ValueError('人工修正字段无效')
        if any(not isinstance(v, str) or len(v) > 4096 for v in change.values()): raise ValueError('人工修正内容过长或格式无效')
    return edits, set(excluded), set(included)


def _inspect_sheet(data, table, selected, sheets, raw, name):
    auto = data.get('autoClean', False)
    if type(auto) is not bool: raise ValueError('自动整理设置无效')
    header_row = data.get('headerRow')
    depth = data.get('headerDepth')
    if depth is not None and (type(depth) is not int or not 1 <= depth <= 3): raise ValueError('表头层数需为1至3')
    if header_row is not None and (type(header_row) is not int or not 1 <= header_row <= 10000): raise ValueError('表头行无效')
    if auto and (header_row is None or depth is None):
        choices, reliable_starts = [], []
        for n, _, _ in table[:50]:
            if header_row is not None and n != header_row: continue
            if header_row is None and not _has_header_word(_header(table, n, 1)): continue
            for layers in ([depth] if depth is not None else (1, 2, 3)):
                guessed = _mapping(_header(table, n, layers), True)
                if header_row is None and any(_looks_like_data(values, guessed)
                                               for row_number, values, _ in table if row_number < n):
                    continue
                if any(_looks_like_data(values, guessed)
                       for row_number, values, _ in table if n <= row_number < n + layers):
                    continue
                if any(not _has_header_word(values)
                       for row_number, values, _ in table if n < row_number < n + layers):
                    continue
                score = sum(bool(v) for v in guessed.values()) * 10 + (20 if guessed['name'] else 0) - layers
                choices.append((score, -n, -layers))
                if guessed['name'] and sum(bool(guessed[k]) for k in ('code','name','unit','price')) >= 2:
                    reliable_starts.append(n)
        if choices:
            if header_row is None and reliable_starts:
                first_reliable = min(reliable_starts)
                choices = [choice for choice in choices if -choice[1] == first_reliable]
            _, start, layers = max(choices)
            header_row, depth = -start, -layers
    if header_row is None:
        header_row = next((n for n, values, _ in table[:25] if any(normal(v) in [normal(a) for a in FIELDS['name'][1]] for v in values.values())), 1)
    if type(header_row) is not int or not 1 <= header_row <= 10000: raise ValueError('表头行无效')
    depth = depth or 1
    headers = _header(table, header_row, depth)
    mapping = data.get('mapping')
    if mapping is None:
        mapping = _mapping(headers, auto)
    if not isinstance(mapping, dict) or set(mapping) - FIELDS.keys(): raise ValueError('字段映射无效')
    used = [v for v in mapping.values() if v]
    if any(not isinstance(v, str) or v not in headers for v in used) or len(used) != len(set(used)):
        raise ValueError('请选择不同且有效的字段列')
    mode = data.get('priceMode', 'as_is')
    if mode not in ('auto', 'as_is', 'discount95', 'table_discount'): raise ValueError('价格规则无效')
    resolved_mode = _price_mode(mode, mapping, headers)
    candidates = [(n, values, notes) for n, values, notes in table if n >= header_row + depth]
    if len(candidates) > 10000: raise ValueError('知识库明细超过10000行')
    edits, excluded, included = _changes(data, {n for n, _, _ in candidates})
    errors = [f'请选择{FIELDS[k][0]}列' for k in ('code', 'name', 'unit', 'price') if not mapping.get(k)]
    if resolved_mode == 'ambiguous': errors.append('表中同时有价格和折扣，尚不明确价格是折前还是折后，请选择价格口径')
    if resolved_mode == 'table_discount' and not mapping.get('discount'): errors.append('请选择唯一的折扣列')
    groups, warnings, codes, review = {}, [], defaultdict(set), []
    for n, values, notes in candidates:
        original = {k: str(values.get(mapping.get(k), '')) for k in FIELDS}
        row = {k: _clean_value(k, v) if auto else v.strip() for k, v in original.items()}
        row.update({k: _clean_value(k, v) if auto else v.strip() for k, v in edits.get(str(n), {}).items()})
        if not any(row.values()) and not any(original.values()) and not any(values.values()): continue
        row_errors, row_warnings = [], []
        if auto and not any(original.values()) and any(values.values()):
            row_warnings.append('原行内容位于未映射列，请检查列对应关系或手动补填/排除')
        # Original contents determine automatic exclusions, so edits cannot silently
        # hide a product. Users explicitly restore such rows before publishing.
        source = {k: _clean_value(k, v) if auto else v.strip() for k, v in original.items()}
        repeated = auto and normal(source['name']) in _aliases('name', True) and sum(
            bool(source[k]) and normal(source[k]) in _aliases(k, True) for k in FIELDS) >= 2
        subtotal = auto and source['name'] in ('合计','小计','总计') and not source['code']
        auto_skipped = bool(repeated or subtotal)
        is_excluded = n in excluded or (auto_skipped and n not in included)
        skip_reason = '重复表头' if repeated else '合计或小计行' if subtotal else ''
        entry = {'rowNumber': n, 'original': original, 'values': row, 'issues': row_errors,
                 'warnings': row_warnings, 'excluded': is_excluded, 'autoSkipped': auto_skipped,
                 'skipReason': '手动排除' if n in excluded else skip_reason, 'normalizedPrice': ''}
        review.append(entry)
        if is_excluded: continue
        if not row['name']:
            if 'autoClean' not in data and not edits: continue  # Preserve legacy callers only.
            row_errors.append('商品名称缺失，请补填或排除此行')
        if any(notes.get(mapping.get(k)) for k in ('code', 'name', 'unit', 'price')):
            row_warnings.append('有公式缓存或单元格异常，请核对原表')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', row['code']) or not row['unit']:
            row_errors.append('商品编码或单位缺失/无效')
        if any(len(row[k]) > 300 for k in ('name','spec','unit')): row_errors.append('商品信息过长')
        price = ''
        discount = Decimal(1)
        if resolved_mode == 'table_discount':
            try: discount = _discount(row['discount'])
            except InvalidOperation:
                discount = None
                row_errors.append('折扣为空或无效，请填写9折、95%、0.95或1等明确折扣')
        elif row['discount']:
            try: _discount(row['discount'])
            except InvalidOperation: row_warnings.append('本次不使用折扣列，该行折扣格式无法识别，已保留原值')
        if row['price']:
            try:
                if auto and not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', row['price']): raise InvalidOperation()
                value = Decimal(row['price'] if auto else row['price'].replace(',', '').replace('￥', '').replace('¥', ''))
                if not value.is_finite() or not 0 < value <= 1_000_000_000: raise InvalidOperation()
                if resolved_mode == 'discount95': value *= Decimal('.95')
                elif resolved_mode == 'table_discount' and discount is not None: value *= discount
                value = value.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)
                if value <= 0: raise InvalidOperation()
                if discount is not None and resolved_mode != 'ambiguous': price = format(value, 'f')
            except InvalidOperation: row_errors.append('参考价不是有效的正数')
        else: row_warnings.append('参考价为空，录单时需补填')
        entry['normalizedPrice'] = price
        entry['discountFactor'] = format(discount, 'f') if resolved_mode == 'table_discount' and discount is not None else '.95' if resolved_mode == 'discount95' else ''
        errors.extend(f'第{n}行{message}' for message in row_errors)
        warnings.extend(f'第{n}行{message}' for message in row_warnings)
        key = tuple(row[k] for k in ('code','name','spec','unit'))
        item = groups.setdefault(key, {k: row[k] for k in ('code','name','spec','unit')} | {'sourceRows':[], 'sourcePrices':[], 'referencePrices':{'hospital':[], 'guichang95':[]}})
        item['sourceRows'].append(n)
        item.setdefault('sourceLocations', []).append({'sheet': selected, 'row': n, 'column': mapping.get('price','')})
        item['sourcePrices'].append({'sheet':selected,'column':mapping.get('price',''),'row':n,'hospital':'','guichang95':price})
        if price and price not in item['referencePrices']['guichang95']: item['referencePrices']['guichang95'].append(price)
        codes[(norm(row['name']),norm(row['spec']))].add(row['code'])
    items = list(groups.values())
    if not items: errors.append('未找到商品明细')
    for item in items:
        item['recordKey'] = hashlib.sha256(json.dumps([item[k] for k in ('code','name','spec','unit')],ensure_ascii=False).encode()).hexdigest()[:24]
        item['conflictingCodes'] = sorted(codes[(norm(item['name']),norm(item['spec']))])
    source_hash = hashlib.sha256(raw).hexdigest()
    index = {'schemaVersion':1,'sourceFile':Path(name).name,'sourceSheet':selected,'sourceSha256':source_hash,'priceColumn':mapping.get('price',''),
             'priceMode':mode, 'resolvedPriceMode':resolved_mode, 'priceLabel': '最终参考价', 'items':items}
    audit = {'autoClean': auto, 'headerRow': header_row, 'headerDepth': depth, 'mapping': mapping,
             'resolvedPriceMode': resolved_mode, 'edits': edits, 'excludedRows': sorted(excluded), 'includedRows': sorted(included)}
    digest = hashlib.sha256(json.dumps([source_hash, selected, header_row, mapping, mode, audit],sort_keys=True).encode()).hexdigest()
    result = {'sheetNames':sheets,'sheet':selected,'headerRow':header_row,'columns':[{'column':c,'label':v} for c,v in sorted(headers.items(),key=lambda x:column_index(x[0]))],
              'mapping':mapping,'priceMode':mode,'resolvedPriceMode':resolved_mode,'digest':digest,'recordCount':len(items),'errors':errors[:50], 'errorCount':len(errors),
              'warnings':warnings[:20], 'preview':items[:10], 'ready':not errors,'fields':[{'key':k,'label':v[0]} for k,v in FIELDS.items()]}
    result.update({'autoClean': auto, 'headerDepth': depth, 'reviewRows': review,
                   'cleanupSummary': {'totalRows': len(review), 'changedRows': sum(r['original'] != r['values'] for r in review),
                                      'excludedRows': sum(r['excluded'] for r in review), 'issueRows': sum(bool(r['issues']) for r in review)}})
    index['cleanupAudit'] = audit
    return index, result


def _invalid_sheet_result(selected, sheets, data, message):
    """Keep a malformed sheet addressable in the editor without publishing it."""
    return {'sheetNames': sheets, 'sheet': selected, 'headerRow': data.get('headerRow') or 1,
            'headerDepth': data.get('headerDepth') or 1, 'autoClean': data.get('autoClean', False),
            'columns': [], 'mapping': {}, 'priceMode': data.get('priceMode', 'as_is'),
            'resolvedPriceMode': 'ambiguous', 'digest': '', 'recordCount': 0,
            'errors': [str(message)], 'errorCount': 1, 'warnings': [], 'preview': [], 'ready': False,
            'fields': [{'key': k, 'label': v[0]} for k, v in FIELDS.items()], 'reviewRows': [],
            'cleanupSummary': {'totalRows': 0, 'changedRows': 0, 'excludedRows': 0, 'issueRows': 0}}


def _combine_sheets(raw, name, names, indices, results):
    groups, codes = {}, defaultdict(set)
    for index in indices:
        for item in index['items']:
            key = tuple(item[k] for k in ('code', 'name', 'spec', 'unit'))
            if key not in groups:
                groups[key] = {k: item[k] for k in ('code', 'name', 'spec', 'unit', 'recordKey')}
                groups[key].update(sourceRows=[], sourcePrices=[], sourceLocations=[],
                                   referencePrices={'hospital': [], 'guichang95': []})
            merged = groups[key]
            for field in ('sourceRows', 'sourcePrices', 'sourceLocations'):
                merged[field].extend(item.get(field, []))
            for field in ('hospital', 'guichang95'):
                for price in item['referencePrices'][field]:
                    if price not in merged['referencePrices'][field]: merged['referencePrices'][field].append(price)
            codes[(norm(item['name']), norm(item['spec']))].add(item['code'])
    items = list(groups.values())
    for item in items:
        item['conflictingCodes'] = sorted(codes[(norm(item['name']), norm(item['spec']))])
    source_hash = hashlib.sha256(raw).hexdigest()
    digest = hashlib.sha256(json.dumps([source_hash, [[r['sheet'], r['digest']] for r in results]],
                                      ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    audit = {'selectedSheets': names, 'sheets': [{'sheet': i['sourceSheet'], **i['cleanupAudit']} for i in indices]}
    index = {'schemaVersion': 1, 'sourceFile': Path(name).name, 'sourceSheet': '、'.join(names),
             'sourceSheets': names, 'sourceSha256': source_hash, 'priceColumn': '', 'priceMode': 'per_sheet',
             'resolvedPriceMode': 'per_sheet', 'priceLabel': '最终参考价', 'items': items, 'cleanupAudit': audit}
    errors = [f'工作表“{r["sheet"]}”：{message}' for r in results for message in r['errors']]
    warnings = [f'工作表“{r["sheet"]}”：{message}' for r in results for message in r['warnings']]
    result = {**results[0], 'selectedSheets': names, 'sheetResults': results, 'digest': digest,
              'ready': all(r['ready'] for r in results), 'recordCount': len(items), 'preview': items[:10],
              'errors': errors[:50], 'errorCount': sum(r['errorCount'] for r in results), 'warnings': warnings[:20],
              'cleanupSummary': {key: sum(r['cleanupSummary'][key] for r in results)
                                 for key in ('totalRows', 'changedRows', 'excludedRows', 'issueRows')}}
    return index, result


def inspect(data):
    name, encoded = data.get('filename'), data.get('file')
    if not isinstance(name, str) or not 1 <= len(name) <= 200 or Path(name).suffix.lower() not in ('.xls', '.xlsx'):
        raise ValueError('请选择xls或xlsx知识库文件')
    if not isinstance(encoded, str) or len(encoded) > 13_400_000: raise ValueError('知识库最大10MB')
    raw = base64.b64decode(encoded, validate=True)
    kind = customer_excel_format(raw)
    if (kind == 'xlsx') != name.lower().endswith('.xlsx'): raise ValueError('扩展名与真实格式不一致')
    multi = 'sheets' in data
    settings = data.get('sheets')
    if multi:
        allowed = {'sheet', 'autoClean', 'headerRow', 'headerDepth', 'mapping', 'priceMode',
                   'edits', 'excludedRows', 'includedRows'}
        if not isinstance(settings, list) or not 1 <= len(settings) <= 20:
            raise ValueError('请至少选择1个、最多20个工作表')
        if any(not isinstance(s, dict) or set(s) - allowed or not isinstance(s.get('sheet'), str)
               or not s['sheet'] for s in settings): raise ValueError('工作表选择或整理设置无效')
        if len({s['sheet'] for s in settings}) != len(settings): raise ValueError('不能重复选择同一工作表')
        if len(json.dumps(settings, ensure_ascii=False).encode('utf-8')) > 1_500_000:
            raise ValueError('全部工作表的人工整理内容过多，请减少选择的工作表')
    reader = XlsReader(raw) if kind == 'xls' else HtmlReader(raw) if kind == 'html_xls' else WorkbookReader(raw)
    try:
        sheets = [s.get('name') for s in reader.sheets]
        if not sheets or len(sheets) != len(set(sheets)): raise ValueError('工作表名称为空或重复')
        if not multi:
            settings = [dict(data, sheet=data.get('sheet') or sheets[0])]
        if any(s['sheet'] not in sheets for s in settings): raise ValueError('工作表不存在')
        # Workbook order makes selection order immaterial to price tie-breaking and retries.
        settings = sorted(settings, key=lambda s: sheets.index(s['sheet']))
        indices, results, total_rows, text_size = [], [], 0, 0
        for setting in settings:
            selected = setting['sheet']
            sheet = next(s for s in reader.sheets if s.get('name') == selected)
            table = reader.table(sheet)
            total_rows += len(table)
            text_size += sum(len(str(value)) for _, values, _ in table for value in values.values())
            if total_rows > 10000: raise ValueError('所选工作表合计超过10000行，请减少选择的工作表')
            if text_size > 4_000_000: raise ValueError('所选工作表文字内容超限，请减少选择的工作表')
            try:
                index, result = _inspect_sheet(setting, table, selected, sheets, raw, name)
                indices.append(index)
            except ValueError as exc:
                if not multi: raise
                result = _invalid_sheet_result(selected, sheets, setting, exc)
            results.append(result)
        if not multi: return raw, indices[0], results[0]
        index, result = _combine_sheets(raw, name, [s['sheet'] for s in settings], indices, results)
        return raw, index, result
    finally:
        if hasattr(reader, 'archive'): reader.archive.close()
        if hasattr(reader, 'book'): reader.book.release_resources()


class SharedKnowledge:
    def __init__(self, root, department='gov'):
        if department not in ('gov', 'food'):
            raise ValueError('知识库部门必须为gov或food')
        self.root, self.department, self.lock = Path(root).resolve(), department, threading.RLock()

    def _record_allowed(self, record):
        """A legacy metadata file without department belongs to gov only."""
        department = record.get('department')
        return (self.department == 'gov' and department in (None, 'gov')) or department == self.department

    def resolve(self, identity=None):
        if self.department == 'food' and identity in (None, ''):
            raise ValueError('请选择食材知识库')
        if self.department == 'gov' and identity in (None, ''):
            identity = 'default-catalog'
        if identity == 'default-catalog':
            if self.department != 'gov': raise ValueError('所选知识库不存在或未开放，请重新选择知识库')
            return {'id':identity,'name':LABEL,'uploadedBy':'系统','shared':True,'department':'gov'}
        if not isinstance(identity, str) or not re.fullmatch(r'kb-[a-f0-9]{32}', identity): raise ValueError('知识库不存在')
        try:
            record = json.loads((self.root/identity/'metadata.json').read_text(encoding='utf-8'))
        except (OSError, ValueError): raise ValueError('知识库不存在或未完成上传') from None
        if not isinstance(record, dict) or record.get('id') != identity or not self._record_allowed(record):
            raise ValueError('知识库不存在或未开放')
        return record

    def _published_records(self):
        """Read complete metadata files without changing any published version."""
        records = []
        for path in sorted(self.root.glob('kb-*/metadata.json')):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(record, dict) and record.get('id') == path.parent.name and self._record_allowed(record):
                    records.append(record)
            except (OSError, ValueError):
                continue
        return records

    def catalogues(self):
        records = self._published_records()
        children = {}
        for record in records:
            replaced = record.get('replacesId')
            if isinstance(replaced, str):
                children.setdefault(replaced, []).append(record)
        active, retired = [], []
        for record in records:
            replacements = children.get(record.get('id'), [])
            if replacements:
                # A valid publish can only have one child.  Sorting keeps the
                # response deterministic even if an old disk state is repaired
                # manually.
                replacement = sorted(replacements, key=lambda item: (item.get('version', 0), item.get('createdAt', 0), item.get('id', '')))[-1]
                retired.append({**record, 'status': 'retired', 'replacedBy': replacement['id']})
            else:
                active.append(record)
        builtin = [self.resolve()] if self.department == 'gov' else []
        return {'knowledgeBases': builtin + active, 'retiredKnowledgeBases': retired,
                'defaultId':'default-catalog' if self.department == 'gov' else ''}

    def publish(self, data, user):
        if data.get('confirmation') not in ('确认发布为全员共享知识库', '确认发布为本部门共享知识库'):
            raise ValueError('请确认发布共享知识库')
        title = data.get('name')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80: raise ValueError('知识库名称需为1至80字')
        raw, index, preview = inspect(data)
        if not preview['ready'] or data.get('digest') != preview['digest']: raise ValueError('文件或映射已变化，请重新预览并确认')
        with self.lock:
            records = self._published_records()
            replace_id = data.get('replaceId')
            expected_version = data.get('expectedVersion')
            replacement_target = None
            if replace_id is not None or expected_version is not None:
                if not isinstance(replace_id, str) or not re.fullmatch(r'kb-[a-f0-9]{32}', replace_id):
                    raise ValueError('替换知识库必须指定有效的replaceId')
                if isinstance(expected_version, bool) or not isinstance(expected_version, (int, str)) or not expected_version:
                    raise ValueError('替换知识库必须指定有效的expectedVersion')
                replacement_target = next((r for r in records if r.get('id') == replace_id), None)
                if replacement_target is None:
                    raise ValueError('要替换的知识库不存在')
                if replacement_target.get('name') != title.strip():
                    raise ValueError('replaceId与知识库名称不一致')
                actual_version = replacement_target.get('version', 1)
                # The digest is the preferred optimistic-concurrency token;
                # integer versions remain accepted for older clients.
                version_matches = (expected_version == replacement_target.get('digest') or
                                   (isinstance(expected_version, int) and expected_version >= 1 and
                                    actual_version == expected_version))
                if not version_matches:
                    raise ValueError('知识库版本已变化，请刷新后重试')
                existing_replacement = next((r for r in records if r.get('replacesId') == replace_id and
                                             r.get('digest') == preview['digest'] and
                                             r.get('name') == title.strip()), None)
                if existing_replacement:
                    return existing_replacement
                if any(r.get('replacesId') == replace_id for r in records):
                    raise ValueError('要替换的知识库已被替换，请刷新后选择当前版本')
            elif 'replaceId' in data or 'expectedVersion' in data:
                raise ValueError('replaceId和expectedVersion必须同时提供')
            # A double click/retry must not create a second published version.
            if replace_id is None:
                replaced_ids = {r.get('replacesId') for r in records if r.get('replacesId')}
                previous = next((r for r in records if r.get('id') not in replaced_ids and
                                 r.get('digest') == preview['digest'] and r.get('name') == title.strip()), None)
            else:
                previous = next((r for r in records if r.get('digest') == preview['digest'] and
                                 r.get('name') == title.strip() and r.get('replacesId') == replace_id), None)
            if previous: return previous
            if len(records) >= 100: raise ValueError('共享知识库已达100个版本，请联系管理员整理')
            identity = 'kb-' + uuid.uuid4().hex
            folder = self.root / identity
            staging = self.root / ('.pending-' + identity)
            staging.mkdir(parents=True)
            metadata = {'id':identity,'name':title.strip(),'uploadedBy':user['display_name'],'uploaderId':user['id'],
                        'createdAt':time.time(),'shared':True,'recordCount':preview['recordCount'],'digest':preview['digest'],
                        'filename':Path(data['filename']).name,'sheet':index['sourceSheet'],'priceMode':index['priceMode'], 'resolvedPriceMode':index['resolvedPriceMode'],
                        'department': self.department}
            metadata['version'] = (replacement_target.get('version', 1) + 1) if replacement_target is not None else 1
            if replace_id is not None:
                metadata['replacesId'] = replace_id
            if 'selectedSheets' in preview: metadata['selectedSheets'] = preview['selectedSheets']
            audit = {'sourceSha256': index['sourceSha256'],
                'sheet': index['sourceSheet'], 'priceMode': index['priceMode'], 'digest': preview['digest'],
                **index['cleanupAudit'], 'summary': preview['cleanupSummary'],
                'rows': preview['reviewRows']}
            if 'sheetResults' in preview:
                # Equal row numbers on distinct sheets must never share audit identity.
                audit.pop('rows')
                audit['sheets'] = [{'sheet': r['sheet'], 'priceMode': r['priceMode'],
                    **i, 'summary': r['cleanupSummary'], 'rows': r['reviewRows']}
                    for r, i in zip(preview['sheetResults'], index['cleanupAudit']['sheets'])]
            try:
                (staging/'original').write_bytes(raw)
                (staging/'index.json').write_text(json.dumps(index,ensure_ascii=False),encoding='utf-8')
                (staging/'cleanup-audit.json').write_text(json.dumps(audit, ensure_ascii=False), encoding='utf-8')
                (staging/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
                staging.replace(folder)
            finally:
                if staging.exists():
                    for filename in ('original', 'index.json', 'cleanup-audit.json', 'metadata.json'):
                        (staging/filename).unlink(missing_ok=True)
                    staging.rmdir()
            return metadata

    def search(self, identity, query, row, page):
        base = self.resolve(identity)
        path = INDEX if base['id'] == 'default-catalog' else self.root/base['id']/'index.json'
        result = search_catalog(query, row, page, index_path=path, source_label=None if base['id']=='default-catalog' else base['name'])
        return {**result,'knowledgeBaseId':base['id'],'candidates':[{**r,'knowledgeBaseId':base['id']} for r in result['candidates']]}

"""Local reusable order headers. Never imports credentials, approval, goods or prices."""
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from order_header_contract import LOCAL_ONLY_HEADER_FIELDS, local_field_warnings
import knowledge_bases

_FIELD_ROWS = [
 ('knowledgeBaseId','商品知识库','knowledgeBase'),
 ('customerName','客户名称','customer'),('agentId','客户 ID','customer'),
 ('invoiceAgentId','开票客户 ID','customer'),('receiveAddress','收货地址','customer'),
 ('salesOrgName','销售组织','organization'),('salesOrgId','销售组织 ID','organization'),
 ('settlementOrgId','结算组织 ID','organization'),('stockOrgId','库存组织 ID','organization'),
 ('departmentName','销售部门','organization'),('saleDepartmentId','部门 ID','organization'),
 ('salespersonName','销售业务员','organization'),('corpContact','业务员 ID','organization'),
 ('transactionName','交易类型','transaction'),('transactionTypeId','交易类型 ID','transaction'),
 ('currencyName','币种名称','currency'),('currencyId','币种 ID','currency'),
 ('natCurrencyId','本币 ID','currency'),('exchangeRateType','汇率类型 ID','currency'),
 ('memo','订单备注','memo'),
 ('invoiceCustomerName','开票客户名称','customer'),
 ('settlementOrgName','结算组织','organization'),('stockOrgName','库存组织','organization'),
 ('exchangeRateTypeName','汇率类型','currency'),('natCurrencyName','本币名称','currency'),
]
_FIELD_ROWS += [(key, label, 'localOnly') for key, label in LOCAL_ONLY_HEADER_FIELDS.items() if key != 'plannedShipDate']
ID_FIELDS = {key for key, label, _ in _FIELD_ROWS if label.endswith(' ID')}
FIELDS = [{'key': key, 'label': label, 'group': group,
           'description': '由用友档案选择自动带出；兼容旧模板文本ID，不要求操作员手填'
             if key in ID_FIELDS else ('可留空，每笔订单仍需人工核实' if key != 'memo' else '通用备注，可留空')}
          for key, label, group in _FIELD_ROWS]
LABELS = {f['key']: f['label'] for f in FIELDS}
ALIASES = {alias: f['key'] for f in FIELDS for alias in (f['key'], f['label'], f['label'].replace(' ', ''))}
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
MAX_SIZE = 1_000_000


def normalize(header):
    if not isinstance(header, dict) or len(header) > 50: raise ValueError('默认信息必须是字段对象')
    clean = {}
    seen = set()
    for raw_key, value in header.items():
        key = ALIASES.get(raw_key)
        if key is None: raise ValueError('存在不支持的字段；只能导入订单基础信息，不允许日期、复核、商品、价格或凭证')
        if key in seen: raise ValueError('同一字段重复出现：' + LABELS[key])
        seen.add(key)
        if value is None or value == '': continue
        if not isinstance(value, str):
            raise ValueError(LABELS[key] + '必须是文本；数字类型ID可能已经失真，请从用友重新复制为文本')
        value = value.strip()
        if not value: continue
        if len(value) > (2000 if key == 'memo' else 300): raise ValueError(LABELS[key] + '内容过长')
        if any(ord(char) < 32 and char not in '\n\r\t' for char in value): raise ValueError('字段含不可用字符')
        if key in ID_FIELDS:
            if key == 'exchangeRateType':
                valid = bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value))
            else: valid = bool(re.fullmatch(r'[0-9]{1,20}', value))
            if not valid: raise ValueError(LABELS[key] + '格式无效，请填写真实档案ID文本（不支持科学计数法）')
        clean[key] = value
    if clean.get('knowledgeBaseId'):
        knowledge_bases.resolve(clean['knowledgeBaseId'])
    if not clean: raise ValueError('未填写任何默认信息，请在模板的“值”列填写后导入')
    if clean.get('currencyId') and clean.get('natCurrencyId') and clean['currencyId'] != clean['natCurrencyId']:
        raise ValueError('当前录单流程仅支持币种ID与本币ID相同、汇率1')
    return clean


def warnings(header):
    result = []
    for name, identity in [('customerName','agentId'),('salesOrgName','salesOrgId'),
        ('departmentName','saleDepartmentId'),('salespersonName','corpContact'),
        ('transactionName','transactionTypeId'),('currencyName','currencyId'),
        ('invoiceCustomerName','invoiceAgentId'),('settlementOrgName','settlementOrgId'),
        ('stockOrgName','stockOrgId'),('exchangeRateTypeName','exchangeRateType'),('natCurrencyName','natCurrencyId')]:
        if bool(header.get(name)) != bool(header.get(identity)):
            result.append(LABELS[name] + '尚未完成档案关联，请按名称查询并选择')
    missing = [LABELS[key] for key in ID_FIELDS if not header.get(key)]
    if missing: result.append('未配置的档案字段可在每单补填：' + '、'.join(sorted(missing)))
    result.append('配置导入不代表用友档案已验证；每单仍需复核，不自动保存或提交')
    result.extend(local_field_warnings(header))
    return result


def _json_unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out: raise ValueError('JSON含重复字段')
        out[key] = value
    return out


def read_defaults_xlsx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(e.file_size for e in entries) > 10_000_000:
                raise ValueError('模板解压大小超限')
            if len({e.filename for e in entries}) != len(entries): raise ValueError('模板包含重复文件项')
            def xml(path):
                data = archive.read(path)
                if b'<!DOCTYPE' in data or b'<!ENTITY' in data: raise ValueError('不支持XML实体')
                return ET.fromstring(data)
            rels = {r.get('Id'): r for r in xml('xl/_rels/workbook.xml.rels')}
            sheets = xml('xl/workbook.xml').findall('m:sheets/m:sheet', NS)
            if len(sheets) != 1: raise ValueError('每个默认配置文件只允许一个工作表')
            rid = sheets[0].get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            rel = rels[rid]
            if rel.get('TargetMode') == 'External': raise ValueError('不支持外部工作表')
            target = rel.get('Target', '')
            path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            if '..' in path or '\\' in path: raise ValueError('模板路径无效')
            shared = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                shared = [''.join(node.itertext()) for node in xml('xl/sharedStrings.xml').findall('m:si', NS)]
            rows = []
            for row in xml(path).findall('m:sheetData/m:row', NS):
                cells = {}
                for cell in row.findall('m:c', NS):
                    if cell.find('m:f', NS) is not None: raise ValueError('默认配置不接受公式，请粘贴为文本值')
                    col = re.sub(r'\d+', '', cell.get('r', ''))
                    typ = cell.get('t', 'n')
                    value = cell.find('m:v', NS)
                    value = value.text if value is not None else ''
                    if typ == 's': value = shared[int(value)]
                    elif typ == 'inlineStr': value = ''.join(cell.find('m:is', NS).itertext())
                    if value: cells[col] = (str(value), typ)
                if cells: rows.append(cells)
            if not rows or rows[0].get('A', ('',))[0].strip() != '字段' or rows[0].get('B', ('',))[0].strip() != '值':
                raise ValueError('请使用模板：第一行A列“字段”、B列“值”，C列可为“说明”')
            if len(rows) > 51: raise ValueError('配置字段行数超限')
            out = {}
            seen = set()
            for row in rows[1:]:
                field = row.get('A', ('',))[0].strip()
                value, typ = row.get('B', ('', 'inlineStr'))
                if not field and not value: continue
                key = ALIASES.get(field)
                if not key: raise ValueError('模板含不支持的字段，请下载最新模板填写')
                if key in seen: raise ValueError('模板含重复字段：' + LABELS[key])
                seen.add(key)
                if not value.strip(): continue
                if typ not in ('inlineStr', 's', 'str'):
                    raise ValueError(LABELS[key] + '必须使用文本单元格；数字ID可能已失真，请重新复制为文本')
                out[key] = value
            return normalize(out)
    except (zipfile.BadZipFile, KeyError, IndexError, TypeError, ET.ParseError, AttributeError):
        raise ValueError('无法读取此Excel模板') from None


class DefaultStore:
    FIELDS = FIELDS
    knowledge_bases = staticmethod(knowledge_bases.catalogues)

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()

    def preview(self, raw, filename):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SIZE: raise ValueError('配置文件不能为空且最大1MB')
        suffix = Path(str(filename)).suffix.lower()
        if suffix == '.xlsx': header = read_defaults_xlsx(raw)
        elif suffix == '.json':
            try: data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_json_unique)
            except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError('JSON文件编码或格式无效') from None
            if isinstance(data, dict) and set(data) == {'header'}: data = data['header']
            header = normalize(data)
        else: raise ValueError('只支持.xlsx或.json默认信息文件')
        return {'header': header, 'fields': [{'key': k, 'label': LABELS[k], 'value': v} for k, v in header.items()],
                'warnings': warnings(header), 'fieldCount': len(header)}

    def list(self):
        with self.lock:
            profiles = []
            for path in sorted(self.root.glob('*.json')):
                if re.fullmatch(r'[0-9a-f]{32}\.json', path.name):
                    try: profiles.append(self.get(path.stem))
                    except (ValueError, OSError): continue
            return {'profiles': sorted(profiles, key=lambda p: p['createdAt'], reverse=True)}

    def get(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity): raise ValueError('默认配置ID无效')
        try: record = json.loads((self.root / (identity + '.json')).read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError): raise ValueError('默认配置不存在或损坏') from None
        if not isinstance(record, dict) or not isinstance(record.get('name'), str) or not isinstance(record.get('createdAt'), (int, float)):
            raise ValueError('默认配置记录损坏')
        header = normalize(record.get('header'))
        return {'id': identity, 'name': record['name'], 'header': header, 'createdAt': record['createdAt']}

    def save(self, name, header):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('请填写1至80字的配置名称')
        name = name.strip()
        if any(ord(c) < 32 for c in name): raise ValueError('配置名称含不可用字符')
        header = normalize(header)
        with self.lock:
            profiles = self.list()['profiles']
            if any(p['name'].casefold() == name.casefold() for p in profiles): raise ValueError('已有同名配置，请使用新名称，旧配置不会被覆盖')
            if len(profiles) >= 100: raise ValueError('本机最多保存100套默认配置')
            self.root.mkdir(parents=True, exist_ok=True)
            identity = secrets.token_hex(16)
            record = {'id': identity, 'name': name, 'header': header, 'createdAt': time.time()}
            tmp = self.root / (identity + '.tmp')
            with tmp.open('x', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, allow_nan=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.root / (identity + '.json'))
            return record

"""Local XLSX -> YonSuite adapter. Saving and explicit submission are separate."""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from api_throttle import ThrottleBusy, retry_after_seconds, shared_throttle

BASE = 'https://c2.yonyoucloud.com'
PATHS = {
    'invoiceSave': '/yonbip/sd/vouchersaleinvoice/singleSave',
    'invoiceDetail': '/yonbip/sd/vouchersaleinvoice/detail',
    'invoiceList': '/yonbip/sd/vouchersaleinvoice/list',
    'customers': '/yonbip/digitalModel/merchant/newlist',
    'organizations': '/yonbip/digitalModel/orgunit/querytree',
    'departments': '/yonbip/digitalModel/admindept/tree',
    'staff': '/yonbip/digitalModel/staff/list',
    'currencies': '/yonbip/digitalModel/currencytenant/batchQueryDetail',
    'exchangeTypes': '/yonbip/digitalModel/exchangeratetype/batchQueryDetail',
    'transactions': '/yonbip/digitalModel/transtype/queryByBillTypeCodes',
    'taxRates': '/yonbip/digitalModel/taxrate/findByTaxRate',
    'stock': '/yonbip/scm/stock/QueryCurrentStocksByCondition',
    'products': '/yonbip/digitalModel/product/listproductbycondition',
    'price': '/yonbip/sd/pricing/voucher/enquiryPrice',
    'list': '/yonbip/sd/voucherorder/list',
    'detail': '/yonbip/sd/voucherorder/detail',
    'save': '/yonbip/sd/voucherorder/singleSave',
    'submit': '/yonbip/sd/voucherorder/batchsubmit',
}
HEADER_FIELDS = ['salesOrgId', 'transactionTypeId', 'agentId', 'settlementOrgId',
                 'invoiceAgentId', 'currencyId', 'exchangeRateType', 'natCurrencyId',
                 'stockOrgId', 'vouchdate']
ALIASES = {'name': ['名称', '商品名', '商品名称'], 'quantity': ['数量', '销售数量'],
           'spec': ['型号', '规格', '规格型号'], 'unit': ['单位', '销售单位'],
           'sourcePrice': ['京东售价', '单价', '参考价格'], 'sourceAmount': ['金额'],
           'sourceCode': ['编码', '商品编码'], 'deliveryLocation': ['下单科室', '送货地点']}
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def wire(value):
    """Emit exact decimal JSON numbers; IDs are strings, never IEEE-754 floats."""
    if isinstance(value, Decimal):
        if not value.is_finite(): raise ValueError('非有限数值')
        return format(value, 'f')
    if isinstance(value, dict): return '{' + ','.join(json.dumps(k) + ':' + wire(v) for k, v in value.items()) + '}'
    if isinstance(value, list): return '[' + ','.join(wire(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def browser_safe(value):
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool): return str(value)
    if isinstance(value, dict): return {k: browser_safe(v) for k, v in value.items()}
    if isinstance(value, list): return [browser_safe(v) for v in value]
    return value


def number(value, label, zero=False):
    try: d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError): raise ValueError(label + '需填写有效数字')
    if not d.is_finite() or d < 0 or (not zero and d == 0) or d > Decimal('1000000000'):
        raise ValueError(label + '超出 Demo 允许范围')
    if d.as_tuple().exponent < -8: raise ValueError(label + '最多8位小数')
    return d


def read_xlsx(raw):
    if len(raw) > 10_000_000: raise ValueError('Excel 最大10MB')
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        if len(z.infolist()) > 2000 or sum(i.file_size for i in z.infolist()) > 40_000_000:
            raise ValueError('Excel 解压大小超限')
        def xml(path):
            b = z.read(path)
            if b'<!DOCTYPE' in b or b'<!ENTITY' in b: raise ValueError('不支持 XML 实体')
            return ET.fromstring(b)
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(si.itertext()) for si in xml('xl/sharedStrings.xml').findall('m:si', NS)]
        rels = {r.attrib['Id']: r.attrib['Target'] for r in xml('xl/_rels/workbook.xml.rels')}
        sheets = xml('xl/workbook.xml').findall('m:sheets/m:sheet', NS)
        # Read the business export once, never double import its audit/detail sheet.
        chosen = next((s for s in sheets if s.get('name') == '业务录单'), None)
        if chosen is None:
            if len(sheets) != 1: raise ValueError('多工作表文件需包含“业务录单”页，避免重复导入核对明细')
            chosen = sheets[0]
        rid = chosen.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        target = rels[rid]
        path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        if '..' in path or '\\' in path: raise ValueError('不支持的工作表路径')
        table = []
        for row in xml(path).findall('m:sheetData/m:row', NS):
            cells = {}
            for c in row.findall('m:c', NS):
                addr = c.get('r', '')
                col = re.sub(r'\d', '', addr)
                v = c.find('m:v', NS)
                value = v.text if v is not None else ''
                if c.get('t') == 's': value = strings[int(value)]
                elif c.get('t') == 'inlineStr': value = ''.join(c.find('m:is', NS).itertext())
                if c.find('m:f', NS) is not None: value = '__FORMULA__'
                cells[col] = str(value or '').strip()
            if any(cells.values()): table.append((row.get('r'), cells))
        columns = None
        records = []
        for rowno, cells in table:
            if columns is None:
                candidate = {k: next((col for col, val in cells.items() if val in aliases), None) for k, aliases in ALIASES.items()}
                if candidate['name'] and candidate['quantity']: columns = candidate
                continue
            if cells.get(columns['name']) in ('合计', '总计', '小计', '本页合计'): continue
            record = {k: cells.get(col, '') for k, col in columns.items()}
            if not record['name'] and not record['quantity']: continue
            if '__FORMULA__' in record.values(): raise ValueError(f'第{rowno}行含公式，请先另存为值后导入')
            record['sourceRow'] = rowno
            records.append(record)
        if columns is None or not records: raise ValueError('找不到商品名称和数量，请选择已导出的业务表')
        if len(records) > 200: raise ValueError('Demo 每次最多200行，请按客户/订单拆分')
        return {'sheet': chosen.get('name'), 'rows': records, 'fileHash': hashlib.sha256(raw).hexdigest()}
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, TypeError) as e:
        raise ValueError('无法读取此 XLSX 文件') from e


def build_plan(rows, header, mode='live', order_identity=None):
    errors = []
    if not rows or len(rows) > 200: errors.append('需要1至200行商品')
    for key in HEADER_FIELDS:
        if not str(header.get(key, '')).strip(): errors.append('待填写：' + key)
    for name, ref, label in [('salespersonName', 'corpContact', '销售业务员'), ('departmentName', 'saleDepartmentId', '销售部门')]:
        if str(header.get(name, '')).strip() and not str(header.get(ref, '')).strip():
            errors.append(label + '尚未匹配用友档案，请核实名称')
    for key in ('vouchdate', 'hopeReceiveDate'):
        if header.get(key):
            try: datetime.strptime(header[key], '%Y-%m-%d')
            except (ValueError, TypeError): errors.append(key + '应为YYYY-MM-DD')
    if header.get('currencyId') != header.get('natCurrencyId'): errors.append('本 Demo 仅支持原币等于本币、汇率1的订单')
    if header.get('reviewed') is not True: errors.append('请人工确认同一客户、商品规格、单位、价格与组织配置')
    # Importer deliberately does not infer customer/department/address from ambiguous source text.
    details, total = [], Decimal(0)
    for i, row in enumerate(rows, 1):
        try:
            for key in ('name', 'productId', 'unitId', 'taxId'):
                if not str(row.get(key, '')).strip(): raise ValueError('待填写：' + key)
            if row.get('sameUnitConfirmed') is not True: raise ValueError('需确认销售/主/计价单位相同（本Demo不处理多单位换算）')
            if 'unitConversion' in row:
                # Recheck operator evidence at preview/save, after any row edits.
                # Local import avoids the resolver's core.safe_message cycle.
                from order_resolver import manual_unit_conversion
                manual_unit_conversion(row)
            if mode == 'live' and any(str(row.get(k, '')).startswith('DEMO-') for k in ('productId', 'unitId', 'taxId')):
                raise ValueError('模拟编码不可用于真实写入')
            qty = number(row.get('quantity'), '数量')
            price = number(row.get('price'), '含税单价')
            if price.as_tuple().exponent < -6: raise ValueError('含税单价最多6位小数')
            tax_value = row.get('taxRate')
            tax = number('0.13' if tax_value is None or str(tax_value).strip() == '' else tax_value, '税率', zero=True)
            if tax > 1: raise ValueError('税率使用小数，例如0.13')
            amount = (qty * price).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            net = (amount / (1 + tax)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            unit_net = (price / (1 + tax)).quantize(Decimal('.000000'), rounding=ROUND_HALF_UP)
            tax_amount = amount - net
            d = {'_status': 'Insert', 'productId': str(row['productId']), 'masterUnitId': str(row['unitId']),
                 'iProductAuxUnitId': str(row['unitId']), 'iProductUnitId': str(row['unitId']),
                 'subQty': qty, 'qty': qty, 'priceQty': qty, 'invExchRate': Decimal(1), 'invPriceExchRate': Decimal(1),
                 'unitExchangeType': 0, 'unitExchangeTypePrice': 0, 'stockOrgId': str(header.get('stockOrgId', '')),
                 'settlementOrgId': str(header.get('settlementOrgId', '')), 'taxId': str(row['taxId']),
                 'orderProductType': 'SALE', 'oriTaxUnitPrice': price, 'oriSum': amount}
            for k, v in {'oriUnitPrice': unit_net, 'oriMoney': net, 'oriTax': tax_amount,
                         'natUnitPrice': unit_net, 'natTaxUnitPrice': price, 'natMoney': net, 'natSum': amount, 'natTax': tax_amount}.items():
                d['orderDetailPrices!' + k] = v
            if row.get('memo'): d['memo'] = str(row['memo'])
            details.append(d)
            total += amount
        except ValueError as e: errors.append(f'第{i}行 {row.get("name", "")}：{e}')
    for key, value in header.items():
        if mode == 'live' and str(value).startswith('DEMO-'): errors.append('模拟配置不可用于真实写入：' + key)
    data = {k: str(header.get(k, '')).strip() for k in ('salesOrgId', 'transactionTypeId', 'agentId', 'settlementOrgId', 'invoiceAgentId', 'saleDepartmentId', 'corpContact')}
    data.update({'_status': 'Insert', 'vouchdate': str(header.get('vouchdate', '')) + ' 00:00:00',
                 'hopeReceiveDate': str(header.get('hopeReceiveDate', '')) + ' 00:00:00',
                 'orderPrices!currency': str(header.get('currencyId', '')), 'orderPrices!natCurrency': str(header.get('natCurrencyId', '')),
                 'orderPrices!exchangeRateType': str(header.get('exchangeRateType', '')), 'orderPrices!exchRate': Decimal(1),
                 'orderPrices!taxInclusive': True, 'payMoney': total, 'orderDetails': details})
    if header.get('receiveAddress'): data['receiveAddress'] = str(header['receiveAddress'])
    if header.get('memo'): data['memo'] = str(header['memo'])
    for key in ('corpContact', 'saleDepartmentId'):
        if not data.get(key): data.pop(key, None)
    if not header.get('hopeReceiveDate'): data.pop('hopeReceiveDate', None)
    payload = {'data': data}
    if order_identity is not None and (not isinstance(order_identity,str) or not re.fullmatch(r'[a-f0-9]{64}',order_identity)):
        raise ValueError('订单身份无效')
    digest = hashlib.sha256((mode + wire(payload) + (order_identity or '')).encode()).hexdigest()
    # YonSuite gateway error 310024 requires a maximum of 32 characters.
    data['resubmitCheckKey'] = 'gc-demo-' + digest[:24]
    return {'ready': not errors, 'errors': errors, 'payload': payload if not errors else None,
            'total': total, 'rowCount': len(rows), 'digest': digest, 'mode': mode}


def safe_message(value, secrets_to_hide=()):
    """Keep only a bounded diagnostic message, never response bodies or credential URLs."""
    text = str(value or '')[:4000]
    hidden = list(secrets_to_hide) + [os.environ.get('YONYOU_APP_KEY'), os.environ.get('YONYOU_APP_SECRET')]
    for secret in sorted((str(s) for s in hidden if s), key=len, reverse=True):
        for variant in (secret, urllib.parse.quote(secret, safe=''), urllib.parse.quote_plus(secret)):
            text = text.replace(variant, '[REDACTED]')
    text = re.sub(r'https?://\S+', '[URL REDACTED]', text)
    text = re.sub(r'(?i)(appkey|appsecret|access_token|signature|authorization|token)\s*[=:：]\s*[\"\']?[^\s,;\"\'}]+', r'\1=[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9_+/=-]{28,}', '[LONG VALUE REDACTED]', text)
    text = re.sub(r'(?<!\d)\d{11,}(?!\d)', '[ID REDACTED]', text)
    return text[:500]


class YonSuiteError(ValueError):
    def __init__(self, category, message='', http_status=None, api_code=None, hidden=(), retry_after=None, request_dispatched=None):
        self.diagnostic = {'category': category, 'message': safe_message(message, hidden)}
        if http_status is not None: self.diagnostic['httpStatus'] = int(http_status)
        if api_code is not None: self.diagnostic['apiCode'] = safe_message(api_code, hidden)[:60]
        if retry_after is not None: self.diagnostic['retryAfterSeconds'] = max(1, int(retry_after))
        if request_dispatched is not None: self.diagnostic['requestDispatched'] = bool(request_dispatched)
        super().__init__(json.dumps(self.diagnostic, ensure_ascii=False))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('拒绝携带凭证跟随重定向')


class YonSuite:
    def __init__(self, request=None, throttle=None):
        self._request = request or self._http
        # False is an explicit offline-test opt-out. Production clients share one scheduler.
        self._throttle = shared_throttle() if throttle is None else throttle
        self._token = None
        self._expires = 0
        self._lock = threading.Lock()

    @staticmethod
    def configured(): return bool(os.environ.get('YONYOU_APP_KEY') and os.environ.get('YONYOU_APP_SECRET'))

    @staticmethod
    def _http(url, method='GET', body=None):
        req = urllib.request.Request(url, data=wire(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'}, method=method)
        hidden = [v for values in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).values() for v in values]
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=35) as res:
                raw = res.read(10_000_001)
                if len(raw) > 10_000_000: raise YonSuiteError('response_too_large', '响应超过10MB')
                return json.loads(raw, parse_float=Decimal)
        except urllib.error.HTTPError as e:
            try:
                with e:
                    raw = e.read(64_001)
                error = json.loads(raw) if len(raw) <= 64_000 else {}
                if not isinstance(error, dict): error = {}
            except Exception: error = {}
            limited = e.code == 429 or str(error.get('code')) == '310050'
            raise YonSuiteError('rate_limited' if limited else 'http_error',
                               error.get('message') or error.get('msg') or 'HTTP错误，未保留原始响应',
                               e.code, error.get('code'), hidden,
                               retry_after=retry_after_seconds(e.headers.get('Retry-After')) if limited and e.headers else None) from None
        except YonSuiteError: raise
        except json.JSONDecodeError:
            raise YonSuiteError('invalid_json', '返回内容不是有效JSON，未保留原文') from None
        except Exception:
            raise YonSuiteError('transport_error', '连接、超时或响应读取失败；不自动重试') from None

    def _send(self, url, method=None, body=None, hidden=()):
        try:
            if self._throttle:
                self._throttle.acquire()
        except ThrottleBusy as e:
            raise YonSuiteError('rate_limited', str(e) + '，已保留完成结果，请稍后重试未完成项',
                               retry_after=e.retry_after_seconds, request_dispatched=False) from None
        try:
            result = self._request(url) if method is None else self._request(url, method, body)
            if isinstance(result, dict) and str(result.get('code')) == '310050':
                raise YonSuiteError('rate_limited', result.get('message') or result.get('msg'),
                                   api_code='310050', hidden=hidden)
            return result
        except YonSuiteError as e:
            diagnostic = e.diagnostic
            if (diagnostic.get('category') != 'rate_limited' and diagnostic.get('httpStatus') != 429
                    and str(diagnostic.get('apiCode')) != '310050'):
                raise
            seconds = diagnostic.get('retryAfterSeconds')
            seconds = self._throttle.block(seconds) if self._throttle else (seconds or 30)
            raise YonSuiteError('rate_limited', '用友调用频率达到阈值，已暂停后续请求；请稍后重试未完成项',
                               diagnostic.get('httpStatus'), diagnostic.get('apiCode'),
                               hidden=hidden, retry_after=seconds) from None

    def token(self):
        with self._lock:
            if self._token and time.time() < self._expires: return self._token
            if not self.configured(): raise ValueError('未配置本机 YONYOU_APP_KEY / YONYOU_APP_SECRET')
            key, secret = os.environ['YONYOU_APP_KEY'], os.environ['YONYOU_APP_SECRET']
            stamp = str(int(time.time() * 1000))
            message = 'appKey' + key + 'timestamp' + stamp
            sig = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
            query = urllib.parse.urlencode({'appKey': key, 'timestamp': stamp, 'signature': sig})
            result = self._send(BASE + '/iuap-api-auth/open-auth/selfAppAuth/getAccessToken?' + query, hidden=(key, secret))
            if not isinstance(result, dict): raise YonSuiteError('auth_response_invalid', '鉴权响应结构无效')
            if str(result.get('code')) != '00000':
                raise YonSuiteError('auth_rejected', result.get('message'), api_code=result.get('code'), hidden=(key, secret))
            self._token = result['data']['access_token']
            self._expires = time.time() + max(0, int(result['data']['expire']) - 120)
            return self._token

    def call(self, kind, body=None, query=None):
        if kind not in PATHS: raise ValueError('接口不在允许清单')
        qs = urllib.parse.urlencode({'access_token': self.token(), **(query or {})})
        result = self._send(BASE + '/iuap-api-gateway' + PATHS[kind] + '?' + qs,
                            'GET' if kind in ('detail', 'taxRates', 'invoiceDetail') else 'POST', body, hidden=(self._token,))
        if not isinstance(result, dict): raise YonSuiteError('response_invalid', '业务响应结构无效')
        if str(result.get('code')) != '200':
            raise YonSuiteError('business_rejected', result.get('message') or result.get('msg'), api_code=result.get('code'), hidden=(self._token,))
        return result


def demo_input():
    header = {k: 'DEMO-' + k for k in HEADER_FIELDS}
    header.update(currencyId='DEMO-CNY', natCurrencyId='DEMO-CNY', vouchdate='2026-09-09', hopeReceiveDate='2026-09-10', reviewed=False)
    rows = [{'name': '模拟复印纸A4', 'spec': '虚拟测试商品', 'quantity': '4', 'unit': '箱', 'productId': 'DEMO-PAPER',
             'unitId': 'DEMO-BOX', 'taxId': 'DEMO-TAX', 'taxRate': '0.13', 'price': '100',
             'priceSource': '模拟价格，非用友查询', 'sameUnitConfirmed': True}]
    return {'header': header, 'rows': rows, 'mode': 'mock'}

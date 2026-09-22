"""Loopback-only addition to the existing image-to-XLSX program."""
import argparse
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from core import YonSuite, YonSuiteError, safe_message, read_xlsx, build_plan, browser_safe, wire, demo_input, number
from order_resolver import resolve_order, search_references
from save_policy import require_save_allowed, save_status

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'local-state'
PLANS = {}
LOCK = threading.Lock()
CLIENT = YonSuite()
from invoice_workflow import InvoiceService
INVOICES = InvoiceService(CLIENT, STATE / 'invoices')
from order_submission import Submissions
SUBMISSIONS = Submissions(STATE, CLIENT)
VERSION = 'diagnostics-v6-order-references'
SERVICE_INSTANCE = secrets.token_hex(12)
RETRY_ALLOWED_DIGEST = 'eb95059b951cd9d9293899a91806b33f0a65c0046641fcb64609800a267c9546'
RETRY_CONFIRMATION = '已人工确认原订单不存在，仅重试保存一次，不提交不审核'


def write_receipt(marker, receipt):
    temporary = marker.with_name(marker.name + '.' + secrets.token_hex(6) + '.tmp')
    with temporary.open('x', encoding='utf-8') as f:
        json.dump(receipt, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, marker)


def query_order(data, client=CLIENT):
    """Bounded read-only lookup; never accepts an arbitrary upstream API path."""
    if not isinstance(data, dict): raise ValueError('查询条件必须为对象')
    if data.get('date') is not None:
        day = str(data['date'])
        datetime.strptime(day, '%Y-%m-%d')
        customer = data.get('customerId')
        if not isinstance(customer, str) or not re_full_digits(customer):
            raise ValueError('按日期核查必须限定客户ID')
        page = data.get('pageIndex', 1)
        if type(page) is not int or not 1 <= page <= 100: raise ValueError('页号无效')
        return client.call('list', {'pageIndex': page, 'pageSize': 20, 'isSum': True,
            'open_vouchdate_begin': day + ' 00:00:00', 'open_vouchdate_end': day + ' 23:59:59',
            'simpleVOs': [{'field': 'agentId', 'op': 'eq', 'value1': customer}]})
    order_id = data.get('id')
    if order_id is not None:
        if not isinstance(order_id, str) or not re_full_digits(order_id):
            raise ValueError('订单ID需为不超过20位的数字字符串')
        return client.call('detail', query={'id': order_id})
    code = data.get('code')
    if not isinstance(code, str) or not code.strip() or len(code) > 80:
        raise ValueError('请提供具体订单编号，不支持全量查询')
    return client.call('list', {'pageIndex': 1, 'pageSize': 10,
                                'code': code.strip(), 'isSum': True})


def query_stock(data, client=CLIENT):
    """Read-only, single product filter. No arbitrary gateway paths or full scans."""
    import re
    if not isinstance(data, dict): raise ValueError('库存查询条件必须为对象')
    code = data.get('productCode')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code):
        raise ValueError('请提供一个有效商品编码，不支持全量库存查询')
    if set(data) - {'productCode', 'org', 'warehouseCode'}:
        raise ValueError('库存查询包含不支持的条件')
    body = {'productn.code': code, 'billnum': 'voucher_order', 'bNeedSubQty': True}
    if data.get('org') not in (None, ''):
        org = data['org']
        if not isinstance(org, str) or not re_full_digits(org): raise ValueError('库存组织ID需为数字字符串')
        body['org'] = org
    if data.get('warehouseCode') not in (None, ''):
        warehouse = data['warehouseCode']
        if not isinstance(warehouse, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', warehouse):
            raise ValueError('仓库编码无效')
        body['warehouse.code'] = warehouse
    return client.call('stock', body)


def execute_plan(plan, confirmation, client=CLIENT, state=STATE):
    if not plan['ready']: raise ValueError('待补充信息尚未完成')
    if confirmation != '确认仅保存，不提交不审核': raise ValueError('请确认仅保存')
    if plan['mode'] == 'mock':
        return {'state': 'mock_saved', 'message': '模拟保存成功，未调用用友', 'id': 'MOCK-' + plan['digest'][:12]}
    require_save_allowed(state, plan['payload']['data'])
    state.mkdir(exist_ok=True)
    marker = state / (plan['digest'] + '.json')
    try:
        with marker.open('x', encoding='utf-8') as f:
            json.dump({'state': 'dispatch_started', 'at': time.time(), **plan.get('attemptEvidence', {})}, f)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError: raise ValueError('这份订单已尝试保存；为避免重复录单，请先到用友核查，不能直接重试')
    stage, saved_identity = 'before_save', {}
    try:
        if isinstance(client, YonSuite): client.token()
        stage = 'save_request'
        result = client.call('save', plan['payload'])
        stage = 'save_response'
        saved = result.get('data') or {}
        order_id = saved.get('id')
        if not order_id: raise ValueError('保存响应未返回订单ID，需人工核查')
        saved_identity = {'id': str(order_id), 'code': safe_message(saved.get('code', ''))}
        write_receipt(marker, {'state': 'saved_readback_pending', 'stage': stage, **saved_identity, **plan.get('attemptEvidence', {})})
        stage = 'readback'
        verified = client.call('detail', query={'id': str(order_id)}).get('data') or {}
        expected = plan['payload']['data']
        mismatch = []
        for key in ('salesOrgId', 'agentId', 'transactionTypeId', 'settlementOrgId', 'invoiceAgentId'):
            if str(verified.get(key)) != str(expected[key]): mismatch.append(key)
        if expected.get('memo') and str(verified.get('memo') or '') != expected['memo']: mismatch.append('memo')
        if expected.get('hopeReceiveDate') and str(verified.get('hopeReceiveDate') or '').split(' ')[0] != expected['hopeReceiveDate'].split(' ')[0]:
            mismatch.append('hopeReceiveDate')
        if str(verified.get('status')) != '0': mismatch.append('订单不是开立状态')
        if number(verified.get('payMoney'), '回读金额', zero=True) != expected['payMoney']: mismatch.append('payMoney')
        lines = verified.get('orderDetails', [])
        if len(lines) != len(expected['orderDetails']): mismatch.append('明细行数')
        else:
            def key(row): return (str(row.get('productId')), str(row.get('iProductAuxUnitId')),
                                 number(row.get('subQty'), '回读数量'), number(row.get('oriTaxUnitPrice'), '回读价格'), str(row.get('memo') or ''))
            if sorted(map(key, lines)) != sorted(map(key, expected['orderDetails'])): mismatch.append('商品/单位/数量/价格')
        receipt = {'state': 'saved_verified' if not mismatch else 'saved_needs_review',
                   'id': str(order_id), 'code': str(verified.get('code', '')), 'mismatch': mismatch,
                   'stage': 'readback_complete', 'status': str(verified.get('status', '')),
                   'message': '已调用保存，未调用提交或审核；请人工复核' }
    except Exception as e:
        diagnostic = e.diagnostic if isinstance(e, YonSuiteError) else {
            'category': type(e).__name__, 'message': '本机处理或返回结构异常，原始异常未记录'}
        not_dispatched = stage == 'before_save' or (stage == 'save_request' and diagnostic.get('requestDispatched') is False)
        receipt = {'state': 'saved_needs_review' if saved_identity else ('not_dispatched' if not_dispatched else 'uncertain'),
                   'stage': stage, **saved_identity, 'diagnostic': diagnostic,
                   'message': '处理未完整确认，禁止自动重试；请先核查已有记录。'}
    receipt.update(plan.get('attemptEvidence', {}))
    write_receipt(marker, receipt)
    return receipt


def retry_confirmed_plan(plan, confirmation, client=CLIENT, state=STATE):
    """One explicitly confirmed retry of this order; preserve the original marker and upstream key."""
    if confirmation != RETRY_CONFIRMATION: raise ValueError('需要明确确认原订单不存在且仅重试一次')
    if plan.get('mode') != 'live' or plan.get('digest') != RETRY_ALLOWED_DIGEST:
        raise ValueError('本次重试授权仅适用于已核查的327.78元订单，不适用于其他或修改后的请求')
    original = state / (plan['digest'] + '.json')
    if not original.is_file(): raise ValueError('原尝试记录不存在，不是重试场景')
    prior = json.loads(original.read_text(encoding='utf-8'))
    if prior.get('state') != 'uncertain' or prior.get('id'):
        raise ValueError('原尝试已有保存线索或不属于未确认状态，请先回读，不能重试')
    retry_digest = hashlib.sha256(('confirmed-retry-1:' + plan['digest']).encode()).hexdigest()
    evidence = {'retryOf': plan['digest'], 'attempt': 2, 'absenceConfirmedBy': 'user',
                'confirmation': RETRY_CONFIRMATION, 'confirmedAt': time.time()}
    failed_retry = state / (retry_digest + '.json')
    if failed_retry.is_file():
        previous = json.loads(failed_retry.read_text(encoding='utf-8'))
        diagnostic = previous.get('diagnostic') or {}
        key = plan['payload']['data'].get('resubmitCheckKey', '')
        # Only this explicit pre-business gateway rejection permits a corrected request.
        if (previous.get('stage') == 'save_request' and not previous.get('id')
                and str(diagnostic.get('httpStatus')) == '400'
                and str(diagnostic.get('apiCode')) == '310024'
                and 'resubmitCheckKey不能超过32位' in diagnostic.get('message', '')
                and key == 'gc-demo-' + plan['digest'][:24]):
            evidence.update(attempt=3, correctionOf=retry_digest, correction='310024: resubmitCheckKey shortened to 32')
            retry_digest = hashlib.sha256(('corrected-key-1:' + plan['digest']).encode()).hexdigest()
        else:
            raise ValueError('已使用一次重试机会，需人工核查，不可继续重发')
    attempt = {**plan, 'digest': retry_digest, 'attemptEvidence': {
        **evidence}}
    # The corrected-key exception above is bounded to the explicit 310024 rejection.
    return execute_plan(attempt, '确认仅保存，不提交不审核', client, state)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def allowed(self):
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return self.headers.get('Host') in hosts and self.headers.get('Origin') in (None, *('http://' + h for h in hosts))

    def send(self, status, data, mime='application/json; charset=utf-8'):
        raw = data if isinstance(data, bytes) else json.dumps(browser_safe(data), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed(): return self.send(403, {'error': '仅限本机同源访问'})
        path = urlsplit(self.path).path
        if path == '/api/status':
            return self.send(200, {'appId': 'guichang-yonyou-import-demo', 'version': VERSION, 'configured': CLIENT.configured(), **save_status(STATE), 'serviceInstanceId': SERVICE_INSTANCE, 'defaultMode': 'preview', 'stockQueryEnabled': True, 'submitSupported': True, 'multiuserOrderIdentity': True, 'archiveReuseSupported': True})
        if path == '/api/diagnostic':
            digest = parse_qs(urlsplit(self.path).query).get('digest', [''])[0]
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                return self.send(400, {'error': '需要64位摘要'})
            marker = STATE / (digest + '.json')
            if not marker.is_file(): return self.send(404, {'error': '没有本机回执'})
            return self.send(200, json.loads(marker.read_text(encoding='utf-8')))
        if path in ('/', '/app.js', '/style.css'):
            target = ROOT / {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
            mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
            return self.send(200, target.read_bytes(), mime)
        return self.send(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.allowed(): return self.send(403, {'error': '仅限本机同源访问'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 10_000_000: raise ValueError('请求体缺失或超过10MB')
            raw = self.rfile.read(size)
            path = urlsplit(self.path).path
            if path == '/api/xlsx': return self.send(200, read_xlsx(raw))
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise ValueError('需要JSON请求')
            data = json.loads(raw)
            if path.startswith('/api/invoice/'):
                return self.send(200, INVOICES.dispatch(path.removeprefix('/api/invoice/'), data))
            if path == '/api/resolve-order': return self.send(200, resolve_order(data, CLIENT))
            if path == '/api/references': return self.send(200, search_references(data, CLIENT))
            if path == '/api/order-query': return self.send(200, query_order(data))
            if path == '/api/submit-preview': return self.send(200, SUBMISSIONS.preview(data.get('digest')))
            if path == '/api/submit': return self.send(200, SUBMISSIONS.submit(data.get('token'), data.get('confirmation')))
            if path == '/api/stock': return self.send(200, query_stock(data))
            if path == '/api/demo': return self.send(200, demo_input())
            if path == '/api/preview':
                mode = data.get('mode', 'live')
                if mode not in ('live', 'mock'): raise ValueError('模式无效')
                plan = build_plan(data['rows'], data['header'], mode, data.get('orderIdentity'))
                plan_id = secrets.token_urlsafe(24)
                with LOCK:
                    for k in list(PLANS):
                        if time.time() - PLANS[k]['created'] > 1800: del PLANS[k]
                    if len(PLANS) >= 100: raise ValueError('预览过多，请稍后重试')
                    PLANS[plan_id] = {**plan, 'created': time.time()}
                return self.send(200, {**plan, 'planId': plan_id, 'serviceInstanceId': SERVICE_INSTANCE})
            if path in ('/api/save', '/api/retry-save'):
                with LOCK: plan = PLANS.get(data.get('planId'))
                if not plan or time.time() - plan['created'] > 1800: raise ValueError('预览已过期，请重新预览')
                action = retry_confirmed_plan if path == '/api/retry-save' else execute_plan
                return self.send(200, action(plan, data.get('confirmation')))
            if path == '/api/products':
                name = str(data.get('name', '')).strip()
                if not name: raise ValueError('请填写商品名称')
                page = data.get('pageIndex', 1)
                if type(page) is not int or not 1 <= page <= 100: raise ValueError('页号必须为1至100的整数')
                return self.send(200, CLIENT.call('products', {'pageIndex': page, 'pageSize': 20, 'productName': name}))
            if path == '/api/price':
                h, r = data['header'], data['row']
                def long(value):
                    text = str(value)
                    if not re_full_digits(text): raise ValueError('取价需要系统数字ID，不接受名称/编码')
                    return int(text)
                request = {'data': [{'dateTime': h['vouchdate'] + ' 00:00:00', 'saleOrgId': long(h['salesOrgId']),
                    'quantity': number(r['quantity'], '数量'), 'billnum': 'voucher_order', 'amountUnit': long(r['unitId']),
                    'isTaxIncluded': True, 'currency': {'id': str(h['currencyId'])},
                    'dimensions': {'productId': long(r['productId']), 'agentId': long(h['agentId']), 'billingOrg': long(h['settlementOrgId'])}}]}
                return self.send(200, CLIENT.call('price', request))
            return self.send(404, {'error': '未开放此操作'})
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return self.send(400, {'error': str(e)[:250]})
        except Exception:
            return self.send(500, {'error': '本机处理失败；没有自动重试，请检查输入和服务状态'})


def re_full_digits(value): return value.isascii() and value.isdecimal() and len(value) <= 20


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4192)
    args = parser.parse_args()
    print(f'用友导入 Demo: http://127.0.0.1:{args.port}/ (真实保存默认关闭)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()

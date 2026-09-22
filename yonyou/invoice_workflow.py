"""Order-line invoice allocation. Credentials remain in the loopback connector.

Preview is read-only. Writes require an independently verified invoice flow,
explicit confirmation, fresh source reads and a durable invoice-level fence.
"""
import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from core import browser_safe, wire
from invoice_policy import read_policy, require_scope
import invoice_config_match

CONFIRM = '确认按预览保存销售发票，不提交不审核'
LOCK = threading.RLock()
JOBS = set()
CENT = Decimal('.01')


def text(value):
    return str(value if value is not None else '').strip()


def dec(value, label, zero=False):
    s = text(value)
    if not re.fullmatch(r'\d{1,12}(\.\d{1,6})?', s):
        raise ValueError(label + '需要非负数字，最多6位小数')
    try:
        n = Decimal(s)
    except InvalidOperation:
        raise ValueError(label + '无效') from None
    if n < 0 or (not zero and n == 0): raise ValueError(label + '必须大于0')
    return n


def sha(value):
    return hashlib.sha256(json.dumps(browser_safe(value), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            f.write(json.dumps(browser_safe(value), ensure_ascii=False))
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def normalize(data):
    if not isinstance(data, dict): raise ValueError('发票格式无效')
    h = data.get('header') or {}
    if not isinstance(h, dict): raise ValueError('发票抬头无效')
    fields = ('customerId', 'customerName', 'salesOrgId', 'salesOrgName', 'invoiceNo',
              'invoiceCode', 'invoiceDate', 'invoiceType', 'memo')
    header = {k: text(h.get(k)) for k in fields}
    for key, label in [('customerId', '客户'), ('salesOrgId', '销售组织')]:
        if not re.fullmatch(r'\d{1,20}', header[key]): raise ValueError('请查询并选择' + label)
    if not re.fullmatch(r'[A-Za-z0-9-]{1,40}', header['invoiceNo']): raise ValueError('请填写有效发票号码')
    if header['invoiceCode'] and not re.fullmatch(r'[A-Za-z0-9-]{1,40}', header['invoiceCode']): raise ValueError('发票代码无效')
    try: date.fromisoformat(header['invoiceDate'])
    except ValueError: raise ValueError('请填写有效发票日期') from None
    if header['invoiceType'] not in ('1', '2', '3', '9', '10', '11', '12', '13'): raise ValueError('请选择发票类型')
    if any(len(v) > 500 for v in header.values()): raise ValueError('发票信息过长')
    rows = data.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200: raise ValueError('需要1至200条发票商品')
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict): raise ValueError('发票商品格式无效')
        r = {k: text(row.get(k)) for k in ('code', 'name', 'spec', 'unit', 'quantity', 'price', 'taxRate', 'amount')}
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', r['code']): raise ValueError(f'第{index+1}行需要用友商品编码')
        if not r['unit'] or any(len(v) > 200 for v in r.values()): raise ValueError(f'第{index+1}行单位缺失或内容过长')
        qty, price = dec(r['quantity'], '数量'), dec(r['price'], '含税单价')
        rate = dec(r['taxRate'], '税率百分数', True)
        if rate > 100: raise ValueError('税率请按百分数填写，例如13')
        amount = dec(r['amount'], '含税金额')
        if abs((qty * price).quantize(CENT, rounding=ROUND_HALF_UP) - amount) > CENT:
            raise ValueError(f'第{index+1}行数量×含税单价与含税金额不一致')
        result.append({**r, 'line': index+1, 'quantity': str(qty), 'price': str(price),
                       'taxRate': str(rate), 'amount': str(amount)})
    return {'header': header, 'rows': result}


def sample():
    return {'mode': 'mock', 'header': {'customerId': '1001', 'customerName': '演示客户',
        'salesOrgId': '2001', 'salesOrgName': '演示销售组织', 'invoiceNo': 'DEMO-' + secrets.token_hex(4),
        'invoiceCode': '', 'invoiceDate': date.today().isoformat(), 'invoiceType': '10', 'memo': ''},
        'rows': [{'code': 'DEMO-A4', 'name': '演示复印纸', 'spec': 'A4', 'unit': '包',
                  'quantity': '7', 'price': '20', 'taxRate': '13', 'amount': '140'}]}


def mock_orders():
    rows = []
    for num, day, qty, invoiced in [('1', '2026-01-01', '10', '6'), ('2', '2026-01-05', '8', '0')]:
        rows.append({'id': '300'+num, 'code': 'DEMO-ORDER-'+num, 'vouchdate': day,
            'invoiceTitle':'演示客户', 'taxNum':'DEMO-TAX-NO',
            'agentId': '1001', 'salesOrgId': '2001', 'settlementOrgId': '2001', 'invoiceAgentId': '1001',
            'status': '1', 'pubts': 'demo-v1', 'orderPrices': {'currency': '4001', 'natCurrency': '4001', 'exchangeRateType': '1', 'exchRate': '1'},
            'orderDetails': [{'id': '500'+num, 'productId': '6001', 'productCode': 'DEMO-A4',
                'productId_name': '演示复印纸', 'qty': qty, 'priceQty': qty, 'subQty': qty,
                'invoiceQty': invoiced, 'invoiceStatus': '1' if invoiced != '0' else '0',
                'masterUnitId': '7001', 'qtyName': '包', 'iProductUnitId': '7001', 'iProductAuxUnitId': '7001',
                'oriTaxUnitPrice': '20', 'taxId': '8001', 'taxRate': '13'}]})
    return rows


def collect_orders(client, header):
    """Complete bounded history; never return a successful partial scan."""
    found, expected, seen = [], None, set()
    for page in range(1, 11):
        body = {'pageIndex': page, 'pageSize': 50, 'isSum': True,
                'open_vouchdate_end': header['invoiceDate'] + ' 23:59:59',
                'simpleVOs': [{'field': 'agentId', 'op': 'eq', 'value1': header['customerId']},
                              {'field': 'salesOrgId', 'op': 'eq', 'value1': header['salesOrgId']}],
                'queryOrders': [{'field': 'vouchdate', 'order': 'asc'}, {'field': 'id', 'order': 'asc'}]}
        value = client.call('list', body).get('data')
        if not isinstance(value, dict) or not isinstance(value.get('recordList'), list): raise ValueError('订单列表结构无效')
        count = value.get('recordCount')
        if not re.fullmatch(r'\d+', text(count)): raise ValueError('订单列表缺少总条数，无法证明历史完整')
        count = int(count)
        if expected is None: expected = count
        if count != expected or count > 500: raise ValueError('历史订单超过500张或查询期间发生变化，未生成分配，请联系管理员分批核实')
        records = value['recordList']
        for item in records:
            identity = text(item.get('id'))
            if not re.fullmatch(r'\d{1,20}', identity) or identity in seen: raise ValueError('订单分页重复或编号异常，请重新查询')
            if text(item.get('agentId')) != header['customerId'] or text(item.get('salesOrgId')) != header['salesOrgId']:
                raise ValueError('用友未正确应用客户/组织筛选，已停止查询')
            seen.add(identity)
            detail = client.call('detail', query={'id': identity}).get('data')
            if not isinstance(detail, dict) or text(detail.get('id')) != identity: raise ValueError('订单详情身份不一致')
            found.append(detail)
        if len(seen) == expected: return found
        if not records or len(seen) > expected: raise ValueError('订单分页不完整，请重新查询')
    raise ValueError('历史订单未全部读取，不能确定最早订单')


def allocate(draft, orders):
    header, demand = draft['header'], draft['rows']
    candidates, issues, allocations = [], [], []
    codes = {r['code'] for r in demand}
    identities = set()
    for order in orders:
        if text(order.get('agentId')) != header['customerId'] or text(order.get('salesOrgId')) != header['salesOrgId']:
            raise ValueError('订单客户或销售组织与当前发票不符')
        day = text(order.get('vouchdate'))[:10]
        try: date.fromisoformat(day)
        except ValueError: raise ValueError('来源订单缺少有效日期') from None
        if day > header['invoiceDate'] or text(order.get('status')) != '1': continue
        details = order.get('orderDetails')
        if not isinstance(details, list): raise ValueError('订单缺少商品明细')
        for row in details:
            if text(row.get('productCode')) not in codes: continue
            key = (text(order.get('id')), text(row.get('id')))
            if not all(re.fullmatch(r'\d{1,20}', x) for x in key) or key in identities: raise ValueError('来源明细身份缺失或重复')
            identities.add(key)
            if text(row.get('invoiceStatus')) == '2': continue
            if text(row.get('invoiceStatus')) not in ('0', '1'): raise ValueError('商品开票状态未知，不能估算余量')
            qty = dec(row.get('qty'), '订单数量')
            inv = dec(row.get('invoiceQty'), '累计开票数量', True)
            closed = dec(row.get('closedRowCount', '0'), '行关闭数量', True)
            if inv > qty or closed > 0: raise ValueError('订单商品存在超开或关闭数量，需要人工核实')
            if inv == qty: continue
            if text(row.get('isAdvRecInv')).lower() == 'true': raise ValueError('预收款开票暂不自动分配')
            candidates.append({'order': order, 'row': row, 'remaining': qty-inv, 'available': qty-inv,
                               'key': key, 'date': day})
    candidates.sort(key=lambda x: (x['date'], text(x['order'].get('createTime')), *x['key']))
    for item in demand:
        left = dec(item['quantity'], '发票数量')
        matching = [c for c in candidates if text(c['row'].get('productCode')) == item['code'] and c['remaining'] > 0]
        product_ids = {(text(c['row'].get('productId')), text(c['row'].get('skuId'))) for c in matching}
        if len(product_ids) > 1 or any(not p[0] for p in product_ids):
            issues.append(f'第{item["line"]}行编码对应多个或缺失商品ID，请核实'); continue
        for candidate in matching:
            r, o = candidate['row'], candidate['order']
            price = r.get('oriTaxUnitPrice')
            try:
                if text(r.get('qtyName')) != item['unit']: raise ValueError('单位不同')
                spec = text(r.get('modelDescription') or r.get('model'))
                if item['spec'] and spec and item['spec'] != spec: raise ValueError('规格不同')
                precision = text(r.get('unit_Precision'))
                if precision and (not precision.isdigit() or int(precision)>6): raise ValueError('来源单位精度无效')
                if precision and dec(item['quantity'], '数量') != dec(item['quantity'], '数量').quantize(Decimal(1).scaleb(-int(precision))): raise ValueError('数量超过单位允许的小数精度')
                if dec(price, '来源含税单价') != dec(item['price'], '发票含税单价'): raise ValueError('含税单价不同')
                if dec(r.get('taxRate'), '来源税率', True) != dec(item['taxRate'], '发票税率', True): raise ValueError('税率不同')
                if dec(r.get('priceQty'), '订单计价数量') != dec(r.get('qty'), '订单数量'): raise ValueError('计价单位需要换算')
                if r.get('subQty') is not None and dec(r['subQty'], '销售数量') != dec(r['qty'], '订单数量'): raise ValueError('销售单位需要换算')
                if text(r.get('iProductAuxUnitId')) != text(r.get('masterUnitId')) or text(r.get('iProductUnitId')) != text(r.get('masterUnitId')): raise ValueError('单位档案不一致')
            except ValueError as error:
                issues.append(f'第{item["line"]}行最早订单{o.get("code", "")}：{error}，未跳过该订单'); break
            amount = min(left, candidate['remaining'])
            allocations.append({'line': item['line'], 'code': item['code'], 'name': item['name'], 'unit': item['unit'],
                'orderId': candidate['key'][0], 'orderCode': text(o.get('code')), 'detailId': candidate['key'][1],
                'orderDate': candidate['date'], 'available': str(candidate['remaining']), 'quantity': str(amount),
                'remaining': str(candidate['remaining']-amount), 'price': item['price'], 'taxRate': item['taxRate'],
                'amount': str((amount*dec(item['price'], '价格')).quantize(CENT, rounding=ROUND_HALF_UP)),
                'sourceOrder': o, 'sourceRow': r})
            candidate['remaining'] -= amount; left -= amount
            if left == 0: break
        if left > 0: issues.append(f'第{item["line"]}行 {item["code"]} 还有{left}未分配；整张发票暂不保存')
    return allocations, issues


def build_payload(draft, allocations, config):
    if not allocations: raise ValueError('没有可保存的分配')
    h, first = draft['header'], allocations[0]['sourceOrder']
    require_scope(config, h, first)
    prices = first.get('orderPrices') or {}
    signature = lambda o: tuple(text(o.get(k)) for k in ('settlementOrgId', 'invoiceAgentId')) + tuple(text((o.get('orderPrices') or {}).get(k)) for k in ('currency', 'natCurrency', 'exchangeRateType', 'exchRate'))
    if any(signature(a['sourceOrder']) != signature(first) for a in allocations): raise ValueError('跨订单开票主体或币种不同，需要分开发票处理')
    if text(prices.get('currency')) != text(prices.get('natCurrency')) or dec(prices.get('exchRate'), '汇率') != 1: raise ValueError('当前仅支持原币与本币相同、汇率1')
    def required(v, name):
        if not text(v): raise ValueError('来源档案缺少' + name)
        return text(v)
    data = {'orgId': required(first.get('settlementOrgId'), '开票组织'), 'agentId': h['customerId'],
        'invAgentId': required(first.get('invoiceAgentId'), '开票客户'),
        'transactionTypeId': required(config.get('transactionTypeId'), '已验证的销售发票交易类型'),
        'bizFlow': required(config.get('bizFlow'), '已验证的订单开票业务流'),
        'vouchdate': h['invoiceDate']+' 00:00:00', 'source': 'voucher_order', 'invDirection': 2,
        'currency': required(prices.get('currency'), '币种'), 'natCurrency': required(prices.get('natCurrency'), '本币'),
        'exchangeRateType': required(prices.get('exchangeRateType'), '汇率类型'), 'exchRate': 1,
        'invoiceType': int(h['invoiceType']), 'einvoiceHm': h['invoiceNo'], 'einvoiceNo': h['invoiceCode'],
        'memo': h['memo'], 'status': 0, '_status': 'Insert', 'saleInvoiceDetails': []}
    if config.get('bizFlowVersion'): data['bizFlow_version']=config['bizFlowVersion']
    for target, source in [('invoiceTitle', 'invoiceTitle'), ('invoiceTitleType', 'invoiceTitleType'),
                           ('invAgentTaxNo', 'taxNum'), ('invAgentAdress', 'invoiceAddress'), ('invAgentTel', 'invoiceTelephone'),
                           ('invAgentBank','bankName'), ('invAgentSubBank','subBankName'), ('invAgentBankNo','bankAccount')]:
        if text(first.get(source)): data[target] = first[source]
    for field, label in [('invoiceTitle','开票抬头'), ('invAgentTaxNo','购方税号')]: required(data.get(field), label)
    if h['invoiceType']=='1':
        for field in ('invAgentBank','invAgentSubBank','invAgentBankNo','invAgentAdress','invAgentTel'): required(data.get(field), field)
    for a in allocations:
        r = a['sourceRow']; qty = dec(a['quantity'], '分配数量'); price = dec(a['price'], '价格'); rate = dec(a['taxRate'], '税率', True)
        gross = (qty*price).quantize(CENT, rounding=ROUND_HALF_UP)
        net = (gross/(1+rate/100)).quantize(CENT, rounding=ROUND_HALF_UP)
        line = {'source': 'voucher_order', 'makeRuleCode': 'order_saleinvoice', 'invoiceSource': 1,
                'sourceid': int(a['orderId']), 'sourceautoid': int(a['detailId']), 'srcVoucherNo': a['orderCode'],
                'orderId': int(a['orderId']), 'orderDetailId': int(a['detailId']),
                'productId': required(r.get('productId'), '商品ID'), 'unitId': required(r.get('masterUnitId'), '主单位'),
                'chargeUnitId': required(r.get('iProductUnitId'), '计价单位'), 'taxId': required(r.get('taxId'), '税目'),
                'qty': qty, 'priceQty': qty, 'subQty': qty, 'taxRate': rate, 'invPriceExchRate': 1,
                'oriTaxUnitPrice': price, 'natTaxUnitPrice': price, 'sourceOriPrice': price,
                'oriUnitPrice': (price/(1+rate/100)).quantize(Decimal('.000001'), rounding=ROUND_HALF_UP),
                'oriSum': gross, 'natSum': gross, 'oriMoney': net, 'natMoney': net,
                'oriTax': gross-net, 'natTax': gross-net, '_status': 'Insert'}
        line['natUnitPrice'] = line['oriUnitPrice']
        if r.get('skuId'): line['skuId'] = text(r['skuId'])
        data['saleInvoiceDetails'].append(line)
    total = sum(x['oriSum'] for x in data['saleInvoiceDetails'])
    if total != sum(dec(r['amount'], '发票金额') for r in draft['rows']): raise ValueError('拆分后含税金额与发票金额不一致，请核实尾差')
    for row in draft['rows']:
        if sum(dec(a['amount'],'分配金额') for a in allocations if a['line']==row['line']) != dec(row['amount'],'发票行金额'):
            raise ValueError(f'第{row["line"]}行拆分金额有尾差，需要人工核对')
    for key in ('oriSum', 'natSum', 'oriMoney', 'natMoney', 'oriTax', 'natTax'):
        data[key] = sum(x[key] for x in data['saleInvoiceDetails'])
    return {'data': data}


class InvoiceService:
    def __init__(self, client, state, mock=False, config=None, clock=time.time):
        self.client, self.state, self.mock, self.clock = client, Path(state), mock, clock
        self._config = config

    def config(self):
        if self.mock: return {'enabled': True, 'verified': True, 'transactionTypeId': '9001', 'bizFlow': 'demo-flow'}
        if self._config is not None: return self._config
        return read_policy(self.state)

    def status(self):
        c = self.config()
        connected = self.mock or bool(self.client and self.client.configured())
        automatic=c.get('mode')=='history'
        ready = connected and bool(c.get('enabled') and (automatic or (c.get('verified') and c.get('transactionTypeId') and c.get('bizFlow'))))
        return {'supported': True, 'saveEnabled': ready, 'mode': 'mock' if self.mock else 'live',
                'connected': connected, 'configuration': c.get('control','injected'),
                'automaticConfiguration':automatic, 'configMatchingVersion':2,
                'confirmationRequired': True,
                'message': '模拟模式，不调用用友' if self.mock else ('连接尚未配置，请在本机用友连接窗口填写应用凭证' if not connected else ('已连接；匹配时自动读取历史开票流程，核对后人工确认下推' if ready and automatic else ('已连接；真实下推须核对预览并逐次人工确认' if ready else '已连接；开票功能尚未启用，请检查本机配置')))}

    def _scope(self, data):
        scope = data.get('scope')
        if not isinstance(scope, str) or not re.fullmatch(r'[a-f0-9]{64}', scope): raise ValueError('工作区身份无效')
        return scope

    def dispatch(self, action, data):
        scope = self._scope(data)
        if action == 'status': return self.status()
        if action == 'start-preview':
            normalize(data)
            job = secrets.token_hex(24)
            job_key = (str(self.state.resolve()), scope)
            with LOCK:
                if job_key in JOBS: raise ValueError('当前账号正在查询订单，请等待本次查询完成')
                if len(JOBS) >= 4: raise ValueError('订单查询繁忙，请稍后再试')
                JOBS.add(job_key)
                try: atomic(self.state/'jobs'/(job+'.json'), {'scope':scope, 'state':'running', 'created':self.clock()})
                except Exception:
                    JOBS.discard(job_key); raise
            def run():
                try:
                    result = self.preview(scope, data)
                    value = {'state':'complete', 'result':result}
                except ValueError as e:
                    value = {'state':'failed', 'error':str(e)[:250]}
                except Exception:
                    value = {'state':'failed', 'error':'订单查询未完成，请检查连接后重新匹配；未写入用友'}
                with LOCK:
                    try: atomic(self.state/'jobs'/(job+'.json'), {'scope':scope, 'created':self.clock(), **value})
                    finally: JOBS.discard(job_key)
            threading.Thread(target=run, daemon=True).start()
            return {'jobId':job, 'state':'running'}
        if action == 'job':
            job = data.get('jobId')
            if not isinstance(job, str) or not re.fullmatch(r'[a-f0-9]{48}', job): raise ValueError('查询任务无效')
            p = self.state/'jobs'/(job+'.json')
            if not p.exists(): raise ValueError('查询任务不存在，请重新匹配')
            with LOCK:
                value = json.loads(p.read_text(encoding='utf-8'))
                if value['scope'] != scope: raise ValueError('查询任务不属于当前账号')
                if value['state'] == 'running' and (str(self.state.resolve()),scope) not in JOBS:
                    return {'state':'failed','error':'服务已重启，查询已中断；请重新匹配'}
            return {k:v for k,v in value.items() if k != 'scope'}
        if action == 'sample':
            if not self.mock: raise ValueError('演示数据仅限模拟模式')
            return sample()
        if action == 'history':
            values = []
            with LOCK:
                for p in (self.state/'receipts').glob('*.json'):
                    r = json.loads(p.read_text(encoding='utf-8'))
                    if r.get('scope') == scope: values.append(self._public_receipt(r))
            return {'receipts': sorted(values, key=lambda r: r['created'], reverse=True)[:50]}
        if action == 'preview': return self.preview(scope, data)
        if action == 'prepare-save': return self.prepare_save(scope, data)
        if action == 'save': return self.save(scope, data)
        if action == 'check': return self.check(scope, data.get('receiptKey'))
        raise ValueError('未知发票操作')

    def orders(self, draft):
        return mock_orders() if self.mock else collect_orders(self.client, draft['header'])

    def preview(self, scope, data):
        draft = normalize(data)
        orders = self.orders(draft)
        allocations, issues = allocate(draft, orders)
        config, payload = self.config(), None
        resolved_config=config
        candidates=[];selected=None
        payload_issue = ''
        if allocations and not issues:
            try:
                if config.get('mode')=='history':
                    choice=data.get('flowChoice','')
                    reference=data.get('referenceInvoiceCode','')
                    if not isinstance(choice,str) or (choice and not re.fullmatch(r'[a-f0-9]{64}',choice)):raise ValueError('请选择本次查询返回的开票流程')
                    if not isinstance(reference,str):raise ValueError('参考销售发票单号无效')
                    candidates=invoice_config_match.discover(self.client,draft,allocations,reference)
                    if choice:
                        selected=next((c for c in candidates if c['key']==choice),None)
                        if selected is None:raise ValueError('所选开票流程已不可用，请重新选择')
                    elif len(candidates)==1:selected=candidates[0]
                    if selected is None:
                        raise ValueError('找到多个适用开票流程，请按名称选择后重新匹配' if candidates else '未找到同客户、同订单流程且已开税票的历史记录；可填写一张已有销售发票单号重新匹配')
                    resolved_config=selected['config']
                payload = build_payload(draft, allocations, resolved_config)
            except ValueError as e: payload_issue = str(e)
        org = text(allocations[0]['sourceOrder'].get('settlementOrgId')) if allocations else draft['header']['salesOrgId']
        key = sha([self.mock, org, draft['header']['invoiceCode'].upper(), draft['header']['invoiceNo'].upper()])
        token = secrets.token_hex(24)
        plan = {'scope': scope, 'created': self.clock(), 'draft': draft, 'allocations': allocations,
                'issues': issues, 'payloadIssue': payload_issue, 'payload': payload, 'key': key,
                'configCandidate':selected, 'resolvedConfig':resolved_config,
                'configHash': sha(config), 'sourceHash': sha(orders)}
        atomic(self.state/'plans'/(token+'.json'), plan)
        receipt = self.state/'receipts'/(key+'.json')
        blocked = receipt.exists()
        return {'token': token, 'receiptKey': key, 'expiresAt': plan['created']+900,
                'allocations': [{k:v for k,v in a.items() if k not in ('sourceOrder', 'sourceRow')} for a in allocations],
                'issues': issues, 'payloadIssue': payload_issue, 'orderCount': len(orders),
                'configurationCandidates':[invoice_config_match.public(c) for c in candidates],
                'matchedConfiguration':invoice_config_match.public(selected) if selected else None,
                'automaticConfiguration':config.get('mode')=='history',
                'ready': not issues and bool(payload) and not blocked,
                'duplicate': blocked, 'saveEnabled': self.status()['saveEnabled'], 'mode': 'mock' if self.mock else 'live'}

    def _receipt(self, key, scope):
        if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key): raise ValueError('回执编号无效')
        path = self.state/'receipts'/(key+'.json')
        if not path.exists(): raise ValueError('没有保存回执，请重新预览')
        value = json.loads(path.read_text(encoding='utf-8'))
        if value['scope'] != scope: raise ValueError('该发票已有处理记录，请联系原操作员核实')
        return path, value

    @staticmethod
    def _public_receipt(r):
        return {k:v for k,v in r.items() if k not in ('scope', 'plan')}

    def _load_plan(self, scope, data):
        token = data.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{48}', token): raise ValueError('预览凭据无效')
        path = self.state/'plans'/(token+'.json')
        if not path.exists(): raise ValueError('预览不存在，请重新匹配')
        with LOCK:
            plan = json.loads(path.read_text(encoding='utf-8'))
        if plan['scope'] != scope: raise ValueError('预览不属于当前工作区')
        if self.clock()-plan['created'] > 900: raise ValueError('预览已过期，请重新匹配')
        if plan['issues'] or not plan['payload']: raise ValueError('分配或档案尚未完成，不能保存')
        if not self.status()['saveEnabled'] or sha(self.config()) != plan['configHash']: raise ValueError('开票保存未启用或配置已变化，请重新预览')
        return path, plan

    def prepare_save(self, scope, data):
        """Returns an immutable review summary; never calls invoiceSave."""
        with LOCK:
            path, plan = self._load_plan(scope, data)
            if (self.state/'receipts'/(plan['key']+'.json')).exists():
                raise ValueError('此发票已有处理记录，请核查结果')
            nonce = secrets.token_hex(24)
            expires = min(self.clock()+300, plan['created']+900)
            plan['humanConfirmation'] = {'hash':sha(nonce),'expires':expires}
            atomic(path, plan)
            return {'confirmationToken':nonce, 'expiresAt':expires,
                    'matchedConfiguration':invoice_config_match.public(plan['configCandidate']) if plan.get('configCandidate') else None,
                    'header':plan['draft']['header'], 'amount':str(plan['payload']['data']['oriSum']),
                    'allocations':[{k:v for k,v in a.items() if k not in ('sourceOrder','sourceRow')} for a in plan['allocations']]}

    def save(self, scope, data):
        # Serialize all invoice writes and recheck full source history while held.
        with LOCK:
            path, plan = self._load_plan(scope, data)
            if data.get('confirmation') != CONFIRM: raise ValueError('请明确确认当前分配预览')
            if not self.mock:
                confirmation = plan.get('humanConfirmation') or {}
                if (not isinstance(data.get('confirmationToken'),str)
                        or sha(data['confirmationToken']) != confirmation.get('hash')
                        or self.clock() > confirmation.get('expires',0)
                        or data.get('confirmedInvoiceNo') != plan['draft']['header']['invoiceNo']):
                    raise ValueError('请打开真实下推确认窗口，核对并输入当前发票号码；确认过期后需重新确认')
            marker = self.state/'receipts'/(plan['key']+'.json')
            if marker.exists(): return self._public_receipt(self._receipt(plan['key'], scope)[1])
            orders = self.orders(plan['draft'])
            if sha(orders) != plan['sourceHash']: raise ValueError('订单数量或状态已变化，请重新匹配并确认')
            # Rebuild Decimal amounts, never trust JSON floats or a browser payload.
            allocations, issues = allocate(plan['draft'], orders)
            if issues: raise ValueError('订单余量变化，请重新匹配')
            if plan.get('configCandidate'):
                invoice_config_match.revalidate(self.client,plan['configCandidate'],plan['draft'],allocations)
            payload = build_payload(plan['draft'], allocations, plan.get('resolvedConfig',self.config()))
            if not self.mock:
                self._check_existing(plan['draft'], payload)
                self._check_reserved(allocations)
            receipt = {'scope': scope, 'receiptKey': plan['key'], 'created': self.clock(),
                'invoiceNo': plan['draft']['header']['invoiceNo'], 'state': 'uncertain',
                'message': '保存结果待核查，请勿重复保存', 'plan': plan,
                'mode': 'mock' if self.mock else 'live'}
            if not self.mock:
                receipt['confirmationAudit'] = {'confirmedAt':self.clock(), 'invoiceNo':plan['draft']['header']['invoiceNo'],
                    'previewToken':data['token'], 'sourceHash':plan['sourceHash'], 'configHash':plan['configHash']}
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open('x', encoding='utf-8') as f:
                    f.write(json.dumps(browser_safe(receipt), ensure_ascii=False)); f.flush(); os.fsync(f.fileno())
            except FileExistsError: return self._public_receipt(self._receipt(plan['key'], scope)[1])
            if self.mock:
                receipt.update(state='mock_saved', id='MOCK-'+plan['key'][:12], message=f'模拟下推完成，共{len(allocations)}条来源明细；未调用用友')
                atomic(marker, receipt); return self._public_receipt(receipt)
            payload['data']['resubmitCheckKey'] = 'gci-'+plan['key'][:28]
            try:
                saved = self.client.call('invoiceSave', payload).get('data') or {}
                identity = text(saved.get('id'))
                if not re.fullmatch(r'\d{1,20}', identity): raise ValueError('保存未返回有效ID')
                receipt.update(id=identity, code=text(saved.get('code')), state='saved_pending_check')
                atomic(marker, receipt)
            except Exception:
                atomic(marker, receipt); return self._public_receipt(receipt)
            return self.check(scope, plan['key'])

    def _check_existing(self, draft, payload):
        header = draft['header']
        result = self.client.call('invoiceList', {'pageIndex': 1, 'pageSize': 20,
            'simpleVOs': [{'field':'orgId','op':'eq','value1':payload['data']['orgId']},
                         {'field':'einvoiceHm','op':'eq','value1':header['invoiceNo']}]}).get('data')
        if not isinstance(result, dict) or not isinstance(result.get('recordList'), list) or text(result.get('recordCount')) != '0' or result['recordList']:
            raise ValueError('用友已存在同号发票，或无法可靠排除重复；请先核查，未发送保存')

    def _check_reserved(self, allocations):
        keys = {(a['orderId'], a['detailId']) for a in allocations}
        for p in (self.state/'receipts').glob('*.json'):
            r = json.loads(p.read_text(encoding='utf-8'))
            # Only complete observed writeback releases this local source fence.
            if r.get('state') == 'saved_verified': continue
            old = r.get('plan', {}).get('allocations', [])
            if keys & {(a['orderId'], a['detailId']) for a in old}: raise ValueError('来源商品存在尚未完成回写核查的开票记录，请先核查历史结果')

    def check(self, scope, key):
        with LOCK:
            path, receipt = self._receipt(key, scope)
            if self.mock or not receipt.get('id'): return self._public_receipt(receipt)
            try:
                actual = self.client.call('invoiceDetail', query={'id': receipt['id']}).get('data') or {}
                expected = receipt['plan']['payload']['data']
                def identity(r):
                    return (text(r.get('sourceid')), text(r.get('sourceautoid')), text(r.get('productId')),
                            dec(r.get('qty'), '回读数量'), dec(r.get('oriSum'), '回读金额'), text(r.get('source')),
                            text(r.get('unitId')),text(r.get('chargeUnitId')),text(r.get('taxId')),
                            dec(r.get('oriTaxUnitPrice'),'回读单价'),dec(r.get('taxRate'),'回读税率',True))
                if text(actual.get('id')) != receipt['id'] or any(text(actual.get(k)) != text(expected.get(k)) for k in ('orgId','agentId','invAgentId','currency','einvoiceHm','einvoiceNo','transactionTypeId','bizFlow','invoiceType','invDirection')):
                    raise ValueError('回读发票抬头不一致')
                lines = actual.get('saleInvoiceDetails') or actual.get('voucher_saleinvoicedetails') or []
                if Counter(map(identity, lines)) != Counter(map(identity, expected['saleInvoiceDetails'])): raise ValueError('回读来源明细不一致')
                if text(actual.get('status'))!='0':
                    receipt.update(state='saved_unexpected_status',message='销售发票已保存，但单据状态不是开立；请在用友核查该业务流。程序未追加提交或审核，不要重复下推')
                    atomic(path,receipt)
                    return self._public_receipt(receipt)
                allocations = receipt['plan']['allocations']
                grouped = {}
                for a in allocations:
                    k = (a['orderId'], a['detailId'])
                    grouped.setdefault(k, [Decimal(0), dec(a['sourceRow']['invoiceQty'], '原开票量', True)])
                    grouped[k][0] += dec(a['quantity'], '分配数量')
                orders = {oid: self.client.call('detail', query={'id':oid})['data'] for oid in {k[0] for k in grouped}}
                updated = all(any(text(r.get('id')) == did and dec(r.get('invoiceQty'), '已开票量', True) == before+used
                                  for r in orders[oid].get('orderDetails', [])) for (oid,did),(used,before) in grouped.items())
                receipt.update(state='saved_verified' if updated else 'saved_pending_writeback',
                    message='销售发票与来源数量回读一致；未自动提交或审核' if updated else '销售发票已保存，订单开票数量尚未确认回写；请按用友业务流程处理后再核查')
            except Exception:
                receipt.update(state='saved_pending_check', message='已取得销售发票ID，回读未确认；仅可重新核查，不要再次保存')
            atomic(path, receipt)
            return self._public_receipt(receipt)

"""Reuse references from a scoped, approved historical order via existing APIs."""
import copy
import threading
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from order_resolver import norm, text


HEADER_REFERENCE_PAIRS = (
    ('customerName','agentId'),('salesOrgName','salesOrgId'),('transactionName','transactionTypeId'),
    ('departmentName','saleDepartmentId'),('salespersonName','corpContact'),
    ('invoiceCustomerName','invoiceAgentId'),('settlementOrgName','settlementOrgId'),
    ('stockOrgName','stockOrgId'),('currencyName','currencyId'),
    ('natCurrencyName','natCurrencyId'),('exchangeRateTypeName','exchangeRateType'),
)


class HeaderReferenceCache:
    """Per-Gateway memory only. Never store orders, rows or special authorizations."""
    def __init__(self, clock=time.time, ttl=600, maximum=32):
        self.clock,self.ttl,self.maximum=clock,ttl,maximum
        self.entries,self.instance,self.generation={},None,0
        self.lock=threading.RLock()

    def context(self, header, instance):
        if not isinstance(instance,str) or not instance or len(instance)>128:return None
        if not all(text(header.get(k)) for k in ('customerName','salesOrgName','transactionName')):return None
        values=tuple((norm(header.get(name)),text(header.get(ref))) for name,ref in HEADER_REFERENCE_PAIRS)
        if any(len(name)>300 or len(ref)>100 for name,ref in values):return None
        return instance,values

    def apply(self, header, instance, force=False):
        with self.lock:
            now=self.clock()
            if instance!=self.instance or force:
                self.entries.clear();self.instance=instance;self.generation+=1
            self.entries={k:v for k,v in self.entries.items() if 0<=now-v['created']<self.ttl}
            entry=self.entries.get(self.context(header,instance))
            if not entry:return copy.deepcopy(header),None,self.generation
            result=copy.deepcopy(header)
            for field,value in entry['refs'].items():
                if not text(result.get(field)):result[field]=value
            return result,{'source':'verified-header-cache','ageSeconds':int(now-entry['created']),
                           'fields':list(entry['refs'])},self.generation

    def remember(self, original, historical, resolved, instance, generation):
        key=self.context(original,instance)
        evidence=historical.get('_headerReuse') or {}
        if (not key or not evidence.get('eligible') or historical.get('issues') or historical.get('businessPatch')
                or resolved.get('ready') is not True or resolved.get('issues')):return
        verified=evidence.get('refs') or {}
        allowed={ref for _,ref in HEADER_REFERENCE_PAIRS}
        refs={field:text(value) for field,value in verified.items()
              if field in allowed and text(value) and not text(original.get(field))
              and text((resolved.get('header') or {}).get(field))==text(value)}
        if not refs:return
        with self.lock:
            # A concurrent connection change or forced refresh invalidates old work.
            if self.instance!=instance or generation!=self.generation:return
            if len(self.entries)>=self.maximum and key not in self.entries:
                oldest=min(self.entries,key=lambda k:self.entries[k]['created']);del self.entries[oldest]
            self.entries[key]={'created':self.clock(),'refs':refs}


def apply_default_tax(data):
    """Use the verified Guichang 13% sales reference, independent of order history.

    Publication placeholders; replace only after deployment-side verification.
    No real tenant identifiers are shipped. Never apply outside
    this organization/transaction. Keep explicit row choices and other rates.
    """
    result = copy.deepcopy(data)
    h = result.get('header') or {}
    scope = (('salesOrgName', 'salesOrgId', '示例集团有限公司', '9000000000000001'),
             ('transactionName', 'transactionTypeId', '普通销售（有发货）', '9000000000000002'))
    for name_key, id_key, expected_name, expected_id in scope:
        name, ref = text(h.get(name_key)), text(h.get(id_key))
        if not (name or ref): return result
        if name and norm(name) != norm(expected_name): return result
        if ref and ref != expected_id: return result
    for row in result.get('rows') or []:
        if row.get('taxId'): continue
        try:
            rate = Decimal(text(row.get('taxRate')) or '0.13')
        except InvalidOperation:
            continue  # The existing resolver reports invalid business values.
        if rate == Decimal('0.13'):
            row['taxId'] = '9000000000000003'
    return result


def _current_staff_from_candidates(transport, header, candidates, selected):
    """At most five additional details, using already-read scoped candidates."""
    wanted = text(header.get('salespersonName'))
    if not wanted or header.get('corpContact'): return None, []
    selected_id = text(selected.get('id'))
    seen, matches, warnings = {selected_id}, {}, []
    def staff_name(record): return text(record.get('corpContactUserName') or record.get('corpContact_name'))
    candidates = sorted(candidates, key=lambda record: norm(staff_name(record)) != norm(wanted))
    count = 0
    for position, candidate in enumerate(candidates):
        identity = text(candidate.get('id'))
        if not identity or identity in seen: continue
        seen.add(identity)
        if count >= 5:
            remaining = candidates[position:]
            if any(not staff_name(record) or norm(staff_name(record)) == norm(wanted) for record in remaining):
                return None, [{'field': 'salespersonName', 'code': 'history_staff_lookup_incomplete',
                               'message': '当前业务员的待核对历史候选超过本次5张上限，请选择明确档案'}]
            break
        count += 1
        try:
            response = transport('/api/order-query', {'id': identity})
            if not isinstance(response, dict) or str(response.get('code')) != '200': raise ValueError('历史详情查询未成功')
        except ValueError:
            # Permission/cooldown failure must not turn an incomplete search
            # into a successful reference or continue issuing more requests.
            return None, [{'field': 'salespersonName', 'code': 'history_staff_lookup_incomplete',
                           'message': '当前业务员的历史档案核对未完成，请稍后重试或选择明确档案'}]
        record = response.get('data') or {}
        if (not isinstance(record, dict) or text(record.get('id')) != identity or text(record.get('agentId')) != text(header.get('agentId')) or
                str(record.get('status')) != '1' or norm(record.get('salesOrgId_name')) != norm(header.get('salesOrgName')) or
                norm(record.get('transactionTypeId_name')) != norm(header.get('transactionName')) or
                (header.get('salesOrgId') and text(record.get('salesOrgId')) != text(header['salesOrgId'])) or
                (header.get('transactionTypeId') and text(record.get('transactionTypeId')) != text(header['transactionTypeId']))):
            continue
        if norm(staff_name(record)) == norm(wanted) and text(record.get('corpContact')):
            matches[text(record['corpContact'])] = {'orderId': identity, 'orderCode': text(record.get('code')),
                'salespersonName': wanted, 'corpContact': text(record['corpContact']),
                'basis': '同客户、销售组织、交易类型已审核历史订单中的当前业务员姓名', 'additionalDetailsRead': count}
    if len(matches) == 1:
        proof = next(iter(matches.values())); proof['additionalDetailsRead'] = count
        return proof, warnings
    if len(matches) > 1:
        warnings.append({'field': 'salespersonName', 'code': 'history_staff_ambiguous',
                         'message': '已核对历史订单中当前业务员姓名对应多个档案，请选择明确档案'})
    return None, warnings


def reuse_history(transport, data, choices=None, today=None):
    h = copy.deepcopy(data.get('header') or {})
    rows = copy.deepcopy(data.get('rows') or [])
    if not 1 <= len(rows) <= 200: raise ValueError('需要1至200项商品')
    customer = text(h.get('customerName'))
    if not customer: raise ValueError('请先填写客户名称')
    result = transport('/api/references', {'kind': 'customer', 'query': customer, 'pageIndex': 1})
    matches = {text(r['id']): r for r in result.get('candidates', []) if norm(r.get('name')) == norm(customer) and r.get('selectable') is not False}
    if len(matches) != 1 or result.get('hasMore'): raise ValueError('客户没有唯一完整名称匹配，请先核实客户档案')
    cid = next(iter(matches))
    if h.get('agentId') and text(h['agentId']) != cid: raise ValueError('客户名称与当前客户档案不一致')
    h['agentId'] = cid
    history = None
    history_candidates = []
    # Bounded to recent 14 days; first page only. Never query other customers.
    for offset in range(14):
        day = ((today or date.today()) - timedelta(days=offset)).isoformat()
        response = transport('/api/order-query', {'date': day, 'customerId': cid, 'pageIndex': 1})
        if str(response.get('code')) != '200': raise ValueError('历史订单查询未成功')
        candidates = (response.get('data') or {}).get('recordList') or []
        candidates = [r for r in candidates if text(r.get('agentId')) == cid and str(r.get('status')) == '1'
                      and norm(r.get('salesOrgId_name')) == norm(h.get('salesOrgName'))
                      and norm(r.get('transactionTypeId_name')) == norm(h.get('transactionName'))]
        if not candidates: continue
        selected = sorted(candidates, key=lambda r: text(r.get('code')), reverse=True)[0]
        response = transport('/api/order-query', {'id': text(selected['id'])})
        if str(response.get('code')) != '200': raise ValueError('历史订单详情查询未成功')
        history = response.get('data') or {}
        if text(history.get('id')) != text(selected['id']) or text(history.get('agentId')) != cid or str(history.get('status')) != '1':
            raise ValueError('历史订单详情与所选客户不一致')
        if norm(history.get('salesOrgId_name')) != norm(h.get('salesOrgName')) or norm(history.get('transactionTypeId_name')) != norm(h.get('transactionName')):
            raise ValueError('历史订单组织或交易类型不一致')
        if any(h.get(field) and text(h[field]) != text(history.get(field)) for field in ('salesOrgId', 'transactionTypeId')):
            raise ValueError('历史订单组织或交易类型档案与当前已选档案不一致')
        history_candidates = candidates
        break
    if not history: raise ValueError('最近14天内未找到同客户、同销售组织及交易类型的已审核订单')
    issues, warnings, business_patch = [], [], {}
    verified_refs={'agentId':cid}
    p = history.get('orderPrices') or {}
    lines = history.get('orderDetails') or []

    def fill(namekey, refkey, oldname, oldid, label, allow_blank_name=False):
        if text(h.get(refkey)):
            if text(h[refkey])==text(oldid) and (not text(h.get(namekey)) or norm(h[namekey])==norm(oldname)):
                verified_refs[refkey]=text(oldid)
            return
        name = text(h.get(namekey))
        if not text(oldid):
            if name:
                warnings.append({'field': namekey, 'code': 'history_reference_missing_current_lookup',
                                 'message': f'历史订单未提供{label}对应关系，按本单“{name}”查询当前档案'})
            else:
                issues.append({'field': namekey, 'message': f'历史订单未提供{label}对应关系'})
            return
        if (not name and allow_blank_name) or (name and norm(name) == norm(oldname)):
            h[refkey] = text(oldid)
            verified_refs[refkey]=text(oldid)
        elif name:
            warnings.append({'field': namekey, 'code': 'history_name_differs_current_lookup',
                             'message': f'{label}“{name}”与历史订单“{text(oldname)}”不同，按本单名称查询当前档案'})

    for namekey, refkey, label in [('salesOrgName','salesOrgId','销售组织'),('transactionName','transactionTypeId','交易类型'),('departmentName','saleDepartmentId','销售部门'),('invoiceCustomerName','invoiceAgentId','开票客户'),('settlementOrgName','settlementOrgId','开票组织')]:
        fill(namekey, refkey, history.get(refkey+'_name'), history.get(refkey), label, namekey in ('invoiceCustomerName','settlementOrgName'))
    stocks = {(text(r.get('stockOrgId')), text(r.get('stockOrgId_name'))) for r in lines if r.get('stockOrgId')}
    if len(stocks) == 1:
        sid, sname = next(iter(stocks)); fill('stockOrgName', 'stockOrgId', sname, sid, '库存组织', True)
    elif not h.get('stockOrgId'): issues.append({'field':'stockOrgName','message':'历史订单涉及多个库存组织，请选择本单库存组织'})
    fill('currencyName','currencyId',p.get('originalName'),p.get('currency'),'币种')
    fill('natCurrencyName','natCurrencyId',p.get('domesticName'),p.get('natCurrency'),'本币')
    fill('exchangeRateTypeName','exchangeRateType',p.get('exchangeRateType_name'),p.get('exchangeRateType'),'汇率类型')
    staff_name, staff_id = text(history.get('corpContactUserName') or history.get('corpContact_name')), text(history.get('corpContact'))
    choice = (choices or {}).get(text(data.get('sourceId'))) or {}
    if (text(h.get('salespersonName')) == choice.get('fromName') and staff_name == choice.get('toName')
            and staff_id == choice.get('toId') and text(history.get('id')) == choice.get('orderId')
            and not h.get('corpContact')):
        business_patch['salespersonName'] = staff_name
        h.update(salespersonName=staff_name, corpContact=staff_id, reviewed=False)
    elif h.get('salespersonName'):
        fill('salespersonName','corpContact',staff_name,staff_id,'销售业务员')
    staff_fallback = None
    if h.get('salespersonName') and not h.get('corpContact'):
        staff_fallback, extra_warnings = _current_staff_from_candidates(transport, h, history_candidates, history)
        warnings.extend(extra_warnings)
        if staff_fallback:
            h['corpContact'] = staff_fallback['corpContact']
            verified_refs['corpContact'] = h['corpContact']
    # Historical lines supply stock-organization references only. Tax selection
    # is a stable order default, not a vote among historical product tax items.
    rows = apply_default_tax({'header': h, 'rows': rows})['rows']
    evidence = {'orderId': text(history.get('id')), 'orderCode': text(history.get('code')), 'customerId': cid,
                'customerName': customer, 'basis':'同客户、销售组织、交易类型的已审核历史订单；仅复用档案引用',
                'businessPatch': business_patch}
    eligible=not business_patch and all(not text(h.get(ref)) or verified_refs.get(ref)==text(h[ref]) for _,ref in HEADER_REFERENCE_PAIRS)
    return {'header': h, 'rows': rows, 'issues': issues, 'warnings': warnings, 'history': evidence, 'businessPatch': business_patch,
            'historyStaffFallback': staff_fallback, 'readOnly': True,
            '_headerReuse':{'eligible':eligible,'refs':verified_refs}}

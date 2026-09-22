"""Read-only, customer-scoped invoice configuration discovery and revalidation.

History proves a configuration was used; it does not certify tenant workflow
side effects. Saving still requires the invoice-specific human confirmation.
"""
import hashlib
import json
import re
from datetime import date, timedelta

from core import browser_safe
from order_resolver import records


def text(value):
    return str(value if value is not None else '').strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(browser_safe(value),ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def identifier(value):
    return bool(re.fullmatch(r'\d{1,20}',text(value)))


def source_signature(order):
    return tuple(text(order.get(k)) for k in ('salesOrgId','settlementOrgId','agentId','invoiceAgentId','transactionTypeId','bizFlow'))


def context(draft, allocations):
    first=allocations[0]['sourceOrder']
    signature=source_signature(first)
    if not all(signature) or not all(identifier(x) for x in signature[:5]):
        raise ValueError('来源订单缺少组织、客户、交易类型或业务流，无法可靠匹配开票流程')
    if any(source_signature(a['sourceOrder'])!=signature for a in allocations):
        raise ValueError('本次来源订单的交易类型或业务流不同，请分开核对开票流程')
    return first, signature


def transaction_map(client):
    items=records(client.call('transactions',{'codes':['voucher_saleinvoice']}))
    result={}
    for row in items:
        if not identifier(row.get('id')):continue
        identity=text(row['id'])
        if identity in result and result[identity]!=row:raise ValueError('销售发票交易类型档案存在冲突')
        result[identity]=row
    if not result:raise ValueError('没有查到启用的销售发票交易类型，请检查交易类型查询权限及用友档案')
    return result


def inspect_invoice(client, identity, draft, allocations, transactions, order_cache):
    first, signature=context(draft,allocations)
    invoice=client.call('invoiceDetail',query={'id':identity}).get('data')
    if not isinstance(invoice,dict) or text(invoice.get('id'))!=identity:
        raise ValueError('历史销售发票详情身份不一致')
    # Every customer/org/type constraint is checked again on the detail.
    expected={'orgId':first['settlementOrgId'],'agentId':draft['header']['customerId'],
              'invAgentId':first['invoiceAgentId'],'currency':(first.get('orderPrices') or {}).get('currency'),
              'invoiceType':draft['header']['invoiceType']}
    if any(text(invoice.get(k))!=text(v) for k,v in expected.items()):return None
    if (text(invoice.get('source'))!='voucher_order' or text(invoice.get('invDirection'))!='2'
            or text(invoice.get('status')) not in ('0','1')
            or text(invoice.get('businessState')) not in ('0','1')
            or text(invoice.get('taxBillingStatus'))!='2'):return None
    transaction=text(invoice.get('transactionTypeId')); flow=text(invoice.get('bizFlow'))
    if transaction not in transactions or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',flow):return None
    lines=invoice.get('saleInvoiceDetails')
    if not isinstance(lines,list) or not 1<=len(lines)<=200:return None
    source_refs={}
    for line in lines:
        if line.get('source')!='voucher_order' or not identifier(line.get('sourceid')) or not identifier(line.get('sourceautoid')):return None
        oid=text(line['sourceid']);did=text(line['sourceautoid'])
        if oid not in order_cache:
            if len(order_cache)>=200:raise ValueError('历史来源订单核查超过200张，请填写一张已完成的销售发票单号缩小范围')
            value=client.call('detail',query={'id':oid}).get('data')
            if not isinstance(value,dict) or text(value.get('id'))!=oid:raise ValueError('历史发票来源订单身份不一致')
            order_cache[oid]=value
        order=order_cache[oid]
        if source_signature(order)!=signature:return None
        source_line=next((r for r in order.get('orderDetails',[]) if text(r.get('id'))==did),None)
        if source_line is None or text(source_line.get('productId'))!=text(line.get('productId')):return None
        source_refs[oid]=source_signature(order)
    config={'salesOrgId':signature[0],'settlementOrgId':signature[1],
            'transactionTypeId':transaction,'bizFlow':flow,'bizFlowVersion':text(invoice.get('bizFlow_version')),
            'transactionName':text(transactions[transaction].get('name')) or text(invoice.get('transactionTypeId_name')),
            'flowName':text(invoice.get('bizFlow_name'))}
    if not config['transactionName'] or not config['flowName']:return None
    return {'key':fingerprint(config),'config':config,'referenceId':identity,
            'referenceCode':text(invoice.get('code')),'referenceDate':text(invoice.get('vouchdate'))[:10],
            'evidenceHash':fingerprint({'invoice':invoice,'sourceRefs':source_refs,'transaction':transactions[transaction]})}


def discover(client,draft,allocations,reference_code=''):
    first,_=context(draft,allocations)
    transactions=transaction_map(client)
    reference_code=text(reference_code)
    if len(reference_code)>80:raise ValueError('参考销售发票单号过长')
    filters=[{'field':k,'op':'eq','value1':text(v)} for k,v in (
        ('orgId',first['settlementOrgId']),('agentId',draft['header']['customerId']),
        ('invAgentId',first['invoiceAgentId']),('source','voucher_order'))]
    # Full bounded scan of the stated period. Never claim uniqueness from a sample.
    base={'simpleVOs':filters,'invoiceType':int(draft['header']['invoiceType']),'taxBillingStatus':2,'isSum':True}
    if reference_code:base['code']=reference_code
    else:
        base['open_vouchdate_begin']=(date.today()-timedelta(days=366)).isoformat()+' 00:00:00'
        base['open_vouchdate_end']=date.today().isoformat()+' 23:59:59'
    ids=[];expected=None
    for page in range(1,5):
        value=client.call('invoiceList',{**base,'pageIndex':page,'pageSize':50}).get('data')
        if not isinstance(value,dict) or not isinstance(value.get('recordList'),list) or not text(value.get('recordCount')).isdigit():
            raise ValueError('历史销售发票列表不完整，不能自动判断配置')
        count=int(value['recordCount'])
        if expected is None:expected=count
        if count!=expected or count>200:raise ValueError('历史开票记录超过200张或查询期间变化，请填写一张已完成的销售发票单号缩小范围')
        for row in value['recordList']:
            identity=text(row.get('id'))
            if not identifier(identity) or identity in ids:raise ValueError('历史销售发票分页重复或身份无效')
            if text(row.get('orgId'))!=text(first['settlementOrgId']):raise ValueError('历史销售发票组织筛选未生效')
            if reference_code and text(row.get('code'))!=reference_code:continue
            ids.append(identity)
        if reference_code and count!=len(ids):raise ValueError('参考销售发票单号未精确匹配')
        if len(ids)==count:break
        if not value['recordList'] or len(ids)>count:raise ValueError('历史销售发票分页缺失')
    if len(ids)!=expected:raise ValueError('历史销售发票未查完，不能自动选择配置')
    cache={text(a['sourceOrder']['id']):a['sourceOrder'] for a in allocations};groups={}
    for identity in ids:
        candidate=inspect_invoice(client,identity,draft,allocations,transactions,cache)
        if candidate is None:continue
        old=groups.get(candidate['key'])
        if old is None or (candidate['referenceDate'],candidate['referenceId'])>(old['referenceDate'],old['referenceId']):groups[candidate['key']]=candidate
    return sorted(groups.values(),key=lambda c:(c['config']['transactionName'],c['config']['flowName'],c['key']))


def revalidate(client,candidate,draft,allocations):
    transactions=transaction_map(client)
    cache={text(a['sourceOrder']['id']):a['sourceOrder'] for a in allocations}
    current=inspect_invoice(client,candidate['referenceId'],draft,allocations,transactions,cache)
    if current is None or current['key']!=candidate['key'] or current['evidenceHash']!=candidate['evidenceHash']:
        raise ValueError('参考发票或开票配置已变化，请重新匹配并人工确认')


def public(candidate):
    return {'key':candidate['key'], 'transactionName':candidate['config']['transactionName'],
            'flowName':candidate['config']['flowName'],'flowVersion':candidate['config']['bizFlowVersion'],
            'referenceCode':candidate['referenceCode'],'referenceDate':candidate['referenceDate']}

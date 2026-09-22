"""Pure reference-price handoff example. Never creates orders or replaces existing prices."""
from copy import deepcopy

def consume(order_lines,payload):
    if payload.get('contract_version')!=2:raise ValueError('需要 v2 交接契约')
    output=deepcopy(order_lines)
    ids=[r['source_line_id'] for r in output]
    if len(ids)!=len(set(ids)):raise ValueError('消费端来源行ID重复')
    byid={r['source_line_id']:r for r in output}
    seen=set()
    for price in payload['rows']:
        key=price['source_line_id']
        if key in seen:raise ValueError('交接行ID重复')
        seen.add(key)
        if key not in byid:raise ValueError('来源行不属于当前订单，禁止按名称猜配')
        line=byid[key]
        if any(line.get(k)!=price.get(k) for k in ('source_batch_id','input_fingerprint','name','spec','unit','quantity')):
            raise ValueError('原行内容/单位变化，须重新匹配确认')
        # Existing order price is never replaced. A repeated payload is idempotent.
        if line.get('price') is not None or price['protected']:
            line['handoff_status']='保留原价';continue
        token=[key,price['calculation_batch_id']]
        if not price['can_fill_price']:
            line['reference_price']=None;line['reference_token']=None;line.pop('reference_evidence',None);line['handoff_status']='待核';continue
        line['reference_price']=price['price_decimal']
        line['reference_token']=token
        line['reference_evidence']=deepcopy(price)
        line['handoff_status']='参考价待业务确认'
    return output

"""Local order logic. No upstream writes or inferred unit conversions."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
import hashlib, json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ['GC_FOOD_SOURCE']) if os.environ.get('GC_FOOD_SOURCE') else None
def now(): return datetime.now().astimezone().isoformat(timespec='seconds')
def number(value, label, optional=True):
    if value is None or value == '':
        if optional: return None
        raise ValueError(f'{label}不能为空')
    if isinstance(value, bool): raise ValueError(f'{label}必须为数字')
    try: n = Decimal(str(value))
    except InvalidOperation: raise ValueError(f'{label}必须为数字')
    if not n.is_finite() or n < 0 or n > 100000000: raise ValueError(f'{label}需在0至1亿之间')
    if n.as_tuple().exponent < -4: raise ValueError(f'{label}最多4位小数')
    return str(n)
def amount(q,p):
    if q is None or p is None: return None
    return format((Decimal(q)*Decimal(p)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),'.2f')
def seed(source=None):
    source=SOURCE or source
    if not source or not source.is_file():
        return dict(id='food40',order_no='',customer='',subject='',revision=1,stage='draft',statement_confirmed_at=None,created_at=now(),updated_at=now(),rows=[],source=dict(kind='待载入原单',path='',sha256='',note='尚未配置历史原单，请由实施人员指定已授权样本。'),log=[])
    raw = source.read_bytes(); src=json.loads(raw)
    rows=[]
    for r in src['rows']:
        rows.append(dict(id=str(r[0]),raw_name=r[1],ordered_qty=str(r[2]),unit=r[3],original_note=r[4],
          historical_name=r[7],historical_code=r[6],historical_qty=str(r[8]),historical_unit=r[9],historical_note=r[10],
          name=r[7],code=r[6],note=r[4] or r[10],confirmed=False,supplier='',purchase_no='',
          purchase_qty=None,purchase_price=None,warehouse_qty=None,delivered_qty=None,received_qty=None,
          provisional_price='0.01',final_price=None,price_note='',difference_note=''))
    return dict(id='food40',order_no=src['order'],customer=src['customer'],subject='',revision=1,
      stage='draft',statement_confirmed_at=None,created_at=now(),updated_at=now(),rows=rows,
      source=dict(kind='贵昌历史原单与保存单整理',path=source.name,sha256=hashlib.sha256(raw).hexdigest(),
        note=src['evidence']+'；本Demo的后续采购、签收和最终价格等待人工录入，不代表历史事实。'),
      log=[dict(time=now(),message='载入历史40项；未补量价，未向外部系统写入')])
EDITABLE = {'name','code','note','confirmed','supplier','purchase_no','purchase_qty','purchase_price','warehouse_qty','delivered_qty','received_qty','provisional_price','final_price','price_note','difference_note'}
NUMERIC = {'purchase_qty','purchase_price','warehouse_qty','delivered_qty','received_qty','provisional_price','final_price'}
def validate(old, incoming):
    if incoming.get('revision') != old['revision']: raise RuntimeError('该订单已在另一页面更新，请刷新后再修改')
    if not isinstance(incoming.get('rows'),list) or len(incoming['rows']) != len(old['rows']): raise ValueError('订单行数不一致')
    fresh=json.loads(json.dumps(old)); lookup={r['id']:r for r in incoming['rows']}
    if len(lookup)!=len(old['rows']) or set(lookup)!={r['id'] for r in old['rows']}:raise ValueError('订单行标识不一致')
    for r in fresh['rows']:
        v=lookup[r['id']]
        for k in EDITABLE:
            if k not in v: continue
            if k in NUMERIC: r[k]=number(v[k],r['raw_name']+' '+k)
            elif k=='confirmed':
                if not isinstance(v[k],bool):raise ValueError('商品确认值无效')
                r[k]=v[k]
            else:r[k]=str(v[k] or '').strip()[:500]
        previous=next(item for item in old['rows'] if item['id']==r['id'])
        if any(r.get(k)!=previous.get(k) for k in ('name','code','unit')):
            r.update(confirmed=False,final_price=None,price_note='',purchase_price=None)
            fresh['stage']='draft'
        if r['confirmed'] and not r['name']:raise ValueError('请补确认商品名称')
        if r['delivered_qty'] is not None and r['warehouse_qty'] is not None and Decimal(r['delivered_qty']) > Decimal(r['warehouse_qty']):raise ValueError(r['raw_name']+' 发出量不能超过仓库实收量')
        if r['received_qty'] is not None and r['delivered_qty'] is not None and Decimal(r['received_qty'])>Decimal(r['delivered_qty']):raise ValueError(r['raw_name']+' 客户实收不能超过发出量')
    fresh['subject']=str(incoming.get('subject',old['subject']) or '').strip()[:150]
    changed = fresh['subject'] != old['subject'] or fresh['rows'] != old['rows']
    if changed:
        fresh['statement_confirmed_at']=None
        fresh['log'].append(dict(time=now(),message='保存量价/商品/主体修改；对账按最新值重算'))
    fresh['updated_at']=now();fresh['revision']+=1
    return fresh

def summary(order):
    rows=order['rows']; missing=[]; sale=Decimal(0); buy=Decimal(0); priced=0; bought=0
    for r in rows:
        a=amount(r['received_qty'],r['final_price']); b=amount(r['warehouse_qty'],r['purchase_price'])
        if a is not None:sale+=Decimal(a);priced+=1
        if b is not None:buy+=Decimal(b);bought+=1
        if not r['confirmed']:missing.append(f"第{r['id']}行：确认商品")
        for k,label in [('warehouse_qty','仓库实收'),('delivered_qty','发出量'),('received_qty','客户实收'),('final_price','最终销售价')]:
            if r[k] is None:missing.append(f"第{r['id']}行：{label}")
        if r['final_price'] is not None and not r['price_note']:missing.append(f"第{r['id']}行：定价依据")
        if r['received_qty'] is not None and Decimal(r['received_qty'])!=Decimal(r['ordered_qty']) and not r['difference_note']:missing.append(f"第{r['id']}行：数量差异说明")
    if not order['subject']:missing.insert(0,'请选择/填写本笔核算主体')
    return dict(sale_total=format(sale,'.2f'),purchase_total=format(buy,'.2f'),priced_rows=priced,purchased_rows=bought,
      total_rows=len(rows),missing=missing,ready=not missing,
      row_amounts={r['id']:amount(r['received_qty'],r['final_price']) for r in rows},
      note='金额按每行四舍五入至分后相加；未知量价不计0，暂价不进入最终金额。')

def transition(order,action):
    rows=order['rows']; s=summary(order)
    if action != 'confirm_statement':order['statement_confirmed_at']=None
    if action=='confirm_order':
        if not order['subject'] or not all(r['confirmed'] for r in rows):raise ValueError('请先填写主体并确认每项商品')
        order['stage']='confirmed';msg='本地确认订单，原数量与加工要求保留'
    elif action=='confirm_purchase':
        if order['stage']=='draft':raise ValueError('请先确认订单')
        if any(r['purchase_qty'] is None or r['warehouse_qty'] is None or not r['supplier'] for r in rows):raise ValueError('请补每行采购量、仓库实收和供应商')
        order['stage']='received';msg='本地确认采购与仓库实收'
    elif action=='confirm_delivery':
        if order['stage'] not in ['received','delivered','statement']:raise ValueError('请先确认采购收货')
        if any(r['delivered_qty'] is None or r['received_qty'] is None for r in rows):raise ValueError('请补每行发出量与客户实收')
        order['stage']='delivered';msg='本地确认发出与客户实收'
    elif action=='confirm_statement':
        if not s['ready']:raise ValueError('对账仍缺：'+'；'.join(s['missing'][:4]))
        if order['stage'] not in ['delivered','statement']:raise ValueError('请先完成采购与签收确认')
        order['stage']='statement';order['statement_confirmed_at']=now();msg='本地确认对账；不是付款、开票或外部结算'
    else:raise ValueError('未知操作')
    order['log'].append(dict(time=now(),message=msg));order['revision']+=1;order['updated_at']=now()
    return order

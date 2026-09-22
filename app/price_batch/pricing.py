import json, threading, uuid
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
STORE=Path(__file__).with_name('projects.json')
LOCK=threading.RLock()

def number(value):
    try: x=Decimal(str(value))
    except Exception: raise ValueError('请输入有效数值')
    if not x.is_finite() or abs(x)>Decimal('1000000000'): raise ValueError('数值超出范围')
    return x

def validate(d):
    r={k:d[k] for k in ('name','purpose','basis','adjustment_kind','adjustment','fee','fee_order','rounding','fixed_price')}
    r['name']=str(r['name']).strip()
    if not r['name'] or len(r['name'])>100: raise ValueError('请填写项目名称（最多100字）')
    for k, choices in {'purpose':['sales','purchase'],'basis':['low_price','average_price','high_price','fixed'],'adjustment_kind':['rate','coefficient'],'fee_order':['before','after'],'rounding':['ROUND','ROUNDDOWN','none']}.items():
        if r[k] not in choices: raise ValueError('无效规则：'+k)
    for k in ('adjustment','fee','fixed_price'):r[k]=str(number(r[k]))
    if number(r['fixed_price'])<0:raise ValueError('固定价不能为负')
    multiplier=number(r['adjustment'])+(1 if r['adjustment_kind']=='rate' else 0)
    if multiplier<0:raise ValueError('浮动后系数不能为负')
    return r

def projects():
    with LOCK:return json.loads(STORE.read_text()) if STORE.exists() else {}

def save(d):
    r=validate(d)
    with LOCK:
        all=projects(); key=d.get('id') or uuid.uuid4().hex
        if d.get('id') and key not in all:raise ValueError('项目不存在，请新建')
        r.update(id=key,version=all.get(key,{}).get('version',0)+1)
        all[key]=r; temp=STORE.with_suffix('.tmp'); temp.write_text(json.dumps(all,ensure_ascii=False,indent=2));temp.replace(STORE)
    return r

def calculate(base,ratio,rule,fee=None,adjustment=None):
    r=validate(rule); r.update({k:rule[k] for k in ('id','version') if k in rule}); b=number(base); f=number(r['fee'] if fee is None else fee); a=number(r['adjustment'] if adjustment is None else adjustment)
    ratio=number(ratio)
    if ratio<=0 or ratio>1000000:raise ValueError('换算比例须为有效正数')
    m=a+(1 if r['adjustment_kind']=='rate' else 0)
    if b<0 or m<0:raise ValueError('基准价或浮动系数不能为负')
    converted=b*ratio
    value=(converted+f)*m if r['fee_order']=='before' else converted*m+f
    if value<0:raise ValueError('最终价不能为负')
    if r['rounding']!='none':value=value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP if r['rounding']=='ROUND' else ROUND_DOWN)
    expression=f'({b} × {ratio} + {f}) × {m}' if r['fee_order']=='before' else f'{b} × {ratio} × {m} + {f}'
    return {'price':float(value),'price_decimal':str(value),'base_price':str(b),'factor':str(ratio),'fee':str(f),'adjustment':str(a),'formula':expression+'；'+r['rounding'],'rule':r}

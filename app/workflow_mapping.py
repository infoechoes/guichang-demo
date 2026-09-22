"""Confirmed order worksheets provide scoped mapping candidates, never invoice writes."""
import json
from decimal import Decimal, ROUND_HALF_UP
from workflow_excel import tables_excel

def text(v): return str(v if v is not None else '').strip()
def scope(header):
    return tuple(text(header.get(k)) for k in ('agentId','invoiceAgentId','salesOrgId','knowledgeBaseId'))
def source_key(row):
    return tuple(text(row.get(k)) for k in ('originalCode','sourceName','sourceSpec','sourceDemandUnit'))
def original_row(row, original):
    return {**row,'originalCode':original.get('code') or original.get('sourceCode') or row.get('originalCode',''),
            'sourceName':original.get('name',''),'sourceSpec':original.get('spec',''),
            'sourceDemandUnit':original.get('unit') or row.get('sourceDemandUnit','')}
def confirmed_mapping(review, batch):
    inp=review['input']
    if inp.get('mode')!='live' or inp.get('header',{}).get('reviewed') is not True:return None
    originals={str(r.get('id') or i+1):r for i,r in enumerate(batch['snapshot']['rows'])}
    rows=[]
    for r in inp['rows']:
        if not all(text(r.get(k)) for k in ('code','productId','unit','unitId')):continue
        row=original_row(r,originals.get(str(r.get('sourceLineId')),{}))
        if not row['originalCode'] or not row['sourceDemandUnit']:continue
        if row['sourceDemandUnit']!=text(row.get('unit')):
            # A conversion must already have passed the gateway validator.
            if not row.get('unitConversion'):continue
        rows.append(row)
    return {'schema':'confirmed-order-mapping-v1','batchId':batch['batchId'],'planId':review['planId'],
            'inputHash':review['inputHash'],'fileHash':batch['fileHash'],'header':inp['header'],'rows':rows}
def lookup(store, data):
    header=data.get('header') or {}; row=data.get('row') or {}
    key=source_key(row); customer=scope(header)
    if not all(customer[:3]) or not key[0] or not key[3]:return {'candidates':[], 'message':'请先选择客户、开票客户及销售组织，并保留客户商品编码和原单位；不按同名自动复用。'}
    candidates=[]
    for path in sorted((store.root/'batches').glob('*/batch.json'), key=lambda p:p.stat().st_mtime,reverse=True):
        record=store.get(path.parent.name); mapping=record.get('confirmedMapping')
        if not mapping or not record.get('final') or record.get('draftRetiredAt') or scope(mapping['header'])!=customer:continue
        # Confirmed data is tied to the immutable final archive, not a mutable draft.
        import hashlib
        final=record['final']; f=store.folder(record['batchId'])/('final-'+hashlib.sha256(final['planId'].encode()).hexdigest()+'.xlsx')
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=final.get('fileHash'):continue
        for item in mapping['rows']:
            if source_key(item)==key:
                candidates.append({'batchId':mapping['batchId'],'sourceLineId':item.get('sourceLineId'),
                    'planId':mapping['planId'],'row':item,'downloadUrl':final['downloadUrl']})
    return {'candidates':candidates,'message':'只列客户、组织、客户商品编码、原名称、规格和单位全部一致的已确认底稿；冲突请人工选择。'}
def apply(store,data):
    matches=lookup(store,data)['candidates']
    selected=next((c for c in matches if c['batchId']==data.get('mappingBatchId') and c['sourceLineId']==data.get('mappingLineId')),None)
    if selected is None:raise ValueError('映射已变化或业务条件不匹配，请重新查询')
    current=data['row']; previous=selected['row']
    result={**current,**{k:previous.get(k,'') for k in ('name','code','spec','unit','productId','unitId','taxId')},
            'sameUnitConfirmed':False,'catalogSource':None,'defaultPrice':None,'price':'','priceSource':'',
            'priceNeedsReview':True,'mappingReference':{k:selected[k] for k in ('batchId','sourceLineId','planId')}}
    result.pop('unitConversion',None)
    conversion=previous.get('unitConversion')
    if conversion:
        # Reuse only the confirmed ratio; calculate this order's quantity/price afresh.
        q=text(current.get('sourceDemandQuantity')); price=text(current.get('sourcePrice'))
        if not q or not price:raise ValueError('单位换算需要本单原数量和原单价格，请补齐后复用')
        ratio=Decimal(conversion['ratio']); quantity=Decimal(q); original_price=Decimal(price)
        if not all(v.is_finite() and v>0 for v in (ratio,quantity,original_price)):raise ValueError('换算数值无效')
        result['quantity']=format((quantity*ratio).quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP),'f')
        result['price']=format((original_price/ratio).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP),'f')
        result['priceSource']='人工确认复用底稿换算；本单原价'
        result['unitConversion']={**conversion,'fromQuantity':q,'fromPrice':price,'toQuantity':result['quantity'],'toPrice':result['price']}
        import sys
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'yonyou'))
        from order_resolver import manual_unit_conversion
        manual_unit_conversion(result)
        result['priceNeedsReview']=False
    else:
        result['quantity']=current.get('sourceDemandQuantity') or current.get('quantity','')
    return {'row':result}
def mapping_table(rows):
    fields=[('sourceLineId','来源行'),('originalCode','客户商品编码'),('sourceName','客户商品名称'),('sourceSpec','客户规格'),('sourceDemandUnit','原单位'),('code','用友商品编码'),('productId','用友商品ID'),('name','用友商品名称'),('spec','用友规格'),('unit','用友单位'),('unitId','用友单位ID')]
    return [[label for key,label in fields]+['已确认换算记录']]+[[r.get(k,'') for k,label in fields]+[json.dumps(r.get('unitConversion') or {},ensure_ascii=False)] for r in rows]
def draft_excel(record):
    draft=record.get('draft')
    if not draft:raise ValueError('请先保存当前草稿')
    originals={str(r.get('id') or i+1):r for i,r in enumerate(record['snapshot']['rows'])}
    rows=[original_row(r,originals.get(str(r.get('sourceLineId')),{})) for r in draft['rows']]
    fields=[('sourceLineId','来源行'),('sourceName','原名称'),('sourceSpec','原规格'),('originalCode','客户商品编码'),('name','确认商品'),('code','用友编码'),('quantity','数量'),('unit','单位'),('sourcePrice','原单价格'),('price','成交价'),('priceSource','价格依据'),('sourceFile','来源文件'),('sourceSheet','来源表'),('sourceRow','原表行'),('priceEvidenceText','查价规则与来源快照')]
    for r in rows:r['priceEvidenceText']=json.dumps(r.get('priceEvidence') or {},ensure_ascii=False)
    info=[['项目','内容'],['状态','可维护底稿；尚非确认版本或用友保存回执'],['批次',record['batchId']],['原件SHA256',record['fileHash']],['草稿SHA256',record['draftHash']]]+[[k,v] for k,v in draft['header'].items()]
    return tables_excel([('业务录单',[[v for k,v in fields]]+[[r.get(k,'') for k,v in fields] for r in rows]),('客户商品映射待复核',mapping_table(rows)),('来源与状态',info)])

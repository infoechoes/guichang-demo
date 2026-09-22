"""Bounded read-only inventory facade. Preserve stock dimensions; never reserve stock."""
import re
import time
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation


def text(value):
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''


def quantity(value):
    if value is None or isinstance(value, bool): return None
    try:
        number=Decimal(str(value))
        if not number.is_finite() or abs(number)>Decimal('1e15'): return None
        return format(number,'f')
    except (InvalidOperation, ValueError): return None


def query_inventory(transport, data, clock=time.time):
    if not isinstance(data,dict) or set(data)-{'productCode','org','warehouseCode'}:
        raise ValueError('库存查询仅支持商品编码、组织及仓库条件')
    code=data.get('productCode')
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',code):
        raise ValueError('请先匹配一个有效商品编码，不能查询全量库存')
    request={'productCode':code}
    for key, pattern in [('org',r'[0-9]{1,20}'),('warehouseCode',r'[A-Za-z0-9_-]{1,80}')]:
        value=data.get(key)
        if value in (None,''): continue
        if not isinstance(value,str) or not re.fullmatch(pattern,value): raise ValueError('库存查询条件无效：'+key)
        request[key]=value
    raw=transport('/api/stock',request)
    if not isinstance(raw,dict) or str(raw.get('code'))!='200' or 'data' not in raw or (raw['data'] is not None and not isinstance(raw['data'],list)):
        raise ValueError('库存接口未返回有效数据；不能将查询失败视为零库存')
    # The live endpoint returns an explicit null on a successful empty lookup.
    # Preserve that distinction without manufacturing a zero-quantity record.
    raw_rows=raw['data'] if raw['data'] is not None else []
    if len(raw_rows)>1000: raise ValueError('库存记录过多，请限定组织或仓库后查询')
    records=[]
    fields={
        'recordId':'id','productCode':'product_code','productId':'product','productName':'product_name',
        'model':'product_model','skuId':'productsku','organizationId':'org','organizationName':'org_name',
        'organizationCode':'org_code','warehouseId':'warehouse','warehouseCode':'warehouse_code',
        'warehouseName':'warehouse_name','unitId':'unit','unitName':'product_unitName','unitCode':'product_unitCode',
        'stockStatusId':'stockStatusDoc','stockStatusName':'stockStatusDoc_statusName',
        'inventoryOwner':'inventoryowner','custodian':'custodian','custodianType':'custodianType',
        'reserveId':'reserveid','batchNo':'batchno'}
    for row in raw_rows:
        if not isinstance(row,dict) or text(row.get('product_code'))!=code:
            raise ValueError('库存响应商品编码与查询不符，已停止展示')
        if request.get('org') and text(row.get('org'))!=request['org']:
            raise ValueError('库存响应组织与查询不符，已停止展示')
        if request.get('warehouseCode') and text(row.get('warehouse_code'))!=request['warehouseCode']:
            raise ValueError('库存响应仓库与查询不符，已停止展示')
        item={out:text(row.get(source)) for out,source in fields.items()}
        item.update(currentQty=quantity(row.get('currentqty')),availableQty=quantity(row.get('availableqty')),
                    plannedAvailableQty=quantity(row.get('planavailableqty')))
        dimensions={key:item[key] for key in fields if key not in ('productName','model','organizationName','warehouseName','unitName','stockStatusName')}
        item['selectionKey']=hashlib.sha256(json.dumps(dimensions,sort_keys=True).encode()).hexdigest()
        item['comparable']=bool(item['organizationId'] and item['warehouseId'] and item['unitName']
                                and item['availableQty'] is not None)
        item['comparisonReason']='' if item['comparable'] else '缺少组织、仓库、单位或有效可用量，不能比较'
        records.append(item)
    key_counts=Counter(record['selectionKey'] for record in records)
    for index, record in enumerate(records):
        if key_counts[record['selectionKey']]>1:
            record['selectionKey'] += '-duplicate-' + str(index)
            record['comparable']=False
            record['comparisonReason']='返回了相同标识的多条库存记录，无法确定独立库存，暂不比较'
    return {'productCode':code,'queriedAt':clock(),'maxAgeSeconds':120,'found':bool(records),
            'status':'ok' if records else 'no_records',
            'emptyResponse': 'null' if raw['data'] is None else 'list' if not records else None,
            'records':records,'recordCount':len(records),'rule':'voucher_order',
            'message':'请选择组织、仓库和库存状态对应的一条记录查看可用量。' if records else '没有返回库存记录，不能据此认定库存为0。',
            'warnings':['仅为查询时点库存，不占用或预留库存；按所选记录比较，不跨仓库、状态或所有权累加。']}

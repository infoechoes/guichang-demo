"""Read-only order reference resolution. No order writes or historical-ID defaults.

Paths and fields are from the official 2026-09-08 API snapshot in yonyou-doc.
Use the product's verified main unit for all three order quantities only when
the requested unit is that main unit; never infer a box/piece conversion.
"""
import copy
import re
import unicodedata
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from decimal import Decimal, ROUND_HALF_UP, localcontext
from core import safe_message
from reference_cache import client_cache

RESOLVE_BUDGET_SECONDS = 60

REFERENCE_PATHS = {
    'customers': '/yonbip/digitalModel/merchant/newlist',
    'organizations': '/yonbip/digitalModel/orgunit/querytree',
    'departments': '/yonbip/digitalModel/admindept/tree',
    'staff': '/yonbip/digitalModel/staff/list',
    'currencies': '/yonbip/digitalModel/currencytenant/batchQueryDetail',
    'exchangeTypes': '/yonbip/digitalModel/exchangeratetype/batchQueryDetail',
    'transactions': '/yonbip/digitalModel/transtype/queryByBillTypeCodes',
    'taxRates': '/yonbip/digitalModel/taxrate/findByTaxRate',
}


def text(value):
    if isinstance(value, dict):
        value = value.get('zh_CN') or value.get('simplifiedName') or ''
    return str(value or '').strip()


def norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text(value))).casefold()


def records(response, require_complete=True):
    if not isinstance(response, dict) or str(response.get('code')) != '200':
        raise ValueError('用友档案查询未成功')
    data = response.get('data')
    if data is None: return []
    if isinstance(data, dict):
        if 'recordList' in data:
            if require_complete and int(data.get('recordCount') or 0) > len(data['recordList']):
                raise ValueError('查询结果超过单页，请使用更准确的名称')
            data = data['recordList']
        elif 'id' in data: data = [data]
    if not isinstance(data, list): raise ValueError('用友档案返回结构无法识别')
    result = []
    def walk(items, depth=0):
        if depth > 20: raise ValueError('档案层级过深')
        for item in items:
            if not isinstance(item, dict): raise ValueError('档案记录格式无效')
            if 'id' in item: result.append(item)
            for key in ('children', 'data'):
                if isinstance(item.get(key), list): walk(item[key], depth + 1)
            if len(result) > 5000: raise ValueError('档案查询结果过多')
    walk(data)
    return [r for r in result if str(r.get('enable', '1')) == '1' and str(r.get('dr', '0')) == '0']


def unique(items, value, key='name'):
    found = {text(r.get('id')): r for r in items if text(r.get('id')) and norm(r.get(key)) == norm(value)}
    if not found: raise ValueError(f'用友中未找到“{text(value)}”')
    if len(found) != 1: raise ValueError(f'“{text(value)}”有多个同名档案，请选择具体档案')
    return next(iter(found.values()))


def manual_unit_conversion(row):
    """Validate the operator's explicit conversion against this exact row.

    This proves arithmetic and scope, not an inferred packaging ratio. The ERP
    product ID/main-unit checks still run independently before confirmation.
    """
    if 'unitConversion' not in row: return False
    conversion = row['unitConversion']
    if not isinstance(conversion, dict) or conversion.get('kind') != 'manual-unit-v1':
        raise ValueError('人工单位换算记录无效，请重新确认换算')
    for field, row_field in [('productCode', 'code'), ('name', 'name'), ('spec', 'spec')]:
        if not isinstance(conversion.get(field), str) or text(conversion[field]) != text(row.get(row_field)):
            raise ValueError('商品信息已变化，请重新确认单位换算')
    if (not text(row.get('sourceDemandUnit')) or
            not isinstance(conversion.get('fromUnit'), str) or not isinstance(conversion.get('toUnit'), str) or
            norm(conversion.get('fromUnit')) != norm(row.get('sourceDemandUnit')) or
            norm(conversion.get('toUnit')) != norm(row.get('unit')) or
            not text(conversion.get('toUnit'))):
        raise ValueError('单位信息已变化，请重新确认单位换算')

    def number(value, places):
        if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,' + str(places) + r'})?', value):
            raise ValueError('换算数字格式或小数位数无效，请重新确认换算')
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= Decimal('1000000000'):
            raise ValueError('换算数字必须大于0且不超过10亿')
        return amount

    ratio = number(conversion.get('ratio'), 8)
    if norm(conversion.get('fromUnit')) == norm(conversion.get('toUnit')) and ratio != 1:
        raise ValueError('相同单位的换算比例必须为1')
    from_quantity = number(conversion.get('fromQuantity'), 8)
    to_quantity = number(conversion.get('toQuantity'), 8)
    from_price = number(conversion.get('fromPrice'), 6)
    to_price = number(conversion.get('toPrice'), 6)
    if (number(row.get('quantity'), 8) != to_quantity or number(row.get('price'), 6) != to_price):
        raise ValueError('数量或价格已变化，请重新确认单位换算')
    with localcontext() as context:
        context.prec = 60
        if (to_quantity != (from_quantity * ratio).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP) or
                to_price != (from_price / ratio).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)):
            raise ValueError('换算比例与数量、单价不符，请重新确认换算')
        if ((from_quantity * from_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) !=
                (to_quantity * to_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)):
            raise ValueError('单位换算前后金额不一致，请调整比例或精度')
    return True


def search_references(data, client):
    """Name picker fallback for genuine ambiguity; expose no employee contacts."""
    kind, name = data.get('kind'), text(data.get('query'))
    header, page = data.get('header') or {}, data.get('pageIndex', 1)
    if not 1 <= len(name) <= 100 or type(page) is not int or not 1 <= page <= 100:
        raise ValueError('请提供有效的档案名称和页号')
    body = {'pageIndex': page, 'pageSize': 50, 'name': name}
    if kind in ('customer', 'invoiceCustomer'):
        api = 'customers'; body.pop('name'); body.update(fuzzyName=name, filterPotential=True)
    elif kind in ('salesOrg', 'settlementOrg', 'stockOrg'):
        api = 'organizations'; body = {'name': name, 'enable': 1}
    elif kind in ('currency', 'natCurrency'): api = 'currencies'
    elif kind == 'exchangeRateType': api = 'exchangeTypes'
    elif kind == 'transaction': api = 'transactions'; body = {'codes': ['voucher_order']}
    elif kind in ('department', 'salesperson'):
        org = text(header.get('salesOrgId'))
        if not org: raise ValueError('请先选择销售组织，或先点击预览并检查完成自动关联')
        if kind == 'department': api = 'departments'; body = {'externalData': {'parentorgid': org, 'enable': ['1']}}
        else:
            api = 'staff'; body.update({'mainJobList.org_id': org, 'enable': 1})
            if header.get('saleDepartmentId'): body['mainJobList.dept_id'] = header['saleDepartmentId']
    else: raise ValueError('不支持的档案类型')
    response = client.call(api, body)
    items = records(response, require_complete=False)
    if kind in ('department', 'transaction'):
        items = [r for r in items if norm(name) in norm(r.get('name'))]
    candidates = [{'id': text(r['id']), 'name': text(r.get('name')), 'code': text(r.get('code')), 'selectable': True, 'warnings': []} for r in items if text(r.get('name'))]
    payload = response.get('data')
    total = int(payload.get('recordCount') or 0) if isinstance(payload, dict) else len(items)
    return {'available': True, 'candidates': candidates, 'pageIndex': page, 'hasMore': page * 50 < total, 'warnings': []}


def resolve_order(data, client):
    if not isinstance(data, dict) or not isinstance(data.get('header'), dict):
        raise ValueError('订单格式无效')
    rows = data.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200 or any(not isinstance(r, dict) for r in rows):
        raise ValueError('需要1至200项商品')
    header = copy.deepcopy(data['header'])
    issues, evidence = [], []
    cache = {}
    product_cache = client_cache(client)
    force = data.get('forceRefreshArchives') is True
    deadline = time.monotonic() + RESOLVE_BUDGET_SECONDS
    halted = False
    retry_after = 0

    def note_limit(error):
        nonlocal halted, retry_after
        diagnostic = getattr(error, 'diagnostic', {})
        limited = (diagnostic.get('category') == 'rate_limited' or
                   diagnostic.get('httpStatus') == 429 or str(diagnostic.get('apiCode')) == '310050')
        if limited:
            halted = True
            retry_after = max(retry_after, int(diagnostic.get('retryAfterSeconds') or 30))
        return limited

    def query(kind, body=None, query=None):
        nonlocal halted
        import json
        key = (kind, json.dumps(body, sort_keys=True), json.dumps(query, sort_keys=True))
        if key not in cache:
            if time.monotonic() >= deadline: halted = True
            if halted: raise ValueError('用友查询已暂停，已完成的结果已保留，请稍后补齐剩余档案')
            try: cache[key] = records(client.call(kind, body, query=query))
            except ValueError as error:
                note_limit(error)
                raise
        return cache[key]

    def associate(name_key, id_key, label, kind, body, optional=False):
        name = text(header.get(name_key))
        # Existing explicitly selected references remain intact. Names without
        # references must be resolved, even for optional fields (no silent loss).
        if text(header.get(id_key)): return
        if not name:
            if not optional: issues.append({'field': name_key, 'message': f'请填写{label}'})
            return
        try:
            item = unique(query(kind, body), name)
            header[id_key] = text(item['id'])
            evidence.append({'field': id_key, 'name': name, 'code': text(item.get('code')), 'basis': '用友唯一同名档案'})
        except ValueError as error:
            issues.append({'field': name_key, 'message': f'{label}：{safe_message(error)}'})

    associate('salesOrgName', 'salesOrgId', '销售组织', 'organizations', {'name': text(header.get('salesOrgName')), 'enable': 1})
    for name_key, id_key, label in [('customerName', 'agentId', '客户'), ('invoiceCustomerName', 'invoiceAgentId', '开票客户')]:
        if name_key == 'invoiceCustomerName' and not text(header.get(name_key)):
            if not header.get(id_key) and header.get('agentId'):
                header[id_key] = header['agentId']; evidence.append({'field': id_key, 'basis': '开票客户未另设，使用本单客户'})
            continue
        associate(name_key, id_key, label, 'customers', {'pageIndex': 1, 'pageSize': 50, 'name': text(header.get(name_key)), 'filterPotential': True})
    for name_key, id_key, label in [('settlementOrgName', 'settlementOrgId', '开票组织'), ('stockOrgName', 'stockOrgId', '库存组织')]:
        if not text(header.get(name_key)):
            if not header.get(id_key) and header.get('salesOrgId'):
                header[id_key] = header['salesOrgId']; evidence.append({'field': id_key, 'basis': '未另设组织，使用本单销售组织'})
        else: associate(name_key, id_key, label, 'organizations', {'name': text(header[name_key]), 'enable': 1})
    for name_key, id_key, label, kind in [('currencyName', 'currencyId', '币种', 'currencies'), ('natCurrencyName', 'natCurrencyId', '本币', 'currencies'), ('exchangeRateTypeName', 'exchangeRateType', '汇率类型', 'exchangeTypes')]:
        associate(name_key, id_key, label, kind, {'pageIndex': 1, 'pageSize': 50, 'name': text(header.get(name_key))})
    associate('transactionName', 'transactionTypeId', '交易类型', 'transactions', {'codes': ['voucher_order']})
    if header.get('salesOrgId'):
        associate('departmentName', 'saleDepartmentId', '销售部门', 'departments', {'externalData': {'parentorgid': header['salesOrgId'], 'enable': ['1']}}, optional=True)
        staff_body = {'pageIndex': 1, 'pageSize': 50, 'name': text(header.get('salespersonName')), 'mainJobList.org_id': header['salesOrgId'], 'enable': 1}
        if header.get('saleDepartmentId'): staff_body['mainJobList.dept_id'] = header['saleDepartmentId']
        associate('salespersonName', 'corpContact', '销售业务员', 'staff', staff_body, optional=True)

    # Reuse only server-verified archives. Browser IDs cannot skip verification.
    # Submit a bounded window, so rate limiting stops the unscheduled remainder.
    codes = list(dict.fromkeys(text(r.get('code')) for r in rows if text(r.get('code'))))
    if force:
        product_cache.invalidate((text(header.get('salesOrgId')), code) for code in codes)
    def product(code):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code): return None, '商品编码无效', False, False, None
        queried = False
        def load():
            nonlocal queried
            queried = True
            body = {'pageIndex': 1, 'pageSize': 50, 'productCode': code}
            if header.get('salesOrgId'): body['orgId'] = header['salesOrgId']
            item = unique(records(client.call('products', body)), code, 'code')
            if re.search(r'禁用|停用|作废', text(item.get('name'))) or item.get('stopStatus') is True:
                raise ValueError('此商品档案已标记停用，请重新匹配有效商品')
            if item.get('hasSpecs') is True: raise ValueError('此商品含多规格属性，请先核实具体规格档案')
            if not text(item.get('unitId')) or not text(item.get('unitName')):
                raise ValueError('此商品档案缺少主计量单位，请核实用友商品档案')
            # Keep only the reference fields used below, never unrelated product
            # descriptions, prices, contact details or other API response data.
            return {field: text(item.get(field)) for field in ('id', 'code', 'name', 'unitId', 'unitName')}
        try:
            item, reused = product_cache.get((text(header.get('salesOrgId')), code), load, force=force)
            return item, None, queried, reused, None
        except ValueError as error:
            if getattr(error, 'diagnostic', {}).get('requestDispatched') is False: queried = False
            return None, safe_message(error), queried, False, error
    products = {}
    stats = {'queriedProducts': 0, 'cachedProducts': 0, 'pendingProducts': 0}
    with ThreadPoolExecutor(max_workers=4) as pool:
        remaining = iter(codes)
        active = {}
        def schedule():
            while len(active) < 4 and not halted and time.monotonic() < deadline:
                code = next(remaining, None)
                if code is None: break
                active[pool.submit(product, code)] = code
        schedule()
        while active:
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                code = active.pop(future)
                item, error, queried, reused, exception = future.result()
                stats['queriedProducts'] += int(queried)
                stats['cachedProducts'] += int(reused)
                products[code] = (item, error)
                if exception: note_limit(exception)
            schedule()
    for code in codes:
        if code not in products:
            # A cooldown stops new requests, not use of already verified data.
            item = product_cache.peek((text(header.get('salesOrgId')), code))
            if item is not None:
                products[code] = (item, None)
                stats['cachedProducts'] += 1
            else:
                products[code] = (None, '本次查询已暂停，已完成的结果已保留，请稍后补齐剩余档案')
    stats['pendingProducts'] = sum(bool(error) for item, error in products.values())

    output = []
    for index, original in enumerate(rows):
        row = copy.deepcopy(original)
        row.pop('unitAliasResolution', None)  # Rebuild evidence from verified archives.
        row.pop('unitReference', None)
        code = text(row.get('code'))
        label = f'第{index + 1}项 {text(row.get("name"))}'
        def issue(message): issues.append({'row': index, 'message': f'{label}：{message}'})
        item, error = products.get(code, (None, '请先匹配商品编码'))
        if error:
            row['sameUnitConfirmed'] = False
            issue(error)
        else:
            if row.get('productId') and text(row['productId']) != text(item['id']):
                row['sameUnitConfirmed'] = False
                issue('当前编码与已选用友商品不一致，请重新匹配')
            else:
                row['productId'] = text(item['id'])
                row['unitReference'] = {'productCode': code, 'productId': text(item['id']),
                                        'unitId': text(item['unitId']), 'unitName': text(item['unitName'])}
                conversion_valid, conversion_error = False, None
                try: conversion_valid = manual_unit_conversion(row)
                except (ValueError, ArithmeticError) as error: conversion_error = safe_message(error)
                requested_unit, archive_unit = norm(row.get('unit')), norm(item.get('unitName'))
                demand_unit = norm(row.get('sourceDemandUnit'))
                catalog_source = row.get('catalogSource')
                catalog_unit = (norm(catalog_source.get('unit'))
                                if isinstance(catalog_source, dict) and 'unit' in catalog_source else None)
                battery_units = {'节', '粒'}
                battery_alias = (not conversion_valid and not conversion_error
                                 and '电池' in text(row.get('name')) and '电池' in text(item.get('name'))
                                 and requested_unit in battery_units and archive_unit in battery_units
                                 and (not demand_unit or demand_unit in battery_units))
                single_piece_units = {'个', '支', '只'}
                single_piece_alias = (not conversion_valid and not conversion_error
                                      and text(row.get('name')) and text(item.get('name'))
                                      and requested_unit in single_piece_units
                                      and archive_unit in single_piece_units
                                      and (not demand_unit or demand_unit in single_piece_units)
                                      and (catalog_unit is None or catalog_unit in single_piece_units))
                alias = battery_alias or single_piece_alias
                if alias and (requested_unit != archive_unit or (demand_unit and demand_unit != archive_unit)):
                    if not demand_unit:
                        row['sourceDemandUnit'] = text(row.get('unit'))
                    row['unitAliasResolution'] = {
                        'kind': 'single-piece-v1' if single_piece_alias else 'battery-piece-v1',
                        'fromUnit': text(row.get('sourceDemandUnit')) if demand_unit and demand_unit != archive_unit else text(row.get('unit')),
                        'toUnit': text(item['unitName']), 'ratio': '1', 'productCode': code,
                        'basis': '单件计数单位别名' if single_piece_alias else '已确认的电池单件单位别名',
                    }
                    row['unit'] = text(item['unitName'])
                if conversion_error:
                    row['sameUnitConfirmed'] = False
                    issue(conversion_error)
                elif not text(item.get('unitId')) or not norm(row.get('unit')) or norm(row.get('unit')) != norm(item.get('unitName')):
                    row['sameUnitConfirmed'] = False
                    issue(f'订单单位“{text(row.get("unit"))}”与用友主单位“{text(item.get("unitName"))}”不一致，需核对换算')
                elif row.get('sourceDemandUnit') and norm(row['sourceDemandUnit']) != norm(row.get('unit')) and not alias and not conversion_valid:
                    row['sameUnitConfirmed'] = False; issue('原需求单位与当前单位不一致，需核对换算')
                else:
                    row.update(unitId=text(item['unitId']), sameUnitConfirmed=True)
                    row['unitResolution'] = {'basis': '按用友主单位录单，销售/主计量/计价数量均为当前数量，换算率1', 'productCode': code, 'unitName': text(item['unitName']), 'unitId': text(item['unitId'])}
        if not text(row.get('taxId')):
            try:
                rate = Decimal(text(row.get('taxRate')) or '0.13')
                if not rate.is_finite() or not 0 <= rate <= 1: raise ValueError('税率应为0至1的小数')
                candidates = query('taxRates', query={'taxRate': format(rate * 100, 'f')})
                candidates = [r for r in candidates if str(r.get('scope')) in ('1', '3') and Decimal(str(r.get('ntaxrate'))) == rate * 100 and r.get('taxfree') is not True and r.get('notaxation') is not True]
                if len(candidates) != 1: raise ValueError('销售税目没有唯一匹配，请核实税率及用友税目设置')
                row['taxId'] = text(candidates[0]['id'])
            except (ValueError, ArithmeticError) as error: issue(safe_message(error))
        output.append(row)
    return {'header': header, 'rows': output, 'issues': issues, 'evidence': evidence, 'ready': not issues, 'readOnly': True,
            'resolutionStats': stats, 'retryAfterSeconds': retry_after}

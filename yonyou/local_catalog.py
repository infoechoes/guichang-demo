"""Private, offline product catalogue extracted only from the authorised 2026 sheet.

Codes are business codes, never ERP internal IDs. No network fallback exists.
"""
import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

INDEX = Path(__file__).resolve().parent / 'local-state' / 'catalog-2026.json'
SOURCE_TYPE = 'local_catalog_2026'
LABEL = '商品参考目录（由部署者配置）'


def selected_row_price(item, label=LABEL, column='H', price_label='贵昌（95折）众数价'):
    """Modal H-column price within this exact code/name/spec/unit candidate.

    Count numeric equivalents together, using source order to break ties. Never
    count the deduplicated referencePrices list or combine different products.
    """
    result = {'value': '', 'sourceRow': None, 'sourceRows': [], 'column': column,
        'status': 'missing', 'source': '', 'warnings': [], 'method': 'mode',
        'occurrences': 0, 'validObservations': 0, 'tie': False}
    prices = {}
    locations = {}
    invalid = 0
    for observation in item.get('sourcePrices') or []:
        value = observation.get('guichang95')
        if value in ('', None): continue
        try:
            amount = Decimal(str(value))
            if not amount.is_finite() or not 0 < amount <= 1_000_000_000: raise InvalidOperation()
            if amount.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP) == 0: raise InvalidOperation()
            prices.setdefault(amount, []).append(observation.get('row'))
            locations.setdefault(amount, []).append(observation)
        except (InvalidOperation, ValueError): invalid += 1
    if not prices:
        result['status'] = 'invalid' if invalid else 'missing'
        result['warnings'].append(f'所选商品没有可用的{price_label}，请补填单价。')
        return result
    amount = max(prices, key=lambda price: len(prices[price]))
    rows = prices[amount]
    rounded = amount.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)
    result.update(value=format(rounded,'f').rstrip('0').rstrip('.'), status='available',
        sourceRow=rows[0], sourceRows=rows, occurrences=len(rows),
        validObservations=sum(map(len,prices.values())),
        tie=sum(len(group)==len(rows) for group in prices.values())>1,
        source=f'{label} · {price_label} · 出现{len(rows)}次 · 首见{column}{rows[0]}')
    if locations[amount][0].get('sheet'):
        first = locations[amount][0]
        actual_column = first.get('column') or column
        result.update(sourceSheet=first['sheet'], column=actual_column,
            sourceLocations=[{'sheet':p.get('sheet',''), 'row':p.get('row'),
                              'column':p.get('column') or column} for p in locations[amount]],
            source=f"{label} · {price_label} · 出现{len(rows)}次 · 首见 {first['sheet']}!{actual_column}{rows[0]}")
    if amount != rounded: result['warnings'].append('按接口上限将单价四舍五入至6位小数。')
    if result['tie']: result['warnings'].append('出现次数并列，按规则取原表先出现的价格。')
    return result


def norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def name_parts(value):
    """Retain both the name and bracket content; neither is inherently an alias."""
    main = norm(value)
    details = []
    groups = re.compile(r'\([^()]*\)|\[[^\[\]]*\]|【[^【】]*】|\{[^{}]*\}')
    while groups.search(main):
        def remove(match):
            details.append(match.group()[1:-1])
            return ''
        main = groups.sub(remove, main)
    return main, '|'.join(details)


def name_information(value, spec=''):
    main, detail = name_parts(value)
    return {part for part in (main, *detail.split('|'), norm(spec)) if part}


def name_related(left, right):
    if not left or not right:
        return False
    return left == right or (min(len(left), len(right)) >= 2 and (left in right or right in left)) or (
        min(len(left), len(right)) >= 3 and SequenceMatcher(None, left, right).ratio() >= .65)


def unit_compatible(query, row, item):
    demands = [norm(row.get('unit'))]
    if norm(row.get('sourceDemandUnit')): demands.append(norm(row['sourceDemandUnit']))
    selected = norm(item.get('unit'))
    if not selected or not all(demands):
        return False
    return all(demand == selected or {demand, selected} <= {'个', '支', '只'} or (
        {demand, selected} <= {'节', '粒'} and '电池' in query and '电池' in item['name']) for demand in demands)


def information_covered(required, offered):
    # Additional qualifiers might distinguish another SKU; do not silently ignore them.
    return bool(required) and all(any(part == other or (len(part) >= 2 and part in other)
                                     for other in offered) for part in required)


def source_occurrences(item, default_sheet):
    """Count original rows once; the same row number on two sheets is two rows."""
    locations = item.get('sourceLocations')
    if locations:
        if not all(isinstance(p, dict) and isinstance(p.get('sheet'), str) and p['sheet']
                   and type(p.get('row')) is int and p['row'] > 0 for p in locations):
            return None
        return {(p['sheet'], p['row']) for p in locations}
    rows = item.get('sourceRows')
    if not rows or not all(type(n) is int and n > 0 for n in rows):
        return None
    return {(default_sheet, n) for n in rows}


def preferred_code(candidates, default_sheet):
    """Only fully matching descriptions vote. Never guess from partial matches."""
    exact = [c for c in candidates if c['exactMatch'] and c['selectable']]
    counts, unknown = {}, set()
    for candidate in exact:
        code = candidate['code']
        observations = source_occurrences(candidate, default_sheet)
        counts.setdefault(code, set())
        if observations is None: unknown.add(code)
        else: counts[code].update(observations)
    frequencies = {code: len(rows) for code, rows in counts.items()}
    winner = None
    if len(counts) > 1 and not unknown:
        maximum = max(frequencies.values())
        leaders = [code for code, count in frequencies.items() if count == maximum]
        if len(leaders) == 1: winner = leaders[0]
    return winner, frequencies, unknown


def comparable_price(value):
    # Exact decimal equality: formatting zeros are harmless, actual cents are not.
    if isinstance(value, bool): return None
    try:
        price = Decimal(str(value).strip())
        return price if price.is_finite() and 0 < price <= 1_000_000_000 else None
    except (InvalidOperation, ValueError):
        return None


def price_preference(candidates, row, default_sheet):
    source = comparable_price(row.get('sourcePrice'))
    matching = [c for c in candidates if source is not None and c['exactMatch'] and c['selectable']
                and comparable_price(c['comparisonPrice'].get('value')) == source]
    codes = {c['code'] for c in matching}
    if len(codes) == 1: winner = next(iter(codes))
    elif codes: winner = preferred_code(matching, default_sheet)[0]
    else: winner = None
    return matching, winner


def cell_text(value):
    if isinstance(value, str): return value.strip()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(int(value)) if value == int(value) else str(value)
    return ''


def build(source, target=INDEX):
    # XLS reader is needed only for explicit refresh, not for serving queries.
    sys.path.insert(0, str(Path(__file__).parent / 'local-state' / 'reader-deps'))
    import xlrd
    raw = Path(source).read_bytes()
    book = xlrd.open_workbook(file_contents=raw, on_demand=True, formatting_info=True)
    sheet = book.sheet_by_name('2026')
    if sheet.row_values(0)[:4] != ['系统编码', '材料名称', '规格型号', '计量单位']:
        raise ValueError('2026表头与已核实的商品字段不符，停止更新')
    groups, issues = {}, []
    named = 0
    for r in range(1, sheet.nrows):
        vals = sheet.row_values(r)
        code, name, spec, unit = map(cell_text, vals[:4])
        if not name: continue
        named += 1
        if code:
            # Preserve explicit leading-zero display formats for numeric codes.
            fmt = book.format_map[book.xf_list[sheet.cell_xf_index(r, 0)].format_key].format_str
            if sheet.cell_type(r, 0) == xlrd.XL_CELL_NUMBER and re.fullmatch('0+', fmt):
                code = code.zfill(len(fmt))
            if not re.fullmatch(r'[A-Za-z0-9]+', code) or (code.isdigit() and len(code) > 15 and sheet.cell_type(r, 0) == xlrd.XL_CELL_NUMBER):
                issues.append({'row': r + 1, 'reason': '编号格式异常', 'code': code, 'name': name, 'spec': spec})
                code = ''
        else:
            issues.append({'row': r + 1, 'reason': '缺少系统编码', 'name': name, 'spec': spec})
        key = (code, name, spec, unit)
        item = groups.setdefault(key, {'code': code, 'name': name, 'spec': spec, 'unit': unit,
            'sourceRows': [], 'sourcePrices': [], 'referencePrices': {'hospital': [], 'guichang95': []}})
        item['sourceRows'].append(r + 1)
        source_price = {'row': r+1, 'hospital': '', 'guichang95': ''}
        for column, field in [(5, 'hospital'), (7, 'guichang95')]:
            val = vals[column]
            if isinstance(val, (int, float)) and math.isfinite(val) and val >= 0:
                price = format(val, '.12g')
                source_price[field] = price
                if price not in item['referencePrices'][field]: item['referencePrices'][field].append(price)
        item['sourcePrices'].append(source_price)
    items = list(groups.values())
    source_hash = hashlib.sha256(raw).hexdigest()
    for item in items:
        item['id'] = 'catalog-' + hashlib.sha256(json.dumps([item[k] for k in ('code','name','spec','unit')], ensure_ascii=False).encode()).hexdigest()[:20]
    codes_by_key = defaultdict(set)
    for item in items:
        if item['code']: codes_by_key[(norm(item['name']), norm(item['spec']))].add(item['code'])
    for item in items:
        item['conflictingCodes'] = sorted(codes_by_key[(norm(item['name']), norm(item['spec']))])
    result = {'schemaVersion': 1, 'sourceFile': Path(source).name, 'sourceSheet': '2026',
        'sourceSha256': source_hash, 'sourceRange': f'A1:H{sheet.nrows}',
        'stats': {'sourceRows': sheet.nrows - 1, 'namedRows': named, 'records': len(items),
            'codedRecords': sum(bool(x['code']) for x in items),
            'uniqueCodes': len({x['code'] for x in items if x['code']}),
            'issues': dict(Counter(x['reason'] for x in issues)),
            'conflictingNameSpecs': sum(len(codes) > 1 for codes in codes_by_key.values())},
        'items': items, 'issues': issues}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)
    return result['stats']


def search_catalog(query, row=None, page=1, index_path=INDEX, source_label=None):
    row = row if isinstance(row, dict) else {}
    common = {'sourceType': SOURCE_TYPE, 'sourceLabel': LABEL, 'sourceSheet': '2026',
        'pageIndex': page, 'hasMore': False, 'found': False, 'candidates': [], 'recordCount': 0}
    try:
        db = json.loads(Path(index_path).read_text(encoding='utf-8'))
        if db.get('schemaVersion') != 1 or (source_label is None and db.get('sourceSheet') != '2026') or not isinstance(db.get('items'), list):
            raise ValueError('invalid catalogue')
    except (OSError, ValueError, TypeError):
        return {**common, 'available': False, 'matchStatus': 'unavailable', 'warnings': [],
            'message': '2026商品知识库未加载或已损坏，请重新导入；不会转去用友查询。'}
    label, sheet = source_label or LABEL, db.get('sourceSheet', '2026')
    common.update(sourceLabel=label, sourceSheet=sheet)
    q, spec = norm(query), norm(row.get('spec'))
    main_query, query_detail = name_parts(query)
    query_parts = [part for part in query_detail.split('|') if part]
    required_information = name_information(query, spec)
    original_name = row.get('name') or query
    original_information = name_information(original_name, spec)
    candidates = []
    for item in db['items']:
        name, model = norm(item['name']), norm(item['spec'])
        main_name, name_detail = name_parts(name)
        exact_code = bool(q) and q == norm(item['code'])
        similarity = SequenceMatcher(None, main_query, main_name).ratio() if main_query and main_name else 0
        item_parts = [part for part in name_detail.split('|') if part]
        cross_pairs = [(main_query, part) for part in item_parts] + [(part, main_name) for part in query_parts]
        # A shared purpose alone is not a product identity, while a bracketed alias
        # or model can retrieve suggestions even when the outer names differ.
        contextual = re.compile(r'^(?:.*专用|通用|原装|兼容(?:版)?|.*(?:病区|科室|门诊|楼|层)|颜色|[红蓝黑白绿黄]色)$')
        cross_pairs += [(left, right) for left in query_parts for right in item_parts
                        if not contextual.search(left) and not contextual.search(right)]
        cross_similarity = max((SequenceMatcher(None, left, right).ratio() for left, right in cross_pairs if left and right), default=0)
        if not (exact_code or name_related(main_query, main_name) or any(name_related(left, right) for left, right in cross_pairs)): continue
        offered_information = name_information(name, model)
        same_information = bool(original_information) and original_information == offered_information
        units_match = unit_compatible(query, row, item)
        code_spec_match = not spec or spec in offered_information
        certain = units_match and row.get('unitConversion') is None and (same_information or (exact_code and code_spec_match and norm(original_name) == q))
        detail_similarity = SequenceMatcher(None, query_detail, name_detail).ratio() if query_detail and name_detail else 0
        exact = certain
        spec_match = not spec or spec in offered_information
        default_price = selected_row_price(item, label, db.get('priceColumn', 'H'),
                                           '默认参考价（众数）' if source_label else '贵昌（95折）众数价')
        hospital_price = selected_row_price({'sourcePrices':[
            {**p,'guichang95':p.get('hospital','')} for p in item.get('sourcePrices') or []]},
            label, 'F', '客户/医院价')
        comparison_price = {'value':hospital_price['value'], 'basis':'客户/医院价'} if hospital_price['status']=='available' else {
            'value':default_price['value'] if default_price['status']=='available' else '', 'basis':'默认参考价'}
        warnings = ['系统编码为用友商品编码；保存接口支持商品编码，但当前适配器尚需关联单位及税档案，暂不能直接保存。'] + default_price['warnings']
        if not item['code']: warnings.append('此记录缺少有效系统编码，不能选用。')
        if len(item['conflictingCodes']) > 1: warnings.append('同名称和规格对应多个编号，按原始记录频次判断。')
        if not spec: warnings.append('需求未填写规格型号，请核对候选规格。')
        elif not spec_match: warnings.append('未找到该规格的一致记录；此项仅为同名/近似名称候选。')
        if row.get('unit') and not units_match:
            warnings.append('需求单位与总表单位不同；不得直接沿用数量，需确认换算。')
        if not certain: warnings.append('仅为候选，尚未确认商品信息完全一致；不会自动替换原表内容。')
        disabled = bool(re.search(r'禁用|停用|作废', item['name']))
        if disabled: warnings.append('名称包含禁用/停用/作废提示，不可选用。')
        if not item['unit']: warnings.append('总表缺计量单位，不能选用。')
        candidates.append({**item, 'sourceType': SOURCE_TYPE, 'sourceLabel': label,
            'sourceSheet': '、'.join(dict.fromkeys(p['sheet'] for p in item.get('sourceLocations',[]) if p.get('sheet'))) or sheet,
            'defaultPrice': default_price, 'comparisonPrice':comparison_price,
            'productId': '', 'unitId': '', 'brand': '',
            'rowPatch': {k: item[k] for k in ('name','spec','unit')} | {'productCode': item['code'], 'productId': '', 'unitId': ''},
            'selectable': bool(item['code'] and item['unit'] and not disabled), 'warnings': warnings,
            'matchScore': round(60 * SequenceMatcher(None, q, name).ratio() + 25 * max(similarity, cross_similarity) + 15 * detail_similarity),
            'mainNameScore': similarity, 'supplementaryScore': detail_similarity,
            'exactCodeMatch': exact_code,
            'autoAdoptable': certain,
            'autoAdoptInput': {'name': original_name, 'spec': row.get('spec') or '', 'unit': row.get('unit') or '',
                               'sourceDemandUnit': row.get('sourceDemandUnit') or '',
                               'sourcePrice': row.get('sourcePrice') if row.get('sourcePrice') is not None else ''},
            '_plausible': units_match and (exact_code and code_spec_match or information_covered(required_information, offered_information)),
            'matchReasons': ['商品信息完整一致' if certain else '括号内外信息相关，需人工确认',
                             '规格信息一致' if spec and spec_match else '请核对型号及其他描述'],
            'exactMatch': exact})
    candidates.sort(key=lambda x: (not x['exactCodeMatch'], not x['exactMatch'], -x['matchScore'],
        not x['selectable'], x['code']))
    # Resolve ambiguity across the entire result, not just the returned page.
    # Once a complete, unit-compatible exact record exists, only those exact
    # records establish identity.  Similar or richer descriptions remain
    # useful retrieval choices, but cannot veto the complete match.
    exact_candidates = [c for c in candidates if c['exactMatch'] and c['selectable']]
    safe = exact_candidates if exact_candidates else []
    frequency_winner, frequencies, unknown_frequencies = preferred_code(candidates, sheet)
    price_matches, price_winner = price_preference(candidates, row, sheet)
    winner = price_winner if price_matches else frequency_winner
    for candidate in candidates:
        candidate.pop('_plausible')
        # Identity comes from the catalogue's code, not each historical spelling.
        # This only lifts ambiguity for an already fully matching candidate;
        # richer descriptions under its code never become automatic substitutes.
        # Incompatible units or other codes still block adoption.
        unique_identity = bool(safe) and all(
            c['code'] == candidate['code'] and unit_compatible(original_name, {'unit':candidate['unit']}, c)
            for c in safe)
        frequency_identity = winner == candidate['code'] and bool(safe) and all(
            c['code'] in frequencies and unit_compatible(original_name, {'unit':candidate['unit']}, c)
            for c in safe) and set(candidate['conflictingCodes']) <= frequencies.keys()
        candidate['codeFrequency'] = {'count': frequencies.get(candidate['code'], 0),
            'counts': frequencies, 'winner': frequency_winner or '', 'known': candidate['code'] not in unknown_frequencies,
            'method': 'distinct-source-rows'}
        price_eligible = not price_matches or any(candidate is c for c in price_matches)
        candidate['codePreference'] = {'winner':winner or '', 'basis':'source-price' if price_matches else 'frequency',
            'sourcePriceMatched':bool(price_matches) and price_eligible}
        candidate['autoAdoptable'] = candidate['autoAdoptable'] and candidate['selectable'] and (
            unique_identity and len(candidate['conflictingCodes']) <= 1 or frequency_identity) and price_eligible
        if candidate['autoAdoptable'] and frequency_identity:
            candidate['matchReasons'].append(f"{candidate['comparisonPrice']['basis']}与客户原表单价一致，优先采用。" if price_matches else
                f"同描述编码中出现最多：{frequencies[winner]}次，优先采用。")
        elif len(frequencies) > 1 and not winner:
            candidate['warnings'].append('编码频次并列或来源次数不完整，请人工确认。')
        if candidate['exactMatch'] and not candidate['autoAdoptable']:
            candidate['warnings'].append('存在其他可能对应的商品或编号，请人工确认。')
    # The browser adopts the first candidate: put the verified frequency winner first.
    candidates.sort(key=lambda c: not c['autoAdoptable'])
    total = len(candidates)
    return {**common, 'available': True, 'found': bool(total), 'recordCount': total,
        'hasMore': page * 20 < total, 'candidates': candidates[(page-1)*20:page*20],
        'matchStatus': 'exact' if any(x['autoAdoptable'] for x in candidates) else 'candidates' if total else 'not_found',
        'message': '已综合括号内外信息查找；仅完整信息一致且无歧义的商品自动采用，其余请确认。' if total else f'2026物品总表未找到：{query}（规格：{row.get("spec") or "未提供"}）。请人工补充，不会转去用友查询。',
        'warnings': ['仅查询所选知识库；明确匹配后采用其参考价众数，同频取先出现值；保存前仍需整单确认。' if source_label else '仅查询本机2026工作表快照；明确匹配后采用其贵昌95折众数价，同频取先出现值；保存前仍需整单确认。'],
        'sourceSha256': db['sourceSha256']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='建立仅供本机使用的2026商品知识库')
    parser.add_argument('source')
    args = parser.parse_args()
    print(json.dumps(build(args.source), ensure_ascii=False, indent=2))

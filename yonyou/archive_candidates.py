"""Operator-facing candidates using the existing read-only product transport.

No credentials, arbitrary upstream routes, save calls, or invented identity mapping.
Ranking describes similarity within the fetched page, not verified SKU identity.
"""
import re
import unicodedata
import copy
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from difflib import SequenceMatcher
import knowledge_bases

KINDS = {'product', 'customer', 'invoiceCustomer', 'salesOrg', 'settlementOrg',
         'stockOrg', 'department', 'salesperson', 'transaction', 'currency',
         'natCurrency', 'exchangeRateType'}


class ProductSearchCache:
    """Per-Gateway bounded raw results; candidates must still be ranked per row."""
    def __init__(self, clock=time.time, ttl=120, capacity=64, max_bytes=16_000_000):
        self.clock, self.ttl, self.capacity, self.max_bytes = clock, ttl, capacity, max_bytes
        self.lock = threading.RLock()
        self.entries, self.pending = OrderedDict(), {}
        self.instance = None
        self.generation = 0

    def get(self, transport, query, page, header):
        status = transport('/api/status')
        instance = status.get('serviceInstanceId') if isinstance(status, dict) else None
        if (not isinstance(status, dict) or status.get('appId') != 'guichang-yonyou-import-demo'
                or status.get('configured') is not True or not isinstance(instance, str) or not instance):
            raise ValueError('用友连接未就绪，请先连接后查询商品')
        # Include the full order header to prevent crossing customer/organization scopes.
        key = (instance, query, page, json.dumps(header, sort_keys=True, ensure_ascii=False))
        with self.lock:
            if self.instance != instance:
                self.entries.clear()
                self.instance = instance
                self.generation += 1
            generation = self.generation
            now = self.clock()
            for old in list(self.entries):
                if now - self.entries[old][0] >= self.ttl: del self.entries[old]
            if key in self.entries:
                self.entries.move_to_end(key)
                return copy.deepcopy(self.entries[key][1])
            flight_key = (generation, key)
            future = self.pending.get(flight_key)
            owner = future is None
            if owner:
                if len(self.pending) >= self.capacity:
                    raise ValueError('商品查询正在处理中，请稍后再试')
                future = self.pending[flight_key] = Future()
        if not owner:
            return copy.deepcopy(future.result(timeout=110))
        try:
            result = transport('/api/products', {'name': query, 'pageIndex': page})
            product_candidates(result, query, page=page)  # Validate before caching.
            size = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
            with self.lock:
                if generation == self.generation and size <= self.max_bytes:
                    self.entries[key] = (self.clock(), copy.deepcopy(result), size)
                    while len(self.entries) > self.capacity or sum(v[2] for v in self.entries.values()) > self.max_bytes:
                        self.entries.popitem(last=False)
                future.set_result(result)
            return copy.deepcopy(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock: self.pending.pop(flight_key, None)


def text(value):
    return value.strip() if isinstance(value, str) else ''


def identity(value):
    # Refuse already-rounded floats; retain all digits returned by existing service.
    if type(value) is int: value = str(value)
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{1,20}', value) else ''


def norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text(value)).casefold())


def product_candidates(result, query, row=None, page=1):
    if not isinstance(result, dict) or str(result.get('code')) != '200':
        raise ValueError('用友商品查询未成功；请核实接口授权或连接状态')
    data = result.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('recordList'), list):
        raise ValueError('用友商品返回结构不符，不能可靠生成候选')
    rows = data['recordList']
    if len(rows) > 200: raise ValueError('商品候选响应超出单页限制')
    row = row if isinstance(row, dict) else {}
    candidates = []
    for product in rows:
        if not isinstance(product, dict): continue
        pid, name = identity(product.get('id')), text(product.get('name'))
        if not pid or not name: continue
        unit_id = identity(product.get('unitId'))
        unit = text(product.get('unitName'))
        spec = ' / '.join(dict.fromkeys(v for v in (text(product.get('model')), text(product.get('modelDescription'))) if v))
        brand = text(product.get('manufacturer'))
        code = text(product.get('code'))
        warnings = []
        disabled = bool(re.search(r'禁用|停用|作废', name)) or product.get('stopStatus') is True or str(product.get('enable', '1')) in ('0', '2') or str(product.get('dr', '0')) == '1'
        if disabled: warnings.append('档案标记停用或名称含禁用提示，不可选用')
        if not unit_id or not unit: warnings.append('档案未返回完整计量单位，需补充核实')
        if text(row.get('unit')) and norm(row['unit']) != norm(unit):
            warnings.append('需求单位与档案单位不同；不得直接沿用数量，需确认换算')
        if text(row.get('spec')) and norm(row['spec']) not in norm(spec): warnings.append('需求规格与候选规格需人工核对')
        warnings.append('本接口未确认税率/税档案和成交价，不自动带入')
        score = round(70 * SequenceMatcher(None, norm(query), norm(name)).ratio())
        reasons = ['名称相似度排序']
        if norm(query) == norm(name): reasons = ['名称一致']
        if norm(row.get('spec')) and norm(row['spec']) in norm(spec):
            score += 20; reasons.append('规格文本包含需求')
        if norm(row.get('unit')) and norm(row['unit']) == norm(unit):
            score += 5; reasons.append('单位一致')
        if norm(row.get('brand')) and norm(row['brand']) == norm(brand):
            score += 5; reasons.append('品牌一致')
        patch = {'productId': pid, 'name': name, 'productCode': code, 'spec': spec,
                 'unit': unit, 'unitId': unit_id}
        candidates.append({'id': pid, 'productId': pid, 'name': name, 'code': code,
            'sourceType': 'yonyou', 'sourceLabel': '用友商品档案',
            'spec': spec, 'brand': brand, 'unit': unit, 'unitId': unit_id, 'rowPatch': patch,
            'selectable': not disabled and bool(unit_id and unit),
            'warnings': warnings, 'matchScore': score, 'matchReasons': reasons})
    candidates.sort(key=lambda item: (not item['selectable'], -item['matchScore'], item['code']))
    count = data.get('recordCount')
    try: total = int(count); more = page * 20 < total
    except (ValueError, TypeError): total = None; more = len(rows) == 20
    return {'available': True, 'candidates': candidates, 'pageIndex': page, 'hasMore': more,
        'recordCount': total, 'warnings': ['仅对本页候选排序，不代表唯一匹配；可缩小关键词或查看下一页。',
        '选择商品后需重新核对规格、单位和价格；不会保存订单。']}


def search(transport, data, product_cache=None):
    if not isinstance(data, dict): raise ValueError('查询条件需要对象')
    kind, query = data.get('kind'), text(data.get('query'))
    if kind not in KINDS: raise ValueError('不支持的档案类型')
    if not 1 <= len(query) <= 100: raise ValueError('请填写1至100字的查询名称')
    page = data.get('pageIndex', 1)
    if type(page) is not int or not 1 <= page <= 100: raise ValueError('页号必须为1至100的整数')
    source = data.get('searchSource', 'catalog')
    if source not in ('auto', 'catalog', 'yonyou'): raise ValueError('商品查询来源无效')
    if source == 'auto' and page != 1: raise ValueError('翻页时请保持首次查询来源')
    if kind != 'product':
        return {'available': False, 'candidates': [], 'hasMore': False, 'pageIndex': page,
            'warnings': [], 'message': '此基础档案的名称查询尚未接入；可先填写名称保存本机预置，不能将未关联档案的配置用于真实录单。'}
    if 'row' in data and not isinstance(data['row'], dict): raise ValueError('商品信息格式无效')
    row = dict(data.get('row', {}))
    if 'spec' in data:
        if not isinstance(data['spec'], str) or len(data['spec']) > 200:
            raise ValueError('规格型号必须为不超过200字的文本')
        row['spec'] = data['spec']
    header = data.get('header', {})
    if not isinstance(header, dict): raise ValueError('订单信息格式无效')
    if source != 'yonyou':
        local = knowledge_bases.search(header.get('knowledgeBaseId'), query, row, page)
        if not isinstance(local, dict) or not isinstance(local.get('candidates'), list):
            raise ValueError('知识库查询结果无效，请检查知识库后重试')
        if not (source == 'auto' and local.get('available') is True
                and not local['candidates'] and local.get('hasMore') is False):
            return {**local, 'searchSource': 'catalog'}
    else:
        knowledge_bases.resolve(header.get('knowledgeBaseId'))
    result = (product_cache.get(transport, query, page, header) if product_cache else
              transport('/api/products', {'name': query, 'pageIndex': page}))
    return {**product_candidates(result, query, row, page), 'searchSource': 'yonyou', 'sourceType': 'yonyou'}

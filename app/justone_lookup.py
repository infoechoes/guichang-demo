"""JustOne search/detail adapter. Credentials and provider URLs stay server-side.

Search prices and SKU prices are reference fields, never automatic quotes.
One in-flight upstream request per account token; no automatic retry or fallback.
"""
import copy
import datetime as dt
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://api.justoneapi.com'
SOURCES = {'justone-jd': ('jd', '京东'), 'justone-taobao': ('taobao', '淘宝/天猫')}
ERRORS = {100: 'Token 无效', 202: '该商品暂不支持详情查询', 301: '平台采集失败',
          302: '达到并发限制', 303: '试用或调用额度不足', 400: '查询参数不被接口支持',
          500: '数据源暂不可用', 600: '接口未开通', 601: '试用额度耗尽或余额不足',
          602: 'Token 调用预算不足'}


def money(value):
    # Do not turn masked prices, ranges, booleans or integer-cent fields into yuan.
    if isinstance(value, bool) or not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(value or '')):
        return None
    n = float(value)
    return n if math.isfinite(n) and n > 0 else None


def text(value, limit=500):
    return str(value or '')[:limit]


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JustOneLookup:
    def __init__(self, config=None, transport=None):
        self.config = Path(config) if config else None
        self.transport = transport or self._http
        self.lock = threading.RLock()
        self.slot = threading.BoundedSemaphore(1)
        self.items, self.cache, self.blocked = {}, {}, {}

    def _token(self):
        token = os.environ.get('JUSTONEAPI_TOKEN', '')
        if not token and self.config:
            try:
                token = json.loads(self.config.read_text(encoding='utf-8-sig')).get('token', '')
            except (OSError, ValueError):
                pass
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{12,200}', token):
            raise ValueError('JustOne 尚未配置，请联系维护人员；不会改用其他来源冒充结果')
        return token

    def _http(self, endpoint, params):
        query = urllib.parse.urlencode({**params, 'token': self._token()})
        request = urllib.request.Request(BASE + endpoint + '?' + query, headers={'Accept': 'application/json'})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            return json.loads(response.read(8_000_000).decode('utf-8-sig'))

    def call(self, endpoint, params):
        if any(self.blocked.get(k, 0) > time.time() for k in ('all', endpoint)):
            raise ValueError('JustOne 此入口因鉴权、额度或频率限制已暂停5分钟，请检查控制台')
        if not self.slot.acquire(blocking=False):
            raise ValueError('JustOne 试用并发为1，另一个请求尚未结束，请稍后再试')
        try:
            try:
                response = self.transport(endpoint, params)
            except ValueError as error:
                if str(error).startswith('JustOne 尚未配置'):
                    raise
                raise ValueError('JustOne 返回格式异常；未生成替代价格') from None
            except Exception:
                # Never expose exceptions containing token-bearing request URLs.
                raise ValueError('JustOne 连接超时或暂不可用；未自动重试') from None
            if not isinstance(response, dict):
                raise ValueError('JustOne 返回格式异常')
            code = response.get('code')
            if code != 0:
                if code in (100, 302, 303, 600, 601, 602):
                    self.blocked['all' if code in (100, 302, 303, 601, 602) else endpoint] = time.time() + 300
                raise ValueError('JustOne：' + ERRORS.get(code, '未返回有效数据') + '（状态 %s）' % text(code, 20))
            if not isinstance(response.get('data'), dict):
                raise ValueError('JustOne 返回空数据或格式异常')
            return response
        finally:
            self.slot.release()

    def search(self, owner, data):
        query, source = str(data.get('query', '')).strip(), data.get('platform')
        if source not in SOURCES or not query or len(query) > 120 or any(ord(c) < 32 for c in query):
            raise ValueError('请输入1–120字商品名称、品牌或型号')
        now = time.time()
        key = (owner, source, query)
        with self.lock:
            self.items = {k: v for k, v in self.items.items() if v['expires'] > now}
            self.cache = {k: v for k, v in self.cache.items() if v['expires'] > now}
            if key in self.cache:
                return {**copy.deepcopy(self.cache[key]['value']), 'cached': True}
        platform, label = SOURCES[source]
        params = {'keyword': query, 'page': 1}
        if platform == 'taobao':
            params['sort'] = '_coefp'
        start = time.monotonic()
        result = self.call('/api/' + platform + '/search-item-list/v1', params)
        d = result['data']
        values = d.get('products') if platform == 'jd' else d.get('model', {}).get('itemList')
        if not isinstance(values, list):
            raise ValueError('JustOne 未返回有效商品列表')
        at, rows = stamp(), []
        for v in values[:20]:
            if not isinstance(v, dict):
                continue
            item_id = str(v.get('id' if platform == 'jd' else 'itemId', ''))
            if not re.fullmatch(r'\d{1,25}', item_id):
                continue
            item_key = 'jo-' + secrets.token_urlsafe(18)
            price = money(v.get('price' if platform == 'jd' else 'priceZKYuanDouble'))
            original = money(v.get('priceYuanDouble')) if platform == 'taobao' else None
            view = {'id': item_key, 'platform': label, 'source': 'JustOne', 'title': text(v.get('title' if platform == 'jd' else 'itemName')),
                    'shop': text(v.get('shopName'), 150), 'sourceProductId': item_id,
                    'referencePrice': price, 'sourceOriginalPrice': original, 'queriedAt': at,
                    'providerRecordTime': text(result.get('recordTime')), 'sourceUpdatedAt': None,
                    'verified': False, 'skuStatus': '搜索展示价，具体SKU待核对', 'shipping': '未提供',
                    'conditions': '搜索展示价；会员、优惠及运费条件待核对',
                    'link': 'https://item.jd.com/' + item_id + '.html' if platform == 'jd' else
                            'https://item.' + ('tmall' if str(v.get('userType')) == '1' else 'taobao') + '.com/item.htm?id=' + item_id}
            with self.lock:
                self.items[item_key] = {'owner': owner, 'platform': platform, 'itemId': item_id, 'view': view, 'expires': now + 1800}
            rows.append(view)
        value = {'query': query, 'platform': 'JustOne · ' + label, 'items': rows, 'queriedAt': at,
                 'cached': False, 'elapsed': round(time.monotonic() - start, 2),
                 'message': '候选需核对SKU；接口采集时间不代表原平台数据更新时间' if rows else '未找到候选，可修改检索词'}
        with self.lock:
            self.cache[key] = {'expires': now + 60, 'value': copy.deepcopy(value)}
        return value

    def detail(self, owner, data):
        key = str(data.get('id', ''))
        with self.lock:
            item = copy.deepcopy(self.items.get(key))
        if not item or item['owner'] != owner or item['expires'] < time.time():
            raise ValueError('候选已过期或不属于当前账号，请重新搜索')
        if item.get('detailExpires', 0) > time.time():
            return {'item': item['detail'], 'cached': True}
        platform = item['platform']
        response = self.call('/api/' + platform + '/get-item-detail/v1', {'itemId': item['itemId']})
        d, v, at = response['data'], item['view'], stamp()
        v.update({'detailCheckedAt': at, 'providerRecordTime': text(response.get('recordTime')),
                  'skuOptions': [], 'warnings': [], 'verified': False,
                  'linkWarning': '请核对原页所选SKU、价格条件及截图；接口未提供可确认的原平台更新时间'})
        if platform == 'jd':
            product = d.get('product') or {}
            if str(product.get('skuId', '')) != item['itemId']:
                raise ValueError('JustOne 详情商品编号不一致或详情缺失，未采用')
            v.update({'title': text(product.get('skuName')) or v['title'], 'brand': text(product.get('brandName')),
                      'model': text(product.get('model')), 'skuStatus': str(product['skuId']),
                      'spec': '；'.join(text(product.get(k)) for k in ('model', 'color', 'size') if product.get(k))})
            raw = (d.get('priceFloor') or {}).get('price')
            v['detailPrice'] = money(raw)
            if v['detailPrice'] is None:
                v['warnings'].append('京东详情价格被遮挡或未提供；搜索价不能当作已核实SKU现价')
            v['conditions'] = '；'.join(filter(None, [text(d.get('bizMsg')), text((d.get('commonInfo') or {}).get('priceLoginText')), '运费与地区条件待核对']))
            v['skuOptions'] = [{'sku': str(product['skuId']), 'spec': v['spec'], 'price': v['detailPrice'], 'selectable': bool(v['spec'])}]
        else:
            product = d.get('item') or {}
            if str(product.get('itemId', '')) != item['itemId']:
                raise ValueError('JustOne 详情商品编号不一致或详情缺失，未采用')
            v['title'] = text(product.get('title')) or v['title']
            v['shop'] = text((d.get('seller') or {}).get('shopName'), 150) or v['shop']
            pricing, sku_data = d.get('itemPrice') or {}, d.get('itemSkuDO') or {}
            v['promotionEnd'] = text(pricing.get('endTime'))
            v['priceRange'] = text(pricing.get('price'))
            try:
                end = dt.datetime.fromisoformat(v['promotionEnd']).replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                if end < dt.datetime.now(dt.timezone.utc):
                    v['warnings'].append('接口活动结束时间已过；活动价不能作为当前有效优惠')
            except ValueError:
                pass
            names = {}
            for prop in sku_data.get('skuPropertyList') or []:
                for value in prop.get('propertyValues') or []:
                    names[str(prop.get('pid')) + ':' + str(value.get('vid'))] = text(value.get('name'))
            skus = sku_data.get('skuList') or {}
            if not isinstance(skus, dict):
                raise ValueError('JustOne SKU详情格式异常')
            for sku_id, sku in list(skus.items())[:200]:
                parts = str(sku.get('propPath', '')).split(';')
                complete = all(p in names for p in parts)
                v['skuOptions'].append({'sku': str(sku.get('skuId') or sku_id),
                    'spec': '；'.join(names.get(p, '未知规格 ' + p) for p in parts),
                    'price': money(sku.get('price')), 'selectable': complete,
                    'conditions': '接口SKU标价；优惠、税费和最终成交价待核对'})
            v['skuStatus'] = '%s 个SKU，请明确选择型号与包装' % len(v['skuOptions'])
            v['shipping'] = '接口标记包邮，适用地区待核对' if d.get('freeShipping') is True else '接口未确认包邮或未给运费金额'
            v['conditions'] = 'SKU标价与活动价分开；不自动采用同系列最低价'
        with self.lock:
            if key in self.items:
                self.items[key].update({'detail': copy.deepcopy(v), 'detailExpires': time.time() + 60})
        return {'item': v, 'cached': False}

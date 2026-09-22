"""Read-only Maishou candidate lookup. Prices never imply verified SKU quotes."""
import concurrent.futures,datetime,json,os,secrets,threading,time,urllib.request,urllib.parse
from justone_lookup import JustOneLookup
from pathlib import Path
from price_batch_bridge import PriceBatchBridge
VERSION='20260918.2'
BASE='https://appapi.maishou88.com/api/'
SOURCES={'jd':('京东','2'),'taobao':('淘宝/天猫','1')}
class PriceLookup:
 def __init__(self,transport=None,justone_config=None):
  self.batch=PriceBatchBridge(Path(justone_config).parent if justone_config else Path("multiuser-state"))
  self.justone=JustOneLookup(justone_config)
  self.transport=transport or self._http;self.lock=threading.RLock();self.items={};self.cache={};self.blocked={};self.slots=threading.BoundedSemaphore(3)
 def _http(self,url,data,form=False):
  body=urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
  req=urllib.request.Request(url,body,{'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json','Accept':'application/json','Referer':'https://hnbc018.kuaizhan.com/','User-Agent':'Mozilla/5.0'})
  with urllib.request.urlopen(req,timeout=15) as r:return json.loads(r.read(4_000_000).decode('utf-8-sig'))
 def call(self,source,url,data,form=False):
  if self.blocked.get(source,0)>time.time():raise ValueError('该平台要求授权或验证，已暂停请求，请稍后或联系维护人员')
  try:r=self.transport(url,data,form)
  except Exception:raise ValueError('数据源连接超时或暂不可用，请稍后重试') from None
  if not isinstance(r,dict):raise ValueError('数据源返回格式异常')
  message=str(r.get('message',''))
  if r.get('code') in (206,401,403) or any(x in message for x in ['未授权','未登录','验证码','验证失败']):
   self.blocked[source]=time.time()+300;raise ValueError('该平台要求授权或验证，已暂停请求5分钟')
  if r.get('code')!=200:raise ValueError('数据源未返回有效结果（业务状态 %s）'%str(r.get('code','未知'))[:80])
  return r.get('data')
 def params(self):return {'inviteCode':os.environ.get('MAISHOU_INVITE_CODE','6110440'),'openid':'','user_id':''}
 def normalize(self,v,source,owner,stamp):
  if not isinstance(v,dict) or not v.get('goodsId'):return None
  key=secrets.token_urlsafe(18)
  result={'id':key,'platform':v.get('shopTypeName') or v.get('platformName') or SOURCES[source][0],'title':str(v.get('title',''))[:500],'shop':str(v.get('shopName',''))[:150], 'referencePrice':v.get('actualPrice'),'sourceOriginalPrice':v.get('originalPrice'),'couponAmount':v.get('couponPrice'),'couponThreshold':v.get('couponConditions'),'couponStart':v.get('couponStartTime'),'couponEnd':v.get('couponEndTime'),'queriedAt':stamp,'source':'买手','verified':False,'skuStatus':'未核实具体SKU','shipping':'未提供','conditions':'优惠适用条件待核实','link':None}
  with self.lock:self.items[key]={'owner':owner,'source':source,'goodsId':str(v['goodsId']),'expires':time.time()+1800,'view':result}
  return result
 def search(self,owner,data):
  if str(data.get('platform','')).startswith('justone-'):return self.justone.search(owner,data)
  query=str(data.get('query','')).strip();source=data.get('platform','jd')
  if not query or len(query)>120:raise ValueError('请输入1–120字商品名称、品牌或型号')
  if source not in SOURCES:raise ValueError('目前仅支持京东、淘宝/天猫；拼多多待授权')
  now=time.time();key=(owner,source,query)
  with self.lock:
   self.items={k:v for k,v in self.items.items() if v['expires']>now};self.cache={k:v for k,v in self.cache.items() if v['expires']>now}
   if key in self.cache:return {**self.cache[key]['value'],'cached':True}
  if not self.slots.acquire(blocking=False):raise ValueError('当前有其他查价请求，请稍后再试')
  try:
   start=time.monotonic();r=self.call(source,BASE+'v1/homepage/searchList',{**self.params(),'keyword':query,'sourceType':SOURCES[source][1],'page':1,'isCoupon':0,'order':'desc'},True)
   if not isinstance(r,list):raise ValueError('数据源未返回商品列表')
   stamp=datetime.datetime.now(datetime.timezone.utc).isoformat();rows=[self.normalize(v,source,owner,stamp) for v in r[:20]];rows=[v for v in rows if v]
   value={'query':query,'platform':SOURCES[source][0],'items':rows,'queriedAt':stamp,'cached':False,'elapsed':round(time.monotonic()-start,2),'message':'未找到候选，可简化关键词再查一次' if not rows else '参考价；采用前核对规格、优惠条件及运费'}
   with self.lock:self.cache[key]={'expires':now+60,'value':value}
   return value
  finally:self.slots.release()
 def detail(self,owner,data):
  if str(data.get('id','')).startswith('jo-'):return self.justone.detail(owner,data)
  key=str(data.get('id',''))
  with self.lock:item=self.items.get(key)
  if not item or item['expires']<time.time() or item['owner']!=owner:raise ValueError('候选已过期或不属于当前账号，请重新搜索')
  if not self.slots.acquire(blocking=False):raise ValueError('当前有其他查价请求，请稍后再试')
  try:
   source=item['source'];params={**self.params(),'goodsId':item['goodsId'],'sourceType':SOURCES[source][1],'supplierCode':'','activityId':'','isShare':'1','token':''}
   r=self.call(source,BASE+'v3/goods/detail',{**params,'keyword':'','usageScene':5})
   if not isinstance(r,dict) or not r.get('title'):raise ValueError('未取得有效商品详情')
   stamp=datetime.datetime.now(datetime.timezone.utc).isoformat();v=self.normalize(r,source,owner,stamp);v['id']=key;v['sourceProductId']=str(r.get('spuid') or '')
   v['detailCheckedAt']=stamp;v['linkWarning']='推广链接可能跳到同系列其他规格，请在原页面重新选择型号'
   try:
    link=self.call(source,'https://msapi.maishou88.com/api/v1/share/getTargetUrl',{**params,'isDirectDetail':0}) or {}
    url=link.get('appUrl') or link.get('h5Url') or '';parsed=urllib.parse.urlsplit(url);host=parsed.hostname or ''
    if parsed.scheme=='https' and any(host==h or host.endswith('.'+h) for h in ['jd.com','taobao.com','tmall.com','tb.cn']):v['link']=url
    else:v['linkError']='未返回可用网页商品链接'
   except ValueError as e:v['linkError']=str(e)
   # Detail is still a reference record: provider does not expose selected SKU pricing.
   with self.lock:self.items[key]['view']=v
   return {'item':v}
  finally:self.slots.release()
 def xinfadi(self,data):
  query=str(data.get('query','')).strip();date=str(data.get('date') or 'latest')
  if not query or len(query)>80 or any(ord(c)<32 for c in query):raise ValueError('请输入1–80字食材名称')
  if date!='latest':
   try:parsed=datetime.date.fromisoformat(date)
   except ValueError:raise ValueError('请输入有效行情日期') from None
   if parsed>datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date():raise ValueError('不能查询未来日期')
  if not self.slots.acquire(blocking=False):raise ValueError('当前有其他查价请求，请稍后再试')
  try:
   from xinfadi_adapter import lookup
   try:result=lookup(query,date)
   except Exception:raise ValueError('新发地行情服务暂不可用，请稍后重试；本次未生成替代价格') from None
   if not isinstance(result,dict) or not isinstance(result.get('items'),list):raise ValueError('新发地行情格式异常')
   return result
  finally:self.slots.release()
 def dispatch(self,owner,path,data):
  if path.startswith('/api/price-lookup/batch/'):return self.batch.dispatch(owner,path.rsplit('/',1)[-1],data)
  if path=='/api/price-lookup/xinfadi':return self.xinfadi(data)
  if path=='/api/price-lookup/search':return self.search(owner,data)
  if path=='/api/price-lookup/detail':return self.detail(owner,data)
  raise ValueError('查价操作不存在')

"""Adapter for the frozen pricing engine. One account, same order rows, no ERP writes."""
import copy, hashlib, importlib.util, io, json, sys, threading, uuid
from pathlib import Path
from decimal import Decimal
from workflow_excel import tables_excel
class OrderPricing:
 def __init__(self,store):
  self.store=store;self.lock=threading.RLock();self.sessions={}
  root=Path(__file__).parent/'price_batch';name='gc_order_prices_'+uuid.uuid4().hex
  spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
  pkg=importlib.util.module_from_spec(spec);sys.modules[name]=pkg;spec.loader.exec_module(pkg)
  self.engine=importlib.import_module(name+'.batch_app');rules=importlib.import_module(name+'.pricing')
  rules.STORE=store.root.parent/'price-projects.json';rules.STORE.parent.mkdir(parents=True,exist_ok=True)
 def call(self,action,data=None,raw=None):
  engine=self.engine
  class Adapter(engine.App):
   def respond(self,status,value,content_type='application/json; charset=utf-8'):
    self.result=(status,value)
  h=object.__new__(Adapter);h.path='/api/'+action;h.result=None
  body=raw if raw is not None else json.dumps(data or {}).encode();h.headers={'Content-Length':str(len(body)),'Host':'localhost'};h.rfile=io.BytesIO(body)
  if action=='projects' and data is None:h.do_GET()
  else:h.do_POST()
  status,value=h.result
  if status!=200:raise ValueError(value.get('error','价格处理失败'))
  return value
 def dispatch(self,action,data):
  with self.lock:
   if action=='projects':return self.call('projects',data if data else None)
   if action=='start':
    record=self.store.get(data['batchId']);self.store.require_active(record)
    if record.get('receipt'):raise ValueError('已有保存回执，不能重新定价')
    if record.get('draftHash')!=data.get('draftHash'):raise ValueError('请先保存当前草稿再查价')
    draft=record.get('draft');rows=draft['rows']
    if record.get('splitReservations'):raise ValueError('请在独立子批次查价，母批次保留已拆分行')
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.title='本单商品';sheet.append(['品名','规格','单位','数量','单价'])
    for r in rows:sheet.append([r.get(k) if str(r.get(k,'')).strip() else None for k in ('name','spec','unit','quantity','price')])
    stream=io.BytesIO();book.save(stream);raw=stream.getvalue()
    price_file=self.store.folder(record['batchId'])/('price-input-'+record['draftHash']+'.xlsx')
    if price_file.exists():raw=price_file.read_bytes()
    else:
     from workflow_service import atomic_write
     atomic_write(price_file,raw)
    uploaded=self.call('upload',raw=raw);sid=uploaded['id']
    self.call('setup',{'id':sid,'mapping':{'name':0,'spec':1,'unit':2,'quantity':3},'date':data.get('date',''),'project_id':data.get('projectId','')})
    payload=self.call('handoff',{'id':sid})
    if payload.get('contract_version')!=2 or payload['returned_rows']!=len(rows):raise ValueError('价格原行接入不完整')
    if payload.get('project') and payload['project']['purpose']!='sales':raise ValueError('采购参考价不能直接作为本单销售价，请选择销售用途规则')
    bindings={r['sourceLineId']:e for r,e in zip(rows,payload['rows'])}
    self.sessions[sid]={'batchId':record['batchId'],'draftHash':record['draftHash'],'rows':copy.deepcopy(rows),'bindings':bindings}
    return {'id':sid,'rowCount':len(rows),'rows':[{'sourceLineId':r['sourceLineId'],'name':r.get('name'),'unit':r.get('unit')} for r in rows]}
   sid=data.get('id');context=self.sessions.get(sid)
   if not context or sid not in self.engine.SESSIONS:raise ValueError('查价会话已失效，请重新从本单发起')
   record=self.store.get(context['batchId']);self.store.require_active(record)
   if record.get('draftHash')!=context['draftHash'] or record.get('receipt'):raise ValueError('订单已修改，请保存后重新查价；旧价格结果不回填')
   ids=[r['sourceLineId'] for r in context['rows']]
   if action in ('query','confirm'):
    ident=data.get('sourceLineId')
    if ident not in ids:raise ValueError('原订单行不存在')
    bound=context['bindings'][ident]
    return self.call(action,{**data,'id':sid,'row':bound['row'],'source_line_id':bound['source_line_id'],'calculation_batch_id':bound['calculation_batch_id']})
   if action=='apply':
    payload=self.call('handoff',{'id':sid})
    if payload.get('contract_version')!=2 or payload.get('scope')!='all' or payload['returned_rows']!=len(ids):raise ValueError('价格交接行数不完整，未应用')
    by_source={e['source_line_id']:e for e in payload['rows']}
    if len(by_source)!=len(ids):raise ValueError('价格原行ID重复，未应用')
    draft=copy.deepcopy(record['draft']);applied=0
    for row in draft['rows']:
     ident=row['sourceLineId'];bound=context['bindings'][ident];evidence=by_source.get(bound['source_line_id'])
     if not evidence or any(evidence.get(k)!=bound.get(k) for k in ('source_batch_id','source_line_id','input_fingerprint','calculation_batch_id','name','spec','unit','quantity')):raise ValueError('价格来源、商品或单位已变化，旧价不能采用')
     if evidence['protected']:
      if Decimal(str(evidence['original_price']))!=Decimal(str(row['price'])):raise ValueError('原单保护价不一致')
     elif evidence['can_fill_price']:
      value=evidence['price_decimal'];price=Decimal(str(value))
      if not price.is_finite() or price<0:raise ValueError('参考价无效')
      row.update(price=str(value),priceSource=evidence.get('source') or (evidence.get('selected_item') or {}).get('reference_url') or '本单已确认价格规则',priceNeedsReview=False);applied+=1
     # Pending rows and original prices stay intact; every row retains its handoff status.
     row['priceEvidence']={**evidence,'sourceLineId':ident,'sourceSheet':row.get('sourceSheet',''),'sourceRow':row.get('sourceRow',''),'settlement_confirmed':False}
    draft['header']['reviewed']=False
    return {'draft':draft,'appliedCount':applied,'totalCount':len(ids),'counts':payload['counts'],'settlement_confirmed':False}
   raise ValueError('未开放价格操作')

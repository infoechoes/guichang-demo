"""Existing-account adapter for food pricing; no order or ERP writes."""
import base64, importlib, importlib.util, io, json, re, sys, threading, uuid
from pathlib import Path

class PriceBatchBridge:
 def __init__(self,root):
  self.root=Path(root);self.contexts={};self.lock=threading.RLock()
 def engine(self,owner):
  if not re.fullmatch('[a-f0-9]{32}',owner):raise ValueError('账号无效')
  if owner not in self.contexts:
   root=Path(__file__).parent/'price_batch';name='gc_price_entry_'+uuid.uuid4().hex
   spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
   pkg=importlib.util.module_from_spec(spec);sys.modules[name]=pkg;spec.loader.exec_module(pkg)
   engine=importlib.import_module(name+'.batch_app');rules=importlib.import_module(name+'.pricing')
   rules.STORE=self.root/'users'/owner/'price-projects.json';rules.STORE.parent.mkdir(parents=True,exist_ok=True)
   self.contexts[owner]=engine
  return self.contexts[owner]
 def dispatch(self,owner,action,data):
  if action not in ('projects','upload','preview','setup','query','confirm','clear','print','handoff','export'):raise ValueError('查价操作不存在')
  with self.lock:
   engine=self.engine(owner)
   class Adapter(engine.App):
    def respond(self,status,value,content_type='application/json; charset=utf-8'):self.result=(status,value)
   h=object.__new__(Adapter);h.path='/api/'+action;h.result=None
   if action=='upload':
    try:body=base64.b64decode(data.get('file_base64',''),validate=True)
    except Exception:raise ValueError('上传文件格式无效') from None
    if len(body)>10_000_000:raise ValueError('Excel大小上限10MB')
   else:body=json.dumps(data or {},ensure_ascii=False).encode()
   h.headers={'Content-Length':str(len(body)),'Host':'localhost'};h.rfile=io.BytesIO(body)
   if action=='projects' and not data.get('project'):h.do_GET()
   else:h.do_POST()
   status,value=h.result
   if status!=200:raise ValueError(value.get('error','价格处理失败'))
   if isinstance(value,bytes):return {'download_base64':base64.b64encode(value).decode()}
   return value

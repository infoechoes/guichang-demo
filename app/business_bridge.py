"""Mount existing business workflows behind the workbench session.

Each account retains its own local working files. No upstream writes are made.
"""
import importlib.util
import io
import json
import mimetypes
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from multiuser_auth import AccessDenied

ROOT=Path(__file__).parent/'business_modules'
class BusinessModules:
    def __init__(self,root):
        self.root=Path(root);self.modules={};self.lock=threading.RLock()
    def module(self,user,kind):
        identity=user['id']
        if not re.fullmatch('[a-f0-9]{32}',identity):raise AccessDenied('账号无效')
        key=(identity,kind)
        with self.lock:
            if key not in self.modules:
                package='gc_business_'+identity+'_'+kind
                spec=importlib.util.spec_from_file_location(package,ROOT/kind/'__init__.py',submodule_search_locations=[str(ROOT/kind)])
                pkg=importlib.util.module_from_spec(spec);sys.modules[package]=pkg;spec.loader.exec_module(pkg)
                spec=importlib.util.spec_from_file_location(package+'.server',ROOT/kind/'server.py')
                mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
                mod.DATA=self.root/'users'/identity/'business'/kind
                mod.DATA.mkdir(parents=True,exist_ok=True)
                if kind=='food':mod.DB=mod.DATA/'demo.sqlite';mod.init(self.root/'business-samples'/'food40.json')
                else:
                    if kind=='tax':
                        mod.SHARED_DB=self.root/'tax-knowledge.sqlite3';mod.OWNER=identity;mod.ACTOR=user.get('display_name') or user.get('username') or identity
                        if mod.SAMPLE is None:mod.SAMPLE=self.root/'business-samples'/'tax-sample.json'
                    for folder in ['batches','uploads','exports']:(mod.DATA/folder).mkdir(exist_ok=True)
                self.modules[key]=mod
            return self.modules[key]
    def handle(self,outer,method):
        url=urlsplit(outer.path);parts=url.path.split('/',3)
        if len(parts)<3 or parts[2] not in ('food','tax','knowledge'):return outer.reply(404,{'error':'页面不存在'})
        kind=parts[2];path='/'+(parts[3] if len(parts)>3 else '')
        api=path.startswith('/api/');token=outer.session_token()
        user=outer.server.runtime.accounts.authenticate(token,outer.headers.get('X-CSRF-Token','') if method=='POST' else None)
        supplied=outer.headers.get('X-Business-Owner') or parse_qs(url.query).get('owner',[''])[0]
        if (api or path.startswith('/download/') or path=='/') and supplied!=user['id']:raise AccessDenied('账号已变化，请从工作台重新打开页面')
        if method=='GET' and not api and not path.startswith('/download/'):
            name=path.lstrip('/') or 'index.html'
            if name=='common.js':file=ROOT/name
            elif name=='shared.css':file=ROOT/name
            else:file=ROOT/kind/'static'/name
            if '/' in name or not file.is_file():return outer.reply(404,{'error':'页面不存在'})
            return outer.reply(200,file.read_bytes(),extra={'X-Frame-Options':'SAMEORIGIN','Content-Security-Policy':"frame-ancestors 'self'"},mime=mimetypes.guess_type(file.name)[0] or 'text/plain')
        if kind=='knowledge' and not path.startswith('/api/v2/'):return outer.reply(404,{'error':'接口不存在'})
        if method=='POST':
            size=int(outer.headers.get('Content-Length','0'))
            if not 0<size<=45*1024*1024 or outer.headers.get('Transfer-Encoding') or outer.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请求格式或大小无效')
        mod=self.module(user,'tax' if kind=='knowledge' else kind)
        class Mounted(mod.Handler):
            def local_request(self):pass # Workbench Host, Origin and session checks already ran.
            def send_response(self,status,*args):self.result_status=status
            def send_header(self,key,value):self.result_headers[key]=value
            def end_headers(self):pass
        h=object.__new__(Mounted);h.path=path+('?' + url.query if url.query and method=='GET' else '');h.headers=outer.headers
        h.rfile=outer.rfile;h.wfile=io.BytesIO();h.server=outer.server;h.result_status=200;h.result_headers={}
        (h.do_POST if method=='POST' else h.do_GET)()
        outer.server.runtime.accounts.authenticate(token,outer.headers.get('X-CSRF-Token','') if method=='POST' else None)
        return outer.reply(h.result_status,h.wfile.getvalue(),extra={k:v for k,v in h.result_headers.items() if k.lower() not in ('content-type','content-length')},mime=h.result_headers.get('Content-Type','application/json'))

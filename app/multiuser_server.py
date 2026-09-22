"""Multi-operator facade. Run on loopback behind an HTTPS reverse proxy.

Operators never receive ERP credentials or direct access to the connector.
The legacy single-user runtime and its files remain separate.
"""
import argparse
import base64
import importlib.util
import io
import json
import html
import mimetypes
import re
import sys
import ssl
import ipaddress
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs, urlencode
from quote_bridge import QuoteBridge
from price_lookup import PriceLookup
from tender_bridge import TenderBridge, TenderBridgeError
from multiuser_auth import Accounts, AccessDenied
from business_bridge import BusinessModules
from trial_diagnostics import BUILD, RequestLog

ROOT = Path(__file__).resolve().parents[1]
ERP = ROOT/'yonyou' if (ROOT/'yonyou').is_dir() else ROOT.parent/'yonyou-import'
sys.path.insert(0,str(ERP))
import knowledge_bases
from shared_knowledge import SharedKnowledge, inspect as inspect_knowledge
from order_submission import CONFIRM as SUBMIT_CONFIRM


def trusted_proxy_addresses(values=None):
    """Trust only explicitly configured same-host proxies, never an HTTP header."""
    result=set()
    for value in values or ():
        try:address=ipaddress.ip_address(value)
        except (ValueError,TypeError):raise ValueError('可信代理必须为单个回环IP地址') from None
        if not address.is_loopback:raise ValueError('可信代理仅支持同机回环IP地址')
        result.add(str(address))
    return frozenset(result)


class Runtime:
    def __init__(self, root, transport=None):
        self.root = Path(root).resolve()
        self.accounts = Accounts(self.root)
        self.knowledge = SharedKnowledge(self.root/'knowledge-bases')
        # Keep the existing government files in place. Food starts with an
        # independent, empty registry and never inherits the hospital catalogue.
        self.department_knowledge = {
            'gov': self.knowledge,
            'food': SharedKnowledge(self.root/'departments'/'food'/'knowledge-bases', department='food'),
        }
        self.transport, self.contexts, self.lock = transport, {}, threading.RLock()
        self.ocr_lock = threading.Lock()
        self.submit_plans = {}
        self.request_log = RequestLog(self.root)
        self.outcome_checks = {}
        self.outcome_lock = threading.Lock()
        self.quotes = QuoteBridge(self.root)
        self.price_lookup = PriceLookup(justone_config=self.root / 'justone.json')
        self.business = BusinessModules(self.root)
        self.tender = TenderBridge(self.root/'tender-bridge.json')

    def workspace(self, user):
        identity=user['id']
        if not re.fullmatch('[a-f0-9]{32}',identity): raise AccessDenied('账号无效')
        with self.lock:
            if identity not in self.contexts:
                name='operator_workflow_'+identity+'_'+uuid.uuid4().hex
                path=Path(__file__).with_name('workflow_service.py')
                spec=importlib.util.spec_from_file_location(name,path)
                module=importlib.util.module_from_spec(spec)
                sys.modules[name]=module
                spec.loader.exec_module(module)
                module.DATA_ROOT=self.root/'users'/identity
                module.EXPORTS=module.DATA_ROOT/'exports'
                module.STORE=module.WorkflowStore(module.EXPORTS)
                # Each module owns its store, lock, defaults and gateway; never
                # mutate the shared legacy module while requests are in flight.
                from integration_gateway import Gateway
                module._gateway=Gateway(transport=self.transport,state_dir=module.DATA_ROOT/'gateway',source_roots=[module.EXPORTS],order_namespace=identity)
                self.contexts[identity]=module
            return self.contexts[identity]

    def dispatch(self, user, method, path, data=None):
        department = user.get('department', 'gov')
        if department == 'bid':
            if path.startswith('/api/admin/'):
                return self._dispatch(user,method,path,data or {})
            if method=='GET' and path=='/api/v1/modules':
                return {'apiVersion':'1','modules':[{'id':'bid','name':'投标部','entry':'/tender/'}]}
            raise AccessDenied('此账号仅开放标书工作台')
        if department not in self.department_knowledge:
            raise AccessDenied('账号部门无效，请联系管理员')
        data = data or {}
        registry = self.department_knowledge[department]
        # ContextVar keeps simultaneous department requests isolated, including
        # calls buried inside defaults/profile validation and catalogue search.
        with knowledge_bases.using(registry):
            if path.startswith('/api/v1/'):
                return self.module_api(user, method, path, data, registry)
            # Existing browser clients retain their URLs, but scope is always
            # derived from the authenticated account, never a request field.
            if not path.startswith('/api/admin/'):
                self.check_department_fields(data, department)
            if department == 'food' and not (
                path.startswith('/api/admin/') or
                path in ('/api/knowledge-bases/inspect', '/api/knowledge-bases/publish',
                         '/api/workflow/knowledge-bases', '/api/workspace/knowledge-draft')
            ):
                raise AccessDenied('食材业务尚未接入此流程，请使用食材模块接口')
            return self._dispatch(user,method,path,data)

    @staticmethod
    def check_department_fields(data, department):
        for field in ('department', 'moduleId'):
            if field in data and data[field] != department:
                raise AccessDenied('不能访问其他部门的业务数据')

    def module_api(self, user, method, path, data, registry):
        """Versioned integration seam; no ERP writes or external demo proxy."""
        department = user.get('department', 'gov')
        self.check_department_fields(data, department)
        prefix = '/api/v1/modules/' + department
        if method == 'GET' and path == '/api/v1/modules':
            return {'apiVersion': '1', 'modules': [{
                'id': department, 'name': '政企事业部' if department == 'gov' else '食材部',
                'status': 'active' if department == 'gov' else 'integration_ready',
                'knowledgeScope': 'department', 'apiBase': prefix,
                'capabilities': {'knowledgeBases': True, 'knowledgeSearch': True,
                                 'orderWorkflow': department == 'gov',
                                 'externalOrderImport': False},
            }]}
        if not path.startswith(prefix + '/'):
            raise AccessDenied('未开放此模块接口或账号无权访问')
        endpoint = path[len(prefix):]
        if method == 'GET' and endpoint == '/knowledge-bases':
            return {**registry.catalogues(), 'apiVersion': '1', 'department': department}
        if method == 'POST' and endpoint in ('/knowledge-bases/inspect', '/knowledge-bases/publish'):
            return self._dispatch(user, method, '/api' + endpoint, data)
        if method == 'POST' and endpoint == '/knowledge-bases/search':
            query, row, page = data.get('query'), data.get('row', {}), data.get('page', 1)
            if not isinstance(query, str) or not query.strip() or len(query) > 300:
                raise ValueError('查询名称需为1至300字')
            if not isinstance(row, dict) or set(row) - {'name', 'spec', 'unit', 'sourceDemandUnit', 'sourcePrice'}:
                raise ValueError('商品查询字段无效')
            if any(not isinstance(v, str) or len(v) > 300 for v in row.values()):
                raise ValueError('商品查询字段需为不超过300字的文本')
            if type(page) is not int or not 1 <= page <= 1000:
                raise ValueError('查询页码无效')
            # Explicit selection is required by the versioned contract, even
            # for gov. A missing food catalogue must never fall back to gov.
            identity = data.get('knowledgeBaseId')
            if not isinstance(identity, str) or not identity:
                raise ValueError('请先选择本部门知识库')
            result = registry.search(identity, query.strip(), row, page)
            if department == 'food':
                # Ranking is reusable; food SKU/unit auto-adoption has not yet
                # been validated and must be implemented by its business module.
                result = {**result, 'candidates': [
                    {**candidate, 'autoAdoptable': False} for candidate in result.get('candidates', [])]}
                result['matchStatus'] = 'candidates' if result.get('found') else 'not_found'
                result['message'] = '本部门知识库候选，请核对商品、等级、规格及单位后确认。'
                result['warnings'] = ['参考价来自所选食材知识库；本接口不自动确定成交价或换算单位。']
            return {**result, 'apiVersion': '1', 'department': department,
                    'searchSource': 'knowledge_base', 'erpFallback': False}
        raise AccessDenied('未开放此模块接口')

    def _dispatch(self,user,method,path,data):
        if path.startswith('/api/admin/'):
            if user['role']!='admin': raise AccessDenied('仅管理员可管理账号或查看全员操作记录')
            if method=='GET' and path=='/api/admin/users': return {'users':self.accounts.list()}
            if method=='GET' and path=='/api/admin/audit': return {'records':self.accounts.recent_audit()}
            if method=='POST' and path=='/api/admin/users':
                account=self.accounts.create(data.get('username'),data.get('displayName'),data.get('password'),data.get('role','operator'),department=data.get('department','gov'))
                self.accounts.audit(user['id'],'create_account',account['id'],'success')
                return account
            if method=='POST' and path=='/api/admin/disable':
                self.accounts.disable(data.get('id'),user['id']);return {'disabled':True}
            if method=='POST' and path=='/api/admin/enable':
                self.accounts.enable(data.get('id'),user['id']);return {'enabled':True}
            if method=='POST' and path=='/api/admin/reset-password':
                self.accounts.admin_reset_password(data.get('id'),user['id'],data.get('password'))
                return {'reset':True,'sessionsRevoked':True}
            raise AccessDenied('未开放的管理操作')
        if path=='/api/knowledge-bases/inspect' and method=='POST': return inspect_knowledge(data)[2]
        if path=='/api/knowledge-bases/publish' and method=='POST':
            result=self.department_knowledge[user.get('department','gov')].publish(data,user)
            self.accounts.audit(user['id'],'publish_knowledge',result['id'],'success')
            return result
        if path=='/api/workflow/knowledge-bases' and method=='GET':
            return knowledge_bases.catalogues()
        if path=='/api/recognize' and method=='POST':
            if not self.ocr_lock.acquire(timeout=90): raise ValueError('识别排队超时，请稍后重试')
            try:
                import ocr_server
                encoded=data.get('image')
                if not isinstance(encoded,str) or len(encoded)>20_000_004:raise ValueError('单张图片最大15MB')
                raw=base64.b64decode(encoded,validate=True)
                rotation=data.get('rotation',0)
                if not raw or len(raw)>15_000_000 or type(rotation) is not int or rotation not in (0,90,180,270):raise ValueError('图片或旋转角度无效')
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width*image.height>25_000_000:raise ValueError('图片像素过大，请缩小至2500万像素以内')
                auto=data.get('autoRotate',True)
                if not isinstance(auto,bool):raise ValueError('旋转选项无效')
                return ocr_server.recognize(raw,rotation,auto_rotate=auto)
            finally:self.ocr_lock.release()
        workspace=self.workspace(user)
        if method=='POST' and sum(p.stat().st_size for p in workspace.DATA_ROOT.rglob('*') if p.is_file())+len(workspace.json_bytes(data))>1_000_000_000:
            raise ValueError('个人工作区空间已达1GB，请联系管理员整理后继续')
        if path=='/api/workspace/knowledge-draft':
            target=workspace.DATA_ROOT/'knowledge-draft.json'
            if method=='GET':return {'draft':json.loads(target.read_text(encoding='utf-8')) if target.exists() else None}
            if method!='POST':raise AccessDenied('未开放此操作')
            draft=data.get('draft')
            if draft is None:
                target.unlink(missing_ok=True)
                return {'saved':False}
            if not isinstance(draft,dict) or len(workspace.json_bytes(draft))>15_000_000:
                raise ValueError('知识库整理草稿格式无效或过大')
            # A saved editor draft is never a server-approved publication preview.
            allowed={'name','filename','file','sheets','selectedSheets','activeSheet'}
            if set(draft)-allowed:raise ValueError('整理草稿含不支持的字段，请重新整理')
            if not isinstance(draft.get('file'),str) or len(draft['file'])>13_400_000:
                raise ValueError('知识库最大10MB')
            if not isinstance(draft.get('sheets'),list) or len(draft['sheets'])>200:
                raise ValueError('整理工作表数据无效')
            for field,limit in (('name',200),('filename',255),('activeSheet',255)):
                if not isinstance(draft.get(field,''),str) or len(draft.get(field,''))>limit:
                    raise ValueError('整理草稿名称无效')
            names=[]
            for sheet in draft['sheets']:
                if not isinstance(sheet,dict) or not isinstance(sheet.get('sheet'),str) or not 1<=len(sheet['sheet'])<=255:
                    raise ValueError('整理工作表名称无效')
                names.append(sheet['sheet'])
                if set(sheet)-{'sheet','priceMode','autoClean','edits','excludedRows','includedRows','headerRow','headerDepth','mapping'}:
                    raise ValueError('整理工作表含不支持的字段')
                if sheet.get('priceMode','auto') not in ('auto','as_is','table_discount') or type(sheet.get('autoClean',True)) is not bool:
                    raise ValueError('整理价格选项无效')
                for field in ('mapping','edits'):
                    if not isinstance(sheet.get(field,{}),dict):raise ValueError('整理字段映射无效')
                if any(not isinstance(v,str) for v in sheet.get('mapping',{}).values()):raise ValueError('整理字段映射无效')
                for row in sheet.get('edits',{}).values():
                    if not isinstance(row,dict) or any(not isinstance(v,(str,int,float,bool)) and v is not None for v in row.values()):
                        raise ValueError('整理行修正无效')
                for field in ('includedRows','excludedRows'):
                    if not isinstance(sheet.get(field,[]),list) or any(type(v) is not int or v<1 for v in sheet.get(field,[])):
                        raise ValueError('整理行选择无效')
                for field,maximum in (('headerRow',10000),('headerDepth',3)):
                    if field in sheet and (type(sheet[field]) is not int or not 1<=sheet[field]<=maximum):raise ValueError('整理表头无效')
            selected=draft.get('selectedSheets',[])
            if len(set(names))!=len(names) or not isinstance(selected,list) or any(not isinstance(v,str) or v not in names for v in selected) or len(selected)>20 or len(set(selected))!=len(selected):
                raise ValueError('整理工作表选择无效')
            if draft.get('activeSheet') and draft['activeSheet'] not in names:raise ValueError('当前工作表无效')
            encoded=base64.b64decode(draft['file'],validate=True)
            if not encoded or len(encoded)>10_000_000:raise ValueError('知识库最大10MB')
            workspace.atomic_write(target,workspace.json_bytes(draft))
            self.accounts.audit(user['id'],'save_knowledge_draft','','success')
            return {'saved':True}
        outcome=re.fullmatch(r'/api/workflow/batches/([a-f0-9-]{36})/check-outcome',path)
        if method=='POST' and outcome:
            if data:raise ValueError('核查仅使用本批次的保存记录，不接受额外查询条件')
            from trial_recovery import check_outcome
            with self.outcome_lock:
                key=(user['id'],outcome[1])
                previous=self.outcome_checks.get(key)
                if previous and time.monotonic()-previous[0]<10:
                    current=workspace.STORE.get(outcome[1])
                    if current.get('receipt')==previous[1].get('receipt') and current.get('submission')==previous[1].get('submission'):
                        return previous[1]
                result=check_outcome(workspace,outcome[1])
                self.outcome_checks[key]=(time.monotonic(),result)
                if len(self.outcome_checks)>100:self.outcome_checks.pop(next(iter(self.outcome_checks)))
            self.accounts.audit(user['id'],'check_order_outcome',outcome[1],'read_only')
            return result
        if path=='/api/workspace/image-draft':
            target=workspace.DATA_ROOT/'image-draft.json'
            if method=='GET':return json.loads(target.read_text(encoding='utf-8')) if target.exists() else None
            images,rows=data.get('images'),data.get('rows')
            if not isinstance(images,list) or not isinstance(rows,list) or len(images)>100 or len(rows)>2000:raise ValueError('图片草稿格式无效')
            total=0
            for item in images:
                raw=base64.b64decode(item.get('fileBase64',''),validate=True)
                if not raw or len(raw)>15_000_000:raise ValueError('草稿单图最大15MB')
                total+=len(raw)
                if total>50_000_000:raise ValueError('多人版图片草稿总大小最大50MB，请分批处理')
            workspace.atomic_write(target,workspace.json_bytes(data))
            self.accounts.audit(user['id'],'save_image_draft','', 'success')
            return {'saved':True}
        match=re.fullmatch(r'/api/workflow/batches/([^/]+)/files/([^/]+)',path)
        if method=='GET' and match:
            return workspace.STORE.file(match[1],match[2])
        match=re.fullmatch(r'/api/workflow/batches/([^/]+)/(submit-preview|submit)',path)
        if method=='POST' and match:
            batch=workspace.STORE.get(match[1])
            receipt=batch.get('receipt') or {}
            if receipt.get('state')!='saved_verified':raise ValueError('本批次尚未保存并回读核对')
            if batch.get('submission'):return {'submission':batch['submission']}
            gateway=workspace.gateway()
            status=gateway.transport('/api/status')
            if not status.get('submitSupported'):raise ValueError('连接服务尚未加载提交功能，请由管理员更新连接服务')
            if match[2]=='submit-preview':
                result=gateway.transport('/api/submit-preview',{'digest':receipt['digest']})
                with self.lock:
                    self.submit_plans={k:v for k,v in self.submit_plans.items() if v['expires']>time.time()}
                    if len(self.submit_plans)>=100:raise ValueError('提交预览过多，请稍后重试')
                    self.submit_plans[result['token']]={'user':user['id'],'batch':batch['batchId'],'expires':time.time()+300}
                return result
            with self.lock:
                token=data.get('token')
                plan=self.submit_plans.get(token)
                if not plan or plan['user']!=user['id'] or plan['batch']!=batch['batchId'] or plan['expires']<=time.time():raise AccessDenied('提交确认不属于当前账号或已过期')
                if data.get('confirmation')!=SUBMIT_CONFIRM:raise ValueError('请明确确认提交')
                self.submit_plans.pop(token)
            self.accounts.audit(user['id'],'submit_order',receipt.get('code',''),'started')
            # Persist uncertainty before dispatch. A timeout or restart must not
            # make the button available to send the same order again.
            batch['submission']={'state':'submission_uncertain','message':'提交结果待核查，请勿重复提交'}
            workspace.STORE.persist(batch)
            try:
                result=gateway.transport('/api/submit',{'token':token,'confirmation':SUBMIT_CONFIRM})
                batch['submission']=result;workspace.STORE.persist(batch)
                self.accounts.audit(user['id'],'submit_order',receipt.get('code',''),result.get('state','unknown'))
                return {'submission':result}
            except Exception:
                self.accounts.audit(user['id'],'submit_order',receipt.get('code',''),'uncertain')
                return {'submission':batch['submission']}
        # Deliberately exclude arbitrary ERP order lookup and the legacy draft
        # retirement endpoint. Only scoped workspace operations are exposed.
        allowed_get=path in ('/api/workflow/knowledge-bases','/api/workflow/defaults','/api/yonyou/status','/api/workflow/batches') or re.fullmatch(r'/api/workflow/batches/[a-f0-9-]{36}',path)
        if method=='POST' and re.fullmatch(r'/api/workflow/invoices/(status|sample|start-preview|job|prepare-save|save|check|history|draft-read|draft-save)',path):
            action=path.rsplit('/',1)[1]
            if action=='save': self.accounts.audit(user['id'],'save_invoice','','started')
            result=workspace.post(path,data)
            if action in ('save','check'): self.accounts.audit(user['id'],action+'_invoice',result.get('receiptKey',''),result.get('state','unknown'))
            return result
        allowed_post=path in ('/api/workflow/mappings/lookup','/api/workflow/mappings/apply','/api/workflow/order-pricing/projects','/api/workflow/order-pricing/start','/api/workflow/order-pricing/query','/api/workflow/order-pricing/confirm','/api/workflow/order-pricing/apply','/api/workflow/customer-excel/inspect','/api/workflow/customer-excel/archive',
            '/api/workflow/archives/search','/api/workflow/stock/search','/api/workflow/defaults/preview','/api/workflow/defaults/save',
            '/api/workflow/batches','/api/yonyou/resolve-order','/api/yonyou/preview','/api/yonyou/confirm','/api/yonyou/save','/api/yonyou/invalidate') or re.fullmatch(r'/api/workflow/batches/[a-f0-9-]{36}/(handoff|draft|final|split-ready)',path)
        if method=='GET' and allowed_get:return workspace.get(path)
        if method=='POST' and allowed_post:
            if path in ('/api/yonyou/preview','/api/yonyou/save') and data.get('mode')=='live':
                status=workspace.gateway().transport('/api/status')
                if not status.get('multiuserOrderIdentity'):raise ValueError('请管理员重新连接新版用友服务，以启用多人订单保存；已有草稿会保留')
            if path=='/api/yonyou/save':self.accounts.audit(user['id'],'save_order',data.get('batchId',''),'started')
            result=workspace.post(path,data)
            if path=='/api/yonyou/save':self.accounts.audit(user['id'],'save_order',result.get('code',''),result.get('state','unknown'))
            return result
        raise AccessDenied('未开放此操作')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,status,value,extra=None,mime='application/json; charset=utf-8'):
        if not getattr(self,'request_id',None):
            self.request_id=uuid.uuid4().hex;self.request_started=time.monotonic()
        if isinstance(value,dict) and status>=400:value={**value,'requestId':self.request_id}
        self.server.runtime.request_log.write(self.request_id,getattr(self,'command',''),self.path,
            status,time.monotonic()-self.request_started,getattr(self,'error_kind',''))
        raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        for key,val in {'Content-Type':mime,'Content-Length':str(len(raw)),'Cache-Control':'no-store',
                        'X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY',
                        'X-Request-ID':self.request_id,'X-App-Build':BUILD,**(extra or {})}.items():self.send_header(key,val)
        self.end_headers();self.wfile.write(raw)

    def session_token(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return ''
        return cookie['gc_session'].value if 'gc_session' in cookie else ''

    def cookie(self,token,clear=False,remember=False):
        secure='; Secure' if self.server.origin.startswith('https://') else ''
        return f'gc_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else Accounts.session_seconds(remember)}{secure}'

    def login_peer(self):
        peer=str(ipaddress.ip_address(self.client_address[0]))
        if peer not in self.server.trusted_proxies:return peer
        # The company proxy must overwrite X-Real-IP with its socket peer.
        # Reject chains/duplicate values. Never derive trust from X-Forwarded-For.
        values=self.headers.get_all('X-Real-IP',[])
        if len(values)!=1:raise AccessDenied('可信代理未提供有效的客户端IP')
        value=values[0]
        if not value or '%' in value:raise AccessDenied('可信代理未提供有效的客户端IP')
        try:return str(ipaddress.ip_address(value))
        except ValueError:raise AccessDenied('可信代理未提供有效的客户端IP') from None

    def handle_request(self,method):
        self.request_id=uuid.uuid4().hex;self.request_started=time.monotonic();self.error_kind=''
        runtime=self.server.runtime
        origin=self.server.origin
        if self.headers.get('Host')!=urlsplit(origin).netloc:
            return self.reply(403,{'error':'访问地址无效'})
        if method=='POST' and self.headers.get('Origin')!=origin:
            return self.reply(403,{'error':'请求来源无效'})
        path=unquote(urlsplit(self.path).path)
        try:
            if path.startswith('/business/'):
                return runtime.business.handle(self,method)
            if path=='/tender' or path.startswith('/tender/'):
                return self.tender_request(method)
            if method=='GET' and path.startswith('/quote/'):
                user=runtime.accounts.authenticate(self.session_token())
                if user.get('department','gov')!='gov':raise AccessDenied('此账号未开放三方比价')
                name=path[len('/quote/'):]
                if name=='demo.html' and parse_qs(urlsplit(self.path).query).get('owner')!=[user['id']]:raise AccessDenied('比价账号已变化，请重新打开工作区')
                raw=runtime.quotes.asset(name)
                if name=='demo.html':
                    tags=f'<meta name="gc-quote-user" content="{html.escape(user["id"],quote=True)}"><meta name="gc-quote-csrf" content="{html.escape(runtime.accounts.csrf(self.session_token()),quote=True)}">'
                    raw=raw.replace(b'</head>',tags.encode()+b'</head>',1)
                    raw=raw.replace(b'href="/demo.css"',b'href="/quote/demo.css"').replace(b'src="/demo.js"',b'src="/quote/demo.js"')
                return self.reply(200,raw,extra={'X-Frame-Options':'SAMEORIGIN','Content-Security-Policy':"frame-ancestors 'self'"},mime=mimetypes.guess_type(name)[0] or 'application/octet-stream')
            if method=='GET' and not path.startswith('/api/'):
                if path=='/':target=self.server.web/'index.html'
                elif path=='/operator-guide.html':target=self.server.web/'operator-guide.html'
                elif path.startswith(('/assets/','/templates/')):
                    if path.startswith('/templates/'):runtime.accounts.authenticate(self.session_token())
                    target=(self.server.web/path.lstrip('/')).resolve()
                    if not target.is_relative_to(self.server.web.resolve()):raise AccessDenied('路径无效')
                else:return self.reply(404,{'error':'Not found'})
                if not target.is_file():return self.reply(404,{'error':'Not found'})
                raw=target.read_bytes()
                if path=='/':
                    raw=raw.replace(b'</head>',b'<meta name="gc-multiuser" content="true"></head>',1)
                    if getattr(runtime,'local_preview',False):raw=raw.replace(b'</head>',b'<meta name="gc-local-preview" content="true"></head>',1)
                extra=None
                if path=='/assets/price-lookup.html':
                    runtime.accounts.authenticate(self.session_token())
                    extra={'X-Frame-Options':'SAMEORIGIN','Content-Security-Policy':"frame-ancestors 'self'"}
                return self.reply(200,raw,extra=extra,mime=mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            token=self.session_token()
            if path.startswith('/api/quote/'):
                file_request=method=='GET' and re.fullmatch(r'/api/quote/files/[a-f0-9-]{36}',path)
                user=runtime.accounts.authenticate(token,None if file_request else self.headers.get('X-CSRF-Token',''))
                if user.get('department','gov')!='gov':raise AccessDenied('此账号未开放三方比价')
                params=parse_qs(urlsplit(self.path).query)
                owner=params.pop('owner',None) if file_request else [self.headers.get('X-Quote-Owner','')]
                if owner!=[user['id']]:raise AccessDenied('比价账号已变化或无权访问此资料')
                raw=b'';mime=self.headers.get('Content-Type','application/json')
                if method=='POST':
                    size=int(self.headers.get('Content-Length','0'))
                    multipart=path in ('/api/quote/files','/api/quote/demo/excel/import') and mime.startswith('multipart/form-data;')
                    if not 0<size<=16_000_000 or self.headers.get('Transfer-Encoding') or not (multipart or mime.split(';')[0]=='application/json'):raise ValueError('比价请求格式或大小无效')
                    raw=self.rfile.read(size)
                    if len(raw)!=size:raise ValueError('比价请求不完整')
                target='/api/'+path[len('/api/quote/'):]
                if params:target+='?'+urlencode(params,doseq=True)
                status,raw,response_mime,extra=runtime.quotes.request(user,method,target,raw,mime)
                # Do not deliver a long-running response after logout/disable.
                runtime.accounts.authenticate(token,None if file_request else self.headers.get('X-CSRF-Token',''))
                return self.reply(status,raw,extra=extra,mime=response_mime)
            if method=='GET' and path=='/api/session':
                try:user=runtime.accounts.authenticate(token)
                except AccessDenied:user=None
                return self.reply(200,{'multiUser':True,'user':user,'csrf':runtime.accounts.csrf(token) if user else '',
                                      'initialized':runtime.accounts.initialized() if hasattr(runtime.accounts,'initialized') else bool(runtime.accounts.list())})
            if path!='/api/login':
                download=method=='GET' and re.fullmatch(r'/api/workflow/batches/[^/]+/files/[^/]+',path)
                user=runtime.accounts.authenticate(token,None if download else self.headers.get('X-CSRF-Token',''))
            data={}
            if method=='POST':
                maximum=70_000_000 if path=='/api/workspace/image-draft' else 22_000_000 if path=='/api/recognize' else 16_000_000
                if path.startswith(('/api/admin/','/api/login','/api/logout','/api/change-password')):maximum=16384
                if path.startswith('/api/price-lookup/'):maximum=2048
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=maximum or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请求格式或大小无效')
                raw=self.rfile.read(size)
                if len(raw)!=size:raise ValueError('请求不完整')
                data=json.loads(raw)
                if not isinstance(data,dict):raise ValueError('请求需为JSON对象')
            if method=='POST' and path=='/api/login':
                user,token,csrf=runtime.accounts.login(data.get('username'),data.get('password'),self.login_peer(),remember=data.get('remember') is True)
                return self.reply(200,{'multiUser':True,'user':user,'csrf':csrf},{'Set-Cookie':self.cookie(token,remember=data.get('remember') is True)})
            if method=='POST' and path=='/api/logout':
                runtime.accounts.logout(token);return self.reply(200,{'loggedOut':True},{'Set-Cookie':self.cookie('',True)})
            if method=='POST' and path=='/api/change-password':
                runtime.accounts.change_password(user['id'],data.get('previous'),data.get('password'))
                return self.reply(200,{'loggedOut':True},{'Set-Cookie':self.cookie('',True)})
            if method=='GET' and path=='/api/health':return self.reply(200,{'ready':True,'local':True,'multiUser':True,'engine':'RapidOCR'})
            if path.startswith('/api/price-lookup/'):
                if user.get('department')=='bid' and user.get('role')!='admin':raise AccessDenied('此账号仅开放标书工作台')
                if method!='POST':return self.reply(405,{'error':'请使用查询表单'})
                result=runtime.price_lookup.dispatch(user['id'],path,data)
                runtime.accounts.authenticate(token,self.headers.get('X-CSRF-Token',''))
                return self.reply(200,result)
            result=runtime.dispatch(user,method,path,data)
            return self.reply(200,result,mime='application/octet-stream' if isinstance(result,bytes) else 'application/json; charset=utf-8')
        except TenderBridgeError as error:
            self.error_kind='tender_bridge'
            return self.reply(error.status,{'error':str(error)})
        except AccessDenied as error:
            self.error_kind='authorization'
            return self.reply(401 if path=='/api/login' else error.status,{'error':str(error)})
        except Exception as error:
            diagnostic=getattr(error,'diagnostic',{})
            if isinstance(diagnostic,dict) and (diagnostic.get('category')=='rate_limited' or str(diagnostic.get('httpStatus'))=='429' or str(diagnostic.get('apiCode'))=='310050'):
                self.error_kind='rate_limited'
                delay=diagnostic.get('retryAfterSeconds',30)
                delay=delay if type(delay) is int and 1<=delay<=86400 else 30
                return self.reply(429,{'error':'用友调用频率达到阈值，请等待后继续未完成项','httpStatus':429,'apiCode':'310050','retryAfterSeconds':delay},{'Retry-After':str(delay)})
            if isinstance(error,(ValueError,TypeError,KeyError)):
                self.error_kind='validation'
                return self.reply(400,{'error':str(error)[:250]})
            self.error_kind='internal'
            return self.reply(503,{'error':'处理未完成，请复制问题信息交给管理员；保存或提交结果不明时请勿重复操作'})

    def tender_request(self,method):
        runtime=self.server.runtime
        token=self.session_token()
        supplied=self.headers.get_all('X-Workbench-Token',[])
        if method=='POST' and len(supplied)!=1:raise AccessDenied('登录状态已变化，请重新进入标书工作台')
        csrf=supplied[0] if method=='POST' else runtime.accounts.csrf(token)
        def validate():
            user=runtime.accounts.authenticate(token,csrf if method=='POST' else None)
            if user.get('role')!='admin' and user.get('department')!='bid':raise AccessDenied('仅投标部和管理员可使用标书工作台')
            return user
        user=validate()
        raw_path=urlsplit(self.path)
        if raw_path.path=='/tender':
            if method!='GET':raise ValueError('标书路径无效')
            return self.reply(302,b'',{'Location':'/tender/'},mime='text/plain')
        if not raw_path.path.startswith('/tender/'):raise ValueError('标书路径无效')
        target=raw_path.path[len('/tender'):]+('?' + raw_path.query if raw_path.query else '')
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get('Transfer-Encoding') or (method=='POST' and len(lengths)!=1):raise ValueError('请求大小无效')
        size=int(lengths[0]) if method=='POST' else 0
        status,stream,mime,extra=runtime.tender.request(user,csrf,method,target,self.rfile,size,self.headers.get('Content-Type',''),validate)
        try:
            validate()
            stream.seek(0,2);length=stream.tell();stream.seek(0)
            self.send_response(status)
            for key,value in {'Content-Type':mime,'Content-Length':str(length),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'SAMEORIGIN','Content-Security-Policy':"frame-ancestors 'self'",**extra}.items():self.send_header(key,value)
            self.end_headers()
            try:
                while chunk:=stream.read(1024*1024):
                    validate()
                    self.wfile.write(chunk)
            except (AccessDenied,OSError):
                self.close_connection=True
                return
            runtime.request_log.write(self.request_id,method,'/tender/',status,time.monotonic()-self.request_started,'')
        finally:stream.close()

    def do_GET(self):self.handle_request('GET')
    def do_POST(self):
        try:heavy=int(self.headers.get('Content-Length','0'))>1_000_000
        except ValueError:return self.reply(400,{'error':'请求大小无效'})
        if heavy and not self.server.upload_slots.acquire(blocking=False):return self.reply(429,{'error':'上传正在排队，请稍后重试'})
        try:self.handle_request('POST')
        finally:
            if heavy:self.server.upload_slots.release()


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,runtime,origin,web=None,tls_context=None,trusted_proxies=None):
        self.runtime,self.origin=runtime,origin.rstrip('/')
        self.trusted_proxies=trusted_proxy_addresses(trusted_proxies)
        self.tls_context=tls_context
        self.web=Path(web or (ROOT/'web' if (ROOT/'web').is_dir() else ROOT/'dist/client'))
        self.slots=threading.BoundedSemaphore(12)
        self.upload_slots=threading.BoundedSemaphore(2)
        super().__init__(address,Handler)
    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,client_address)
        except Exception:
            self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()
    def get_request(self):
        sock,address=super().get_request();sock.settimeout(20)
        if self.tls_context:
            try:sock=self.tls_context.wrap_socket(sock,server_side=True,do_handshake_on_connect=False)
            except Exception:sock.close();raise
        return sock,address


def tls_settings(listen,origin,cert=None,key=None):
    address=ipaddress.ip_address(listen)
    if address.is_unspecified:raise ValueError('请指定本机的内网地址，不监听所有网络接口')
    if bool(cert)!=bool(key):raise ValueError('证书和私钥必须同时配置')
    if not address.is_loopback and not (cert and key and origin.startswith('https://')):
        raise ValueError('内网监听必须配置HTTPS证书')
    if cert:
        if not origin.startswith('https://'):raise ValueError('证书模式必须使用HTTPS地址')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert,key)
        return context
    return None


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-root',default=str(ROOT/'multiuser-state'))
    parser.add_argument('--port',type=int,default=4195)
    parser.add_argument('--origin',default='http://127.0.0.1:4195')
    parser.add_argument('--listen',default='127.0.0.1')
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--trusted-proxy',action='append',default=[],help='同机可信代理的回环IP，可重复；代理必须覆盖X-Real-IP')
    parser.add_argument('--init-admin',action='store_true')
    args=parser.parse_args()
    runtime=Runtime(args.data_root)
    if args.init_admin:
        import getpass
        name=input('管理员账号（字母/数字）: ').strip()
        display=input('管理员姓名: ').strip()
        password=getpass.getpass('密码（至少12位，不显示）: ')
        if password!=getpass.getpass('再次输入密码: '):raise ValueError('两次密码不一致')
        runtime.accounts.create(name,display,password,'admin',bootstrap=True)
        print('管理员已创建，请启动多人服务后登录。')
    else:
        parsed=urlsplit(args.origin)
        if parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:raise ValueError('公开地址格式无效')
        if parsed.scheme!='https' and args.origin!=f'http://127.0.0.1:{args.port}':raise ValueError('多人网络访问必须使用HTTPS公开地址')
        print(f'Multi-user workspace: {args.origin}',flush=True)
        context=tls_settings(args.listen,args.origin,args.tls_cert,args.tls_key)
        Server((args.listen,args.port),runtime,args.origin,tls_context=context,trusted_proxies=args.trusted_proxy).serve_forever()

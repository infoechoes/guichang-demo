"""Authenticated facade -> private Node worker; identity comes from Accounts only."""
import atexit
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit,parse_qs

ASSETS=frozenset(('demo.html','demo.css','demo.js','input-modes.js','image-input.js','excel-input.js','quote-evidence.js','workbench-embed.js','multiuser-client.js'))
ROUTES=re.compile(r'^/api/(?:workflow(?:/(?:save|export|import))?|compare/(?:run|open|resume|close)|session|tasks|files(?:/[a-f0-9-]{36})?|demo/(?:excel/(?:import|preview)|drafts(?:/clear)?|batch(?:/(?:start|continue|cancel|open|xlsx))?|recognition-status|recognize|status|cancel|supplement(?:/open)?|latest|run|xlsx)|aggregate/(?:status|open|read|evidence)|collector/(?:open|detail|jobs(?:/[a-f0-9-]{36}(?:/(?:resume|cancel))?)?))$')

class QuoteBridge:
    def __init__(self,root,project=None,node=None):
        self.root=Path(root).resolve()/'procurement-quotes'
        base=Path(__file__).resolve().parents[1]
        self.base=base
        self.project=Path(project or os.environ.get('GQ_QUOTE_PROJECT') or (base/'quote' if (base/'quote').is_dir() else base.parent.parent/'guichang-quote')).resolve()
        self.node=node or os.environ.get('GQ_NODE') or (str(base/'node/node.exe') if (base/'node/node.exe').is_file() else shutil.which('node'))
        self.lock=threading.Lock();self.process=None;self.port=None;self.key=None
        atexit.register(self.close)
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.process=None
    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:return
            worker=self.project/'local/multiuser-worker.mjs'
            if not self.node or not worker.is_file():raise ValueError('三方比价服务尚未安装，请管理员配置 Node 和比价模块目录')
            self.root.mkdir(parents=True,exist_ok=True)
            ready=self.root/('worker-ready-'+secrets.token_hex(8)+'.json')
            self.key=secrets.token_hex(32)
            # Do not inherit ERP credentials or a legacy shared browser profile.
            allowed={'PATH','SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','HOMEDRIVE','HOMEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','GQ_OCR_PYTHON','GQ_OCR_DEPS','GQ_EXCEL_PYTHON','GQ_ARTIFACT_NODE_MODULES'}
            env={k:v for k,v in os.environ.items() if k.upper() in allowed}
            # The release is self-contained; do not depend on Codex being
            # installed under the Windows service account's home directory.
            if (self.base/'runtime/python.exe').is_file():
                env.setdefault('GQ_OCR_PYTHON',str(self.base/'runtime/python.exe'))
                env.setdefault('GQ_EXCEL_PYTHON',str(self.base/'runtime/python.exe'))
                env.setdefault('GQ_OCR_DEPS',str(self.base/'python-deps'))
            env.update(GQ_MULTIUSER_ROOT=str(self.root),GQ_GATEWAY_KEY=self.key,GQ_READY_FILE=str(ready))
            with (self.root/'worker.log').open('ab') as log:
                self.process=subprocess.Popen([self.node,str(worker)],cwd=self.project,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if ready.is_file():
                    try:
                        data=json.loads(ready.read_text());port=data['port']
                        if data['pid']!=self.process.pid or type(port)is not int or not 1<=port<=65535:raise ValueError()
                        self.port=port;ready.unlink();return
                    except (json.JSONDecodeError,KeyError):pass
                if self.process.poll() is not None:break
                time.sleep(.1)
            self.close()
            raise ValueError('三方比价服务未能启动，请管理员检查模块依赖及服务日志')
    def asset(self,name):
        if name not in ASSETS:raise ValueError('比价资源不存在')
        target=self.project/'public'/name
        if not target.is_file():raise ValueError('比价模块文件不完整')
        return target.read_bytes()
    def request(self,user,method,path,body=b'',content_type='application/json'):
        if not re.fullmatch('[a-f0-9]{32}',user.get('id','')) or user.get('department','gov')!='gov':raise ValueError('账号不能访问比价工作区')
        parsed=urlsplit(path)
        if not ROUTES.fullmatch(parsed.path) or method not in ('GET','POST'):raise ValueError('比价接口未开放')
        if method=='GET' and parsed.query:
            params=parse_qs(parsed.query)
            if set(params)-{'taskId'}:raise ValueError('比价查询参数无效')
        self.start()
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=300)
        try:
            connection.request(method,path,body=body if method=='POST' else None,headers={'Origin':f'http://127.0.0.1:{self.port}','Content-Type':content_type,'X-GQ-Gateway':self.key,'X-GQ-User':user['id']})
            response=connection.getresponse();raw=response.read(80_000_001)
            if len(raw)>80_000_000:raise ValueError('比价响应过大，请减少本次商品数量')
            mime=response.getheader('Content-Type','application/octet-stream')
            headers={}
            disposition=response.getheader('Content-Disposition')
            if disposition:headers['Content-Disposition']=disposition
            if mime.startswith('application/json'):
                value=json.loads(raw)
                def scoped(item):
                    if isinstance(item,str) and re.fullmatch(r'/api/files/[a-f0-9-]{36}',item):return '/api/quote/'+item[5:]+'?owner='+user['id']
                    if isinstance(item,list):return [scoped(x) for x in item]
                    if isinstance(item,dict):return {k:scoped(v) for k,v in item.items()}
                    return item
                raw=json.dumps(scoped(value),ensure_ascii=False).encode()
            return response.status,raw,mime,headers
        except (OSError,http.client.HTTPException) as error:
            raise ValueError('比价服务连接中断；已保存任务保留，请重新读取进度，不要重复发起查询') from error
        finally:connection.close()

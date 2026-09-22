"""Optional loopback review launcher using the existing center account service.

Passwords are forwarded over certificate-verified TLS and never saved. Only
login, session checks, logout and account-list reads reach the center. All
business work uses the local Runtime. Normal deployment uses Accounts unchanged.
"""
import argparse
import hmac
import json
import ssl
import threading
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from multiuser_server import Runtime,Server
from multiuser_auth import Accounts,AccessDenied,LoginRequired

class CenterAccounts:
    def __init__(self,origin,cert,local):
        self.origin=origin.rstrip('/');self.local=local;self.context=threading.local()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cert)))
    def request(self,path,data=None,token='',csrf=''):
        if path not in ('/api/session','/api/login','/api/logout','/api/admin/users'):raise AccessDenied('预览不修改中心机账号')
        headers={'Content-Type':'application/json','Origin':self.origin}
        if token:headers['Cookie']='gc_session='+token
        if csrf:headers['X-CSRF-Token']=csrf
        req=urllib.request.Request(self.origin+path,None if data is None else json.dumps(data).encode(),headers)
        try:
            with self.opener.open(req,timeout=12) as r:return json.load(r),r.headers
        except urllib.error.HTTPError as e:
            try:message=json.load(e).get('error','原账号校验未通过')
            except Exception:message='原账号校验未通过'
            raise LoginRequired(message) from None
        except (OSError,urllib.error.URLError):raise AccessDenied('无法连接中心机账号服务，请确认网络后重试') from None
    def initialized(self):return self.request('/api/session')[0]['initialized']
    def login(self,name,password,peer,remember=False):
        data,headers=self.request('/api/login',{'username':name,'password':password,'remember':remember})
        cookies=SimpleCookie();cookies.load(headers.get('Set-Cookie',''));token=cookies['gc_session'].value
        self.context.token=token;self.context.session=data
        return data['user'],token,data['csrf']
    def authenticate(self,token,csrf=None):
        if not token:raise LoginRequired('请先登录')
        data,_=self.request('/api/session',token=token)
        if not data.get('user'):raise LoginRequired('登录已失效，请重新登录')
        if csrf is not None and not hmac.compare_digest(str(csrf),str(data.get('csrf',''))):raise AccessDenied('页面已失效，请刷新后重试')
        self.context.token=token;self.context.session=data
        return data['user']
    def csrf(self,token):
        if getattr(self.context,'token',None)!=token:self.authenticate(token)
        return self.context.session['csrf']
    def list(self):
        token=getattr(self.context,'token','')
        return self.request('/api/admin/users',token=token,csrf=self.csrf(token))[0]['users']
    def logout(self,token):self.request('/api/logout',{},token,self.csrf(token))
    def audit(self,*args,**kwargs):return self.local.audit(*args,**kwargs)
    def recent_audit(self):return self.local.recent_audit()
    def __getattr__(self,name):
        if name in ('create','disable','enable','admin_reset_password','change_password'):raise AccessDenied('本地审查不修改中心机账号；账号管理请使用原入口')
        raise AttributeError(name)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data-root',required=True);p.add_argument('--center',required=True);p.add_argument('--center-cert',required=True);p.add_argument('--port',type=int,default=4198);a=p.parse_args()
    if not a.center.startswith('https://'):raise ValueError('账号服务必须使用 HTTPS')
    def preview_transport(path,data=None):
        if path=='/api/status':return {'ready':False,'liveSaveEnabled':False,'multiuserOrderIdentity':False,'message':'本地联调未连接用友保存服务'}
        raise ValueError('本地联调未连接用友服务；已保存的原单、草稿和参考信息可继续处理')
    r=Runtime(a.data_root,transport=preview_transport);r.local_preview=True;r.accounts=CenterAccounts(a.center,a.center_cert,r.accounts)
    origin=f'http://127.0.0.1:{a.port}';print(f'统一 Demo 本地审查：{origin}；使用中心机原账号；业务数据保存在本机',flush=True)
    Server(('127.0.0.1',a.port),r,origin).serve_forever()

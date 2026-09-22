const scope=document.querySelector('meta[name="gc-quote-user"]');
if(scope){
 const owner=scope.content,csrf=document.querySelector('meta[name="gc-quote-csrf"]').content;
 const original=window.fetch.bind(window);let frozen=false;
 function freeze(){
  if(frozen)return;
  frozen=true;document.body.replaceChildren(Object.assign(document.createElement('p'),{textContent:'登录状态已变化，请返回工作台重新登录并打开三方比价。'}));
  window.parent.postMessage({type:'guichang-quote:session-expired',version:1},location.origin);
 }
 window.fetch=async(input,options={})=>{
  if(frozen)throw Error('请重新登录');
  const request=new Request(new URL(typeof input==='string'?input:input.url,location.href),options);
  const url=new URL(request.url);
  if(url.origin!==location.origin)throw Error('比价页面仅访问本机认证接口');
  if(url.pathname.startsWith('/api/')){
   if(!url.pathname.startsWith('/api/quote/'))url.pathname='/api/quote/'+url.pathname.slice(5);
   const headers=new Headers(request.headers);headers.set('X-CSRF-Token',csrf);headers.set('X-Quote-Owner',owner);
   const response=await original(new Request(url,{method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:await request.arrayBuffer(),credentials:'same-origin',cache:'no-store',signal:request.signal}));
   if(response.status===401||response.status===403){freeze();throw Error('登录已失效或账号已变化');}
   return response;
  }
  return original(request);
 };
 const prepare=()=>{
  document.documentElement.classList.add('workbench-embedded');
  for(const id of ['open','history','supplement-open','excel-login']){const element=document.getElementById(id);if(element)element.style.display='none';}
 };
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',prepare);else prepare();
 window.addEventListener('message',event=>{if(event.source===window.parent&&event.origin===location.origin&&event.data?.type==='guichang-quote:freeze')freeze();});
 const verify=()=>{if(!frozen)window.fetch('/api/session').catch(()=>{});};
 window.addEventListener('focus',verify);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)verify();});
 setInterval(verify,15000);
}

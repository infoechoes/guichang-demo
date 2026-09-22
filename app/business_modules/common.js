/* Same workbench session and account for all embedded workflows. */
(()=>{const original=window.fetch.bind(window),prefix=location.pathname.match(/^\/business\/[^/]+/)[0],owner=new URLSearchParams(location.search).get('owner');
function scoped(url){let u=new URL(url,location.href);if(u.origin===location.origin&&(/^\/(api|download)\//.test(u.pathname))){u.pathname=prefix+u.pathname;u.searchParams.set('owner',owner)}return u}
window.fetch=async(input,options={})=>{const u=scoped(input);if(u.pathname.startsWith(prefix+'/api/')||u.pathname.startsWith(prefix+'/download/')){const s=await original('/api/session').then(r=>r.json());if(!s.user||s.user.id!==owner){parent.dispatchEvent(new Event('session-expired'));throw Error('登录账号已变化，请重新打开页面')}options={...options,headers:{...options.headers,'X-CSRF-Token':s.csrf,'X-Business-Owner':owner}}}return original(u,options)};
document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(a&&/^\/(api|download)\//.test(a.getAttribute('href')))a.href=scoped(a.href).href});
window.gcGo=module=>parent.postMessage({type:'gc-business-open',module},location.origin);
})();

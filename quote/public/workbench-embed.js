// Versioned readiness only. No order data, credentials or ERP commands cross
// this boundary. The standalone application remains usable without a host.
if(new URLSearchParams(location.search).get('embedded')==='1'&&window.parent!==window){
 let host;
 try{host=new URL(document.referrer);}catch{}
 if((document.querySelector('meta[name="gc-quote-user"]')&&host?.origin===location.origin)||(host?.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(host.hostname))){
  document.documentElement.classList.add('workbench-embedded');
  const ready=()=>window.parent.postMessage({type:'guichang-quote:ready',version:1},host.origin);
  window.addEventListener('message',event=>{
   if(event.source===window.parent&&event.origin===host.origin&&event.data?.type==='guichang-quote:hello'&&event.data?.version===1)ready();
  });
  ready();
 }
}

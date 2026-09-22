import fs from 'node:fs';import path from 'node:path';import {randomUUID} from 'node:crypto';
import {filterCandidates} from './search-match.mjs';
import {Collector} from './collector.mjs';import {Aggregator} from './aggregator.mjs';
export function comparisonRunner(root,saveImage,{CollectorClass=Collector,AggregatorClass=Aggregator}={}){
 const entries=new Map();let active=0;
 const valid=driver=>{if(!['gwdang','jd','taobao','pdd'].includes(driver))throw Error('查价来源无效');};
 const stop=async entry=>{clearTimeout(entry.timer);if(entry.engine)await entry.engine.close();entry.engine=null;entry.manual=false;};
 const engineFor=(driver,entry)=>entry.engine??(entry.engine=driver==='gwdang'?new AggregatorClass({root:entry.directory,saveImage}):new CollectorClass({root:entry.directory,saveImage}));
 const arm=entry=>{clearTimeout(entry.timer);entry.timer=setTimeout(()=>{if(!entry.busy)void stop(entry).catch(()=>{});},20*60*1000);entry.timer.unref?.();};
 const task=query=>({id:randomUUID(),name:query,demoKeyword:query,brand:'',model:'',spec:'',color:'',pack:'',description:'',specConfirmed:false,quotes:[],strategy:'min',revision:0,updatedAt:new Date().toISOString()});
 async function execute(driver,query,resume=false){
  valid(driver);if(typeof query!=='string'||!query.trim()||query.length>120)throw Error('关键词无效');
  let entry=entries.get(driver);if(!entry){const directory=path.join(root,'compare-sources',driver);fs.mkdirSync(directory,{recursive:true});entry={directory,engine:null,busy:false,blocked:0};entries.set(driver,entry);}
  if(entry.busy||active>=2)throw Error('此来源正在运行，请稍后再试');
  if(!resume&&entry.blocked>Date.now()){if(!entry.manual){entry.query=query;entry.job=null;}return{state:entry.state||'verification_required',message:'此来源需要登录或验证，可打开验证页后继续。',canVerify:true,items:[],query:entry.query};}
  if(entry.manual&&!resume)throw Error('验证窗口已打开，请完成后点“验证后继续”');
  active++;entry.busy=true;entry.query=query;let result;
  try{
   const engine=engineFor(driver,entry);engine.keepVisible=entry.manual===true;
   if(driver==='gwdang'){
    if(resume){engine.blocked=false;engine.nextNavigationAt=0;}
    result=await engine.read({keyword:query});
   }else{
    // Only this dedicated driver's in-memory blocked task is cleared after explicit manual continuation.
    if(resume&&!entry.job)engine.jobs.clear();
    const job=resume&&entry.job?await engine.resume(entry.job,driver):engine.start(task(query),[driver],5);entry.job=job.id;
    const deadline=Date.now()+60000;
    while(true){result=engine.snapshot(job.id).platforms[driver];if(!['queued','running'].includes(result.state))break;if(Date.now()>deadline){engine.cancel(job.id);result={...result,state:'timeout',message:'60秒内未完成，已停止此来源'};break;}await new Promise(r=>setTimeout(r,300));}
   }
   const needs=['login_required','verification_required'].includes(result.state);
   entry.blocked=needs?Date.now()+300000:0;entry.state=result.state;
   if(needs){const page=driver==='gwdang'?engine.currentPage:engine.contexts?.get(driver)?.pages().find(p=>!p.isClosed());entry.url=page?.url();result={...result,canVerify:true,message:'需要登录或人工验证。点击打开验证页，在中心机完成后继续。'};}
   const filtered=filterCandidates(result.items||[],query);
   return{...result,state:(result.items?.length&&!filtered.items.length)?'no_match':result.state,items:filtered.items.slice(0,5),excludedCount:filtered.excludedCount,query,queriedAt:new Date().toISOString(),driver};
  }finally{
   entry.busy=false;active--;
   if(entry.manual&&['login_required','verification_required'].includes(result?.state))arm(entry);else{await stop(entry);entry.job=null;}
  }
 }
 const run=({driver,query})=>execute(driver,query);
 run.open=async({driver})=>{
  valid(driver);const entry=entries.get(driver);if(!entry?.query)throw Error('请先查询该来源');if(entry.busy)throw Error('查询进行中，请稍后');
  entry.busy=true;try{
   const engine=engineFor(driver,entry);entry.manual=true;engine.keepVisible=true;
   const context=driver==='gwdang'?null:await engine.context(driver,{visible:true});const page=driver==='gwdang'?await engine.page({visible:true}):(context.pages().find(p=>!p.isClosed())||await context.newPage());
   if(entry.url&&page.url()==='about:blank')await page.goto(entry.url,{waitUntil:'domcontentloaded',timeout:30000});await page.bringToFront();arm(entry);
   return{state:'window_open',message:'验证页已在中心机打开。请在中心机完成登录或验证，再点“验证后继续”。窗口20分钟未操作会关闭，登录状态保留在专用浏览器。'};
  }catch(error){await stop(entry);throw error;}finally{entry.busy=false;}
 };
 run.resume=({driver})=>{valid(driver);const entry=entries.get(driver);if(!entry?.query)throw Error('请先查询该来源');return execute(driver,entry.query,true);};
 run.dismiss=async({driver})=>{valid(driver);const entry=entries.get(driver);if(entry?.busy)throw Error('查询进行中');if(entry)await stop(entry);return{state:'closed',message:'验证窗口已关闭'};};
 run.close=async()=>{for(const entry of entries.values())await stop(entry);};
 return run;
}

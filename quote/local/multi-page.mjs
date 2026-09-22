import {captureCandidates,candidateLimit,usableCandidate} from './candidate-budget.mjs';
import {quoteEvidence} from '../public/quote-evidence.js';
import {validNextPage} from './aggregator.mjs';
const key=q=>q.aggregateProductId||q.platform+':'+q.url;
export function pricedPlatforms(items){return [...new Set(items.filter(q=>Number.isFinite(q.price)&&q.price>0&&q.availability!=='unavailable'&&quoteEvidence(q)).map(q=>q.platform))];}
export class MultiPageSearch{
 constructor(aggregator,{sleep=ms=>new Promise(r=>setTimeout(r,ms)),now=()=>Date.now()}={}){this.aggregator=aggregator;this.sleep=sleep;this.now=now;this.progress=null;this.cancelled=false;}
 cancel(){this.cancelled=true;return {state:'cancelling',message:'将在当前读取或截图结束后停止，已取得的结果保留。'};}
 async run({keyword='',url='',current=false,maxPages=3,candidatesPerPlatform=1},previous,onCheckpoint=()=>{}){
  if(!Number.isInteger(maxPages)||maxPages<1||maxPages>5)throw Error('最多读取1至5页');
  const limit=candidateLimit(candidatesPerPlatform);
  this.cancelled=false;
  const resume=current&&previous?.search?.resumable&&previous.keyword===keyword;
  const items=resume?[...previous.items]:[],pages=resume?[...previous.search.pages]:[],coverage=new Map(),seen=new Set(items.map(key));
  if(resume)for(const p of previous.platformCoverage||[])coverage.set(p.platform,{...p,reasons:[...(p.reasons||[])]});
  let next=url||'',first=true,stopReason='page_limit',state='collected',message='',last=resume?previous:null;
  const target=3;
  const finish=()=>({...(last||{}),keyword,items,platformCoverage:[...coverage.values()].map(p=>({...p,kept:items.filter(q=>q.platform===p.platform).length,status:items.some(q=>q.platform===p.platform)?'有相关候选，规格待核验':p.received?'已返回，但未保留相关候选':'已读聚合页未返回，尚未逐平台查询'})),state:state==='verification_required'?state:items.length?'collected':state==='collected'?'no_match':state,message,search:{pages,maxPages,targetPlatforms:target,pricedPlatforms:pricedPlatforms(items),stopReason,resumable:stopReason==='verification_required',candidatesPerPlatform:limit,scope:`购物党搜索页；每平台取证目标${limit}条；平台达标须同时有价格和绑定截图，仍待核验同款`},cached:pages.length>0&&pages.every(p=>p.cached),rawCount:[...coverage.values()].reduce((n,p)=>n+p.received,0),excludedCount:[...coverage.values()].reduce((n,p)=>n+p.excluded,0)});
  this.progress={state:'running',page:pages.length+1,maxPages,platforms:pricedPlatforms(items),message:'正在读取页面'};
  try{
   while(pages.length<maxPages){
    if(this.cancelled){stopReason='cancelled';message='已停止，保留已取得的候选与截图。';break;}
    if(!first){const wait=Math.max(0,(this.aggregator.nextNavigationAt||0)-this.now());this.progress={...this.progress,page:pages.length+1,message:`等待翻页间隔，约${Math.ceil(wait/1000)}秒`};for(let remaining=wait;remaining>0&&!this.cancelled;remaining-=1000)await this.sleep(Math.min(1000,remaining));if(this.cancelled)continue;}
    this.progress={...this.progress,page:pages.length+1,message:'正在读取并核对商品'};
    let result;try{result=await this.aggregator.read({keyword,url:next,current:first&&current});}catch(e){stopReason='read_error';state='read_error';message='页面读取失败，已取得的结果保留：'+e.message;break;}
    first=false;
    if(result.state==='verification_required'){stopReason='verification_required';state=result.state;message='翻页遇到人工验证，已停止；完成验证后可继续，之前页面的截图已保留。';break;}
    if(!result.sourceUrl||pages.some(p=>p.url===result.sourceUrl)){stopReason=result.sourceUrl?'repeated_page':'page_changed';message='未取得新的有效搜索页，已停止。';break;}
    last=result;
    for(const p of result.platformCoverage||[]){const old=coverage.get(p.platform)||{platform:p.platform,received:0,excluded:0,reasons:[]};coverage.set(p.platform,{...old,received:old.received+p.received,excluded:old.excluded+p.excluded,reasons:[...new Set([...old.reasons,...p.reasons])]});}
    let added=0;
    const candidates=(result.items||[]).filter(q=>!seen.has(key(q)));
    for(const q of candidates){
     if(this.cancelled)break;
     q.searchPageUrl=result.sourceUrl;q.searchPageNumber=pages.length+1;
     if(result.cached){const old=previous?.items.find(x=>key(x)===key(q)&&x.id===q.id&&x.collectedAt===q.collectedAt);if(old&&quoteEvidence(old))q.evidence=old.evidence;}
    }
    const captured=await captureCandidates(candidates,{existing:items,limit,capture:result.cached?null:id=>this.aggregator.evidence(id),cancelled:()=>this.cancelled});
    for(const q of captured){seen.add(key(q));items.push(q);added++;}
    pages.push({url:result.sourceUrl,collectedAt:result.collectedAt,cached:!!result.cached,received:result.rawCount||0,added});
    this.progress={state:'running',page:pages.length,maxPages,platforms:pricedPlatforms(items),message:'本页结果与截图已保存'};
    await onCheckpoint(finish());
    if(this.cancelled){stopReason='cancelled';message='已停止，已取得结果保留。';break;}
    if(pricedPlatforms(items).filter(p=>items.filter(q=>q.platform===p&&usableCandidate(q)).length>=limit).length>=target){stopReason='target_reached';message='已找到至少3个平台带价格和截图的相关候选，停止翻页；具体规格仍需核验。';break;}
    next=validNextPage(result.nextPageUrl,result.sourceUrl);
    if(!next){stopReason='no_next_page';message='页面没有可用的下一页链接，停止读取；未达到本轮取证目标。';break;}
    if(pages.some(p=>p.url===next)){stopReason='repeated_page';message='下一页指向已读页面，停止重复访问。';break;}
   }
   if(stopReason==='page_limit')message=`已达到${maxPages}页上限，取得${pricedPlatforms(items).length}个平台带价格和截图的候选；不补造缺项。`;
   return finish();
  }finally{this.progress={...this.progress,state:'idle',message};}
 }
}

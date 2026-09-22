import {candidateLimit,usableCandidate} from './candidate-budget.mjs';
import {PLATFORMS} from './adapters.mjs';
import {quoteEvidence} from '../public/quote-evidence.js';
import {filterCandidates} from './search-match.mjs';
export const directPlatforms=['jd','taobao','pdd'];
export function missingPlatforms(items,limit=1){const counts=new Map();for(const q of items)if(usableCandidate(q))counts.set(q.platform,(counts.get(q.platform)||0)+1);if([...counts.values()].filter(n=>n>=limit).length>=3)return [];return directPlatforms.filter(p=>(counts.get(PLATFORMS[p].name)||0)<limit);}
const identity=q=>{try{const u=new URL(q.originUrl||q.url);return q.platform+':'+(q.platform==='京东'?u.pathname:u.searchParams.get('id')||u.searchParams.get('goods_id')||u.href);}catch{return q.id;}};
export function mergeDirect(items,added){const out=[...items];for(const q of added){const i=out.findIndex(x=>identity(x)===identity(q));if(i<0)out.push(q);else if(quoteEvidence(q))out[i]=q;}return out;}
export class SupplementSearch{
 constructor(collector,{sleep=ms=>new Promise(r=>setTimeout(r,ms))}={}){this.collector=collector;this.sleep=sleep;this.cancelled=false;this.current=null;this.progress=null;}
 cancel(){this.cancelled=true;if(this.current)this.collector.cancel(this.current);}
 async run(result,{force=false,resume=false,skip=false}={},save=()=>{}){
  if(!result.keyword?.trim())throw Error('原商城补查需要商品关键词，请填写后重新搜索');
  this.cancelled=false;
  const limit=candidateLimit(result.candidatesPerPlatform);const old=result.supplement;
  const s=resume&&old?structuredClone(old):{queue:force?directPlatforms:missingPlatforms(result.items||[],limit),platforms:{},state:'running',reason:force?'手动补查三个原商城':`不足3个平台时补查缺项，每平台取证目标${limit}条`,keyword:result.keyword};
  if(resume&&(!old||old.keyword!==result.keyword))throw Error('没有对应的补查记录');
  if(skip&&resume&&s.pendingPlatform){s.platforms[s.pendingPlatform]={...s.platforms[s.pendingPlatform],state:'skipped',message:'本轮跳过：'+s.platforms[s.pendingPlatform].message};delete s.pendingPlatform;}
  let out={...result,supplement:s};s.state='running';s.message='';
  try{
   for(const platform of s.queue){
    if(this.cancelled)break;
    if(!force&&!missingPlatforms(out.items||[],limit).length)break;
    const prior=s.platforms[platform];
    if(prior&&!['login_required','verification_required','interrupted','queued','running'].includes(prior.state))continue;
    this.progress={state:'running',message:`正在补查${PLATFORMS[platform].name}，取得${limit}条价格与截图候选后停止`};
    let job;
    try{
     if(prior?.jobId)job=await this.collector.resume(prior.jobId,platform);
     else job=this.collector.start({name:result.keyword,demoKeyword:result.keyword,evidenceLimit:limit},[platform],limit+4);
     this.current=job.id;s.platforms[platform]={jobId:job.id,state:'running',message:'正在读取原商城',kept:0};await save(out);
     do{await this.sleep(250);job=this.collector.snapshot(job.id);if(this.cancelled)this.collector.cancel(job.id);}while(['queued','running'].includes(job.platforms[platform].state));
     const state=job.platforms[platform],filtered=filterCandidates(state.items||[],result.keyword);
     out.items=mergeDirect(out.items||[],filtered.items);if(out.items.length)out.state='collected';
     s.platforms[platform]={jobId:job.id,state:state.state,message:state.message,received:state.received||state.items.length,kept:filtered.items.length,excluded:state.excluded||0};
     if(['login_required','verification_required'].includes(state.state)){
      s.state=state.state;s.pendingPlatform=platform;s.message=`${PLATFORMS[platform].name}需要${state.state==='login_required'?'登录':'人工验证'}，补查已暂停；完成后点击“继续原商城补查”。`;
      out.message=s.message;await save(out);return out;
     }
    }catch(e){s.platforms[platform]={state:'error',message:e.message.split('\n')[0],kept:0};}
    finally{this.current=null;}
    await save(out);
   }
   s.state=this.cancelled?'cancelled':'completed';delete s.pendingPlatform;
   s.message=this.cancelled?'原商城补查已停止，已取得结果保留。':s.queue.length?'原商城补查已结束；请查看各平台结果，未核实同款。':'已有三个平台达到本轮价格及截图候选目标，未自动补查；这不代表覆盖完整。';
   out.message=s.message;await save(out);return out;
  }finally{this.current=null;this.progress={state:'idle',message:s.message};}
 }
}

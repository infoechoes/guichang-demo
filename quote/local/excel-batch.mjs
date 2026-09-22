import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {groupRows} from './excel-import.mjs';
import {candidateLimit,usableCandidate} from './candidate-budget.mjs';

export function selectedBatchQuotes(group){
 const found=new Map();
 for(const q of group.result?.items||[])if(usableCandidate(q)&&!found.has(q.platform))found.set(q.platform,q);
 return [...found.values()].slice(0,3);
}
export class ExcelBatch{
 constructor({root,demo,imports,sleep=ms=>new Promise(r=>setTimeout(r,ms)),now=()=>Date.now()}){
  this.file=path.join(root,'excel-batch.json');this.demo=demo;this.imports=imports;this.active=false;this.sleep=sleep;this.now=now;
  try{this.job=JSON.parse(fs.readFileSync(this.file,'utf8'));if(this.job.state==='running'){this.job.state='interrupted';this.job.message='服务重启，已取得结果保留，可继续未完成商品';for(const g of this.job.groups)if(g.state==='running')g.state='queued';this.save();}}catch{this.job=null;}
 }
 save(){if(!this.job)return;this.job.updatedAt=new Date().toISOString();fs.writeFileSync(this.file+'.tmp',JSON.stringify(this.job));fs.renameSync(this.file+'.tmp',this.file);}
 snapshot(){return this.job?structuredClone({...this.job,active:this.active,progress:this.active?this.demo.progress():null}):null;}
 start(input){
  if(this.active||this.demo.busy)throw Error('已有采集正在运行，请等待或停止');
  const source=this.imports.read(input.importId);
  if(!Array.isArray(input.rows)||!input.rows.length||input.rows.length>200)throw Error('请选择1至200条商品');
  const ids=new Set();
  const rows=input.rows.map(raw=>{
   const sheet=source.sheets.find(s=>s.name===raw.sheet),original=sheet?.rows.find(r=>r.number===raw.excelRow);
   if(!original||raw.id!==raw.sheet+':'+raw.excelRow||ids.has(raw.id))throw Error('商品来源行无效或重复');ids.add(raw.id);
   const row={id:raw.id,sheet:raw.sheet,excelRow:raw.excelRow};
   for(const k of ['name','brand','spec','unit','department','sourceRow','query']){if(typeof raw[k]!=='string'||raw[k].length>(k==='query'?200:500))throw Error('商品字段过长或无效');row[k]=raw[k].trim();}
   if(!row.name||!row.query)throw Error('商品名称和搜索词不能为空');
   row.quantity=raw.quantity===''||raw.quantity==null?null:Number(raw.quantity);
   if(row.quantity!==null&&(!Number.isFinite(row.quantity)||row.quantity<=0||row.quantity>1e9))throw Error('商品数量须大于0，未知请留空');
   return row;
  });
  const maxPages=input.maxPages??1;if(!Number.isInteger(maxPages)||maxPages<1||maxPages>5)throw Error('搜索页数应为1至5');
  this.job={id:randomUUID(),importId:source.id,fileName:source.name,createdAt:new Date().toISOString(),state:'running',message:'正在准备批量比价',rows,groups:groupRows(rows),options:{maxPages,candidatesPerPlatform:candidateLimit(input.candidatesPerPlatform),supplement:input.supplement!==false}};
  this.cancelled=false;this.save();this.launch();return this.snapshot();
 }
 launch(){this.active=true;this.promise=this.process().catch(e=>{this.job.state='error';this.job.message=e.message.split('\n')[0];}).finally(()=>{this.active=false;this.save();});}
 continue({skip=false}={}){
  if(this.active||this.demo.busy)throw Error('采集仍在运行');if(!this.job)throw Error('没有可继续的批量任务');
  const pending=this.job.groups.find(g=>g.state==='paused');
  if(skip&&pending){pending.state='skipped';pending.message='已跳过，保留已有候选及缺项原因';}
  this.cancelled=false;this.job.state='running';this.save();this.launch();return this.snapshot();
 }
 cancel(){this.cancelled=true;this.demo.cancel();return {message:'正在停止，已取得结果和清单会保留'};}
 async process(){
  for(const [i,g] of this.job.groups.entries()){
   if(this.cancelled)break;
   if(!['queued','paused','running','cancelled'].includes(g.state))continue;
   const resume=g.state==='paused';g.state='running';this.job.current=i;this.job.message=`正在比价 ${i+1}/${this.job.groups.length}：${g.name}`;this.save();
   try{
    while(!resume&&!this.demo.aggregator?.blocked&&(this.demo.aggregator?.nextNavigationAt||0)>this.now()&&!this.cancelled){
     const remaining=this.demo.aggregator.nextNavigationAt-this.now();this.job.message=`${i+1}/${this.job.groups.length}：${g.name}，等待查询间隔约${Math.ceil(remaining/1000)}秒`;await this.sleep(Math.min(1000,remaining));
    }
    if(this.cancelled){g.state='queued';break;}
    let r;
    if(resume&&g.pending==='supplement'&&this.demo.latest()?.keyword===g.query)r=await this.demo.supplement({resume:true});
    else r=await this.demo.run({keyword:g.query,...this.job.options,...(resume&&g.pending==='aggregate'?{current:true}:{})});
    g.result=r;
    const blocked=['verification_required','login_required'];
    if(blocked.includes(r.supplement?.state)||blocked.includes(r.state)){
     g.pending=blocked.includes(r.supplement?.state)?'supplement':'aggregate';g.state='paused';g.message=r.supplement?.message||r.message||'平台需要验证';
     this.job.state='paused';this.job.message=`${g.name}：${g.message}；已保存结果，可跳过该商品继续。`;this.save();return;
    }
    const count=selectedBatchQuotes(g).length;g.state=this.cancelled?'cancelled':count>=3?'candidates_ready':count?'partial':'no_result';
    g.message=count?`取得${count}个平台的价格与截图，待核验同款`:(r.message||'未取得带价格和绑定截图的候选');
   }catch(e){g.state='error';g.message=e.message.split('\n')[0];}
   this.save();
  }
  this.job.state=this.cancelled?'cancelled':'completed';this.job.message=this.cancelled?'批量比价已停止，可继续未完成商品':'本轮批量比价结束，可下载同一个Excel；缺项和待核验信息已保留';this.save();
 }
}

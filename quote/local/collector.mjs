import {usableCandidate} from './candidate-budget.mjs';
import {filterCandidates} from './search-match.mjs';
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { PLATFORMS,productURL,parsePrice,extractCards,extractDetail,pageState } from './adapters.mjs';

export class Collector {
 constructor({root,saveImage,launchContext}={}){this.root=root;this.saveImage=saveImage;this.launchContext=launchContext;this.contexts=new Map();this.opening=new Map();this.jobs=new Map();this.active=new Map();fs.mkdirSync(path.join(root,'jobs'),{recursive:true});for(const f of fs.readdirSync(path.join(root,'jobs')).filter(f=>f.endsWith('.json'))){try{const j=JSON.parse(fs.readFileSync(path.join(root,'jobs',f),'utf8'));for(const s of Object.values(j.platforms))if(['running','queued'].includes(s.state)){s.state='interrupted';s.message='服务重启，可继续采集';}this.jobs.set(j.id,j);}catch{}}}
 persist(job){const file=path.join(this.root,'jobs',job.id+'.json'),temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(job));fs.renameSync(temp,file);}
 list(taskId){return [...this.jobs.values()].filter(j=>j.target.id===taskId).sort((a,b)=>b.createdAt.localeCompare(a.createdAt)).slice(0,1).map(j=>this.snapshot(j.id));}
 async context(platform,{visible=this.keepVisible===true}={}){
  if(!PLATFORMS[platform])throw Error('平台不支持');
  this.visibleContexts??=new Map();
  if(this.contexts.has(platform)&&this.visibleContexts.get(platform)!==visible)await this.contexts.get(platform).close();
  if(this.contexts.has(platform))return this.contexts.get(platform);
  if(this.opening.has(platform))return this.opening.get(platform);
  const pending=(async()=>{
   const dir=path.join(this.root,'profiles',platform);fs.mkdirSync(dir,{recursive:true});
   const options={headless:!visible,viewport:{width:1440,height:1000},locale:'zh-CN',acceptDownloads:false};
   const edgePaths=process.platform==='win32'?['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe']:process.platform==='darwin'?['/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']:['/usr/bin/microsoft-edge'];
   const executablePath=edgePaths.find(p=>fs.existsSync(p));if(executablePath)options.executablePath=executablePath;
   const context=this.launchContext?await this.launchContext(dir,options):await chromium.launchPersistentContext(dir,options);
   context.setDefaultTimeout(6000);context.on('close',()=>{this.contexts.delete(platform);this.visibleContexts.delete(platform);});this.contexts.set(platform,context);this.visibleContexts.set(platform,visible);return context;
  })();this.opening.set(platform,pending);try{return await pending;}finally{this.opening.delete(platform);}
 }
 async open(platform){if(this.active.has(platform))throw Error('平台正在后台采集，请等待结束后再打开登录窗口');const context=await this.context(platform,{visible:true});const page=context.pages().find(p=>!p.isClosed())||await context.newPage();if(page.url()==='about:blank')await page.goto(PLATFORMS[platform].home,{waitUntil:'domcontentloaded',timeout:30000});await page.bringToFront();return {state:'window_open',message:'窗口已打开；可在窗口中登录。后续查询在后台运行。'};}
 start(target,ids=['jd','taobao','pdd'],limit=10){
  if(!target?.name?.trim())throw Error('请填写商品名称');
  if(!Array.isArray(ids)||!ids.length||new Set(ids).size!==ids.length||ids.some(p=>!PLATFORMS[p]))throw Error('请选择有效平台');
  if(!Number.isInteger(limit)||limit<1||limit>20)throw Error('每个平台采集1至20条候选');
  if(ids.some(p=>this.active.has(p)))throw Error('所选平台已有采集任务，请先等待或停止');
  const job={id:randomUUID(),target,createdAt:new Date().toISOString(),limit,cancelled:false,platforms:Object.fromEntries(ids.map(p=>[p,{state:'queued',message:'等待采集',items:[]}]))};
  this.jobs.set(job.id,job);if(this.jobs.size>30){for(const [id,j] of this.jobs){if(!Object.values(j.platforms).some(s=>['running','queued'].includes(s.state))){this.jobs.delete(id);break;}}}
  this.persist(job);
  for(const p of ids){this.active.set(p,job.id);void this.collect(job,p);}
  return this.snapshot(job.id);
 }
 snapshot(id){const j=this.jobs.get(id);if(!j)throw Error('采集任务不存在或服务已重启，请重新采集');return structuredClone(j);}
 cancel(id){const j=this.jobs.get(id);if(!j)throw Error('任务不存在');j.cancelled=true;for(const s of Object.values(j.platforms)){if(['queued','login_required','verification_required'].includes(s.state)){s.state='cancelled';s.message='已停止';}}return this.snapshot(id);}
 async resume(id,platform){const j=this.jobs.get(id);if(!j||!j.platforms[platform])throw Error('任务不存在');if(j.cancelled)throw Error('已停止的任务请重新开始');if(this.active.has(platform))throw Error('平台正在采集');this.active.set(platform,id);void this.collect(j,platform,true);return this.snapshot(id);}
 async collect(job,platform,resume=false){
  const state=job.platforms[platform],cfg=PLATFORMS[platform];state.state='running';state.message='正在打开搜索结果';
  try{
   if(!resume){const previous=[...this.jobs.values()].filter(j=>j.id!==job.id&&j.platforms[platform]).sort((a,b)=>b.createdAt.localeCompare(a.createdAt))[0]?.platforms[platform];if(previous&&['login_required','verification_required'].includes(previous.state)){state.state=previous.state;state.message='此前该平台需要登录或验证，已暂停新搜索；完成后继续。';return;}}
   const context=await this.context(platform);const page=context.pages().find(p=>!p.isClosed())||await context.newPage();
   const keyword=[job.target.brand,job.target.name,job.target.model,job.target.spec,job.target.color,job.target.pack].filter(Boolean).join(' ');
   if(resume){const blocked=await pageState(page);if(blocked&&blocked.state!=='notfound'){Object.assign(state,blocked);return;}}
   // Reload the original query after login; otherwise keep a verification-cleared search page.
   if(!resume||!/(Search\?|\/search\?|search_result\.html)/.test(page.url()))await page.goto(cfg.search(keyword),{waitUntil:'domcontentloaded',timeout:30000});
   const issue=await pageState(page);if(issue){Object.assign(state,issue);return;}
   await page.locator(cfg.cards+','+cfg.links).first().waitFor({state:'visible',timeout:12000}).catch(()=>{});
   const blocked=await pageState(page);if(blocked){Object.assign(state,blocked);return;}
   for(let i=0;i<3&&!job.cancelled;i++){await page.mouse.wheel(0,650);await page.waitForTimeout(300);}
   const raw=await page.evaluate(extractCards,{cards:cfg.cards,links:cfg.links,title:cfg.title,price:cfg.price,shop:cfg.shop});state.received=raw.length;state.excluded=0;const seen=new Set(state.items.map(q=>q.url));
   for(const r of raw){
    if(job.cancelled)break;if(state.items.length>=job.limit)break;if(job.target.evidenceLimit&&state.items.filter(usableCandidate).length>=job.target.evidenceLimit)break;
    const url=productURL(r.url,platform);if(!url||!r.title||seen.has(url))continue;seen.add(url);
    const q={id:randomUUID(),platform:cfg.name,store:r.store,title:r.title,spec:'',url,price:parsePrice(r.priceRaw),match:'pending',conditions:`搜索页展示价：${r.priceRaw||'未读到'}；规格、优惠、运费和店铺待核验。`,checkedAt:new Date().toISOString(),evidence:[],targetSignature:'',sourceKind:'search',collectedAt:new Date().toISOString(),availability:'unknown',priceBasis:'listed',originUrl:url,sourceText:r.visibleText};
    if(job.target.demoKeyword){const checked=filterCandidates([q],job.target.demoKeyword);if(!checked.items.length){state.excluded++;continue;}Object.assign(q,checked.items[0]);}
    q.directProductId=url;q.searchPageUrl=page.url();
    if(job.target.evidenceLimit&&!(Number.isFinite(q.price)&&q.price>0)){state.items.push(q);continue;}
    const issue=await pageState(page);if(issue){Object.assign(state,issue);return;}
    try{const card=page.locator(`[data-guichang-card="${r.index}"]`);const current=(await card.innerText()).trim().slice(0,2500);if(current!==r.visibleText)throw Error('商品卡片已变化');const urls=await card.locator('a[href]').evaluateAll(a=>a.map(x=>x.href));const own=await card.evaluate(e=>e.href||'');if(![...urls,own].some(u=>productURL(u,platform)===url)&&await card.getAttribute('data-sku')!==url.match(/\/(\d+)\.html/)?.[1])throw Error('截图商品链接不符');const bytes=await card.screenshot({timeout:5000});q.evidence=[{...await this.saveImage(bytes,`${cfg.name}-候选-${state.items.length+1}.png`),kind:'origin',screenshotType:'search_card',sourceUrl:page.url(),quoteId:q.id,platform:q.platform,productId:q.directProductId,productUrl:q.url,productTitle:q.title,capturedAt:new Date().toISOString()}];}catch(e){q.evidenceError=e.message.split('\n')[0];q.conditions+=' 截图未保存。';}
    state.items.push(q);state.message=`已采集${state.items.length}条候选`;
   }
   if(job.cancelled){state.state='cancelled';state.message='已停止，保留已取得候选';}
   else if(state.items.length){state.state='collected';state.message=`取得${state.items.length}条候选，待核验同款及详情价格`;}
   else{state.state='page_changed';state.message='未能解析有效商品；不等同于未找到。可打开具体商品，用下方商品链接补充采集。';}
  }catch(e){state.state=job.cancelled?'cancelled':'error';state.message=job.cancelled?'已停止':`采集失败：${e.message.split('\n')[0]}`;}
  finally{this.persist(job);if(this.active.get(platform)===job.id)this.active.delete(platform);}
 }
 async detail(platform,value){
  const url=productURL(value,platform);if(!url)throw Error('请输入该平台的有效商品链接');if(this.active.has(platform))throw Error('该平台正在搜索，请稍后补充详情');this.active.set(platform,'detail');
  let page;try{
   const context=await this.context(platform);page=await context.newPage();await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
   let issue=await pageState(page);if(issue)return {...issue,items:[]};
   await page.waitForTimeout(1000);issue=await pageState(page);if(issue)return {...issue,items:[]};
   const raw=await page.evaluate(extractDetail,platform);if(!raw.title)return {state:'page_changed',message:'详情标题未识别，保留窗口供人工核验；可在工作台录入并上传截图。',items:[]};
   const actual=productURL(raw.url,platform);if(!actual)return {state:'page_changed',message:'页面已跳转，未形成商品报价',items:[]};
   const bytes=await page.screenshot({fullPage:false});const proof=await this.saveImage(bytes,`${PLATFORMS[platform].name}-详情.png`);
   return {state:'collected',message:'已读取当前详情页，须人工核对选中规格和价格',items:[{id:randomUUID(),platform:PLATFORMS[platform].name,store:raw.store,title:raw.title,spec:raw.spec,url:actual,price:parsePrice(raw.priceRaw),match:'pending',conditions:`详情展示价：${raw.priceRaw||'未读到'}；请核对选中规格、优惠条件与运费。截图为当时可见区域。`,checkedAt:new Date().toISOString(),evidence:[{...proof,kind:'origin',sourceUrl:actual,capturedAt:new Date().toISOString()}],collectedAt:new Date().toISOString(),originUrl:actual,availability:'unknown',priceBasis:'listed',targetSignature:'',sourceKind:'detail',sourceText:raw.visibleText}]};
  }finally{this.active.delete(platform);}
 }
 async close(){await Promise.allSettled([...this.contexts.values()].map(c=>c.close()));}
}

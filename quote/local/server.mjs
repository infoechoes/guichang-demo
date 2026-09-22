import {QuoteWorkflow} from './quote-workflow.mjs';
import {comparisonRunner} from './compare.mjs';
import {clearQuoteDrafts,quoteDraftRevision} from './clear-drafts.mjs';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID,randomBytes } from 'node:crypto';
import { Collector } from './collector.mjs';
import { Aggregator } from './aggregator.mjs';
import { DemoRuns } from './demo.mjs';
import { ImageRecognition } from './image-recognition.mjs';
import { exportDemoXlsx } from './demo-xlsx.mjs';
import { ExcelImports } from './excel-import.mjs';
import { ExcelBatch } from './excel-batch.mjs';
import { validateTask,normalizeTask } from '../lib/quotes.ts';
const project=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export function createApp({root=path.join(project,'.local-data'),collectorFactory,aggregatorFactory,recognizerFactory,gatewayKey=null,multiUser=false}={}){
 fs.mkdirSync(path.join(root,'files'),{recursive:true});fs.mkdirSync(path.join(root,'tasks'),{recursive:true});
 const token=randomBytes(32).toString('hex');
 function saveJSON(file,data){const temp=file+'.'+randomUUID()+'.tmp';fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);}
 async function saveImage(bytes,name){if(!bytes.length||bytes.length>5*1024*1024)throw Error('图片需为5MB以内');const b=Buffer.from(bytes),mime=b[0]===137&&b.subarray(1,4).toString()==='PNG'?'image/png':b[0]===255&&b[1]===216?'image/jpeg':b.subarray(0,4).toString()==='RIFF'&&b.subarray(8,12).toString()==='WEBP'?'image/webp':'';if(!mime)throw Error('仅支持PNG、JPEG或WebP');const id=randomUUID(),safeName=String(name).slice(0,180);fs.writeFileSync(path.join(root,'files',id),b);saveJSON(path.join(root,'files',id+'.json'),{mime,name:safeName});return {id,name:safeName,url:'/api/files/'+id};}
 const workflow=new QuoteWorkflow(root);
 const compare=comparisonRunner(root,saveImage);
 const collector=collectorFactory?collectorFactory({root,saveImage}):new Collector({root,saveImage});
 const aggregator=aggregatorFactory?aggregatorFactory({root,saveImage}):new Aggregator({root,saveImage});
 const demo=new DemoRuns({root,aggregator,saveImage,collector});
 const excelImports=new ExcelImports(root),batch=new ExcelBatch({root,demo,imports:excelImports});
 const recognizer=recognizerFactory?recognizerFactory({root}):new ImageRecognition({root});
 let exporting=false;
 const server=http.createServer(async(req,res)=>{
  const send=(status,data)=>{res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(data));};
  try{
   const host=req.headers.host;if(!/^127\.0\.0\.1:\d+$/.test(host||''))return send(403,{error:'仅允许本机访问'});
   const origin='http://'+host;const url=new URL(req.url,origin);
   if(req.headers.origin&&req.headers.origin!==origin)return send(403,{error:'请求来源不匹配'});
   if(req.headers['sec-fetch-site']==='cross-site')return send(403,{error:'拒绝跨站请求'});
   if(gatewayKey&&req.headers['x-gq-gateway']!==gatewayKey)return send(403,{error:'Internal gateway required'});
   if(multiUser&&req.method==='POST'&&['/api/aggregate/open','/api/collector/open','/api/demo/batch/open','/api/demo/supplement/open'].includes(url.pathname))return send(409,{error:'多人版不会在服务器弹出登录窗口。当前账号需要完成独立商城登录后才能继续；请联系管理员安排此账号的会话登录。'});
   if(url.pathname==='/api/session'&&req.method==='GET'&&multiUser)return send(200,{authenticated:true,mode:'multiuser'});
   if(url.pathname==='/api/session'&&req.method==='GET'){res.setHeader('Set-Cookie',`gq_session=${token}; HttpOnly; SameSite=Strict; Path=/`);return send(200,{authenticated:true,mode:'local',displayName:'本机工作台',capabilities:{browserCollection:'local',imageRecognition:false}});}
   if(url.pathname.startsWith('/api/')&&!gatewayKey){
    if(!(req.headers.cookie||'').split(';').some(c=>c.trim()===`gq_session=${token}`))return send(401,{error:'请重新打开本机工作台'});
    if(req.method!=='GET'&&req.headers.origin!==origin)return send(403,{error:'请求来源不匹配'});
   }
   let body;
   async function readBody(max=2000000){const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>max)throw Error('请求内容过大');chunks.push(chunk);}return Buffer.concat(chunks);}
   async function json(){return body??=JSON.parse((await readBody()).toString());}
   if(batch.active&&req.method==='POST'&&['/api/demo/run','/api/demo/supplement','/api/aggregate/read','/api/aggregate/evidence','/api/collector/jobs','/api/collector/detail'].includes(url.pathname))return send(409,{error:'Excel批量比价正在运行，请先停止或等待完成'});
   if(url.pathname==='/api/demo/excel/import'&&req.method==='POST'){
    const bytes=await readBody(11*1024*1024),request=new Request(origin+req.url,{method:'POST',headers:{'Content-Type':req.headers['content-type']||''},body:bytes});
    const file=(await request.formData()).get('file');if(!(file instanceof File))throw Error('请选择Excel文件');
    return send(200,await excelImports.upload(Buffer.from(await file.arrayBuffer()),file.name));
   }
   if(url.pathname==='/api/demo/excel/preview'&&req.method==='POST')return send(200,excelImports.preview(await json()));
   if(url.pathname==='/api/demo/drafts'&&req.method==='GET')return send(200,{revision:quoteDraftRevision(root)});
   if(url.pathname==='/api/demo/drafts/clear'&&req.method==='POST'){const input=await json();return send(200,clearQuoteDrafts({root,batch,demo,busy:exporting||aggregator.busy||recognizer.busy},input));}
   if(url.pathname==='/api/demo/batch'&&req.method==='GET')return send(200,{batch:batch.snapshot()});
   if(url.pathname==='/api/demo/batch/start'&&req.method==='POST')return send(202,batch.start(await json()));
   if(url.pathname==='/api/demo/batch/continue'&&req.method==='POST')return send(202,batch.continue(await json()));
   if(url.pathname==='/api/demo/batch/cancel'&&req.method==='POST')return send(200,batch.cancel());
   if(url.pathname==='/api/demo/batch/open'&&req.method==='POST'){
    if(batch.active)throw Error('批量采集正在运行');const pending=batch.job?.groups.find(g=>g.state==='paused');if(!pending)throw Error('没有需要验证的商品');
    return send(200,pending.pending==='supplement'?await collector.open(pending.result.supplement.pendingPlatform):await aggregator.open());
   }
   if(url.pathname==='/api/demo/batch/xlsx'&&req.method==='POST'){
    if(exporting||batch.active)throw Error('请等待采集或导出结束，也可先停止并导出已有结果');
    const b=await json(),snapshot=batch.snapshot();if(!snapshot||b.id!==snapshot.id)throw Error('批量任务已变化，请刷新后导出');
    if(b.title!==undefined&&(typeof b.title!=='string'||b.title.length>150))throw Error('报价标题过长');
    exporting=true;try{const output=path.join(root,'exports','latest-batch.xlsx');await exportDemoXlsx({batch:snapshot,root,output,options:{title:b.title}});res.writeHead(200,{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':"attachment; filename*=UTF-8''"+encodeURIComponent('商品三方比价汇总.xlsx'),'Cache-Control':'no-store'});res.end(fs.readFileSync(output));return;}finally{exporting=false;}
   }
   if(url.pathname==='/api/workflow'&&req.method==='GET')return send(200,workflow.view());
   if(url.pathname==='/api/workflow/import'&&req.method==='POST')return send(200,workflow.importRows(await json()));
   if(url.pathname==='/api/workflow/save'&&req.method==='POST')return send(200,workflow.save(await json()));
   if(url.pathname==='/api/workflow/export'&&req.method==='POST'){const bytes=await workflow.export(await json());res.writeHead(200,{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':'attachment; filename="quote-workflow.xlsx"','Cache-Control':'no-store'});res.end(bytes);return;}
   if(url.pathname==='/api/tasks'&&req.method==='GET'){const tasks=fs.readdirSync(path.join(root,'tasks')).filter(n=>n.endsWith('.json')).map(n=>JSON.parse(fs.readFileSync(path.join(root,'tasks',n),'utf8'))).map(normalizeTask).sort((a,b)=>b.updatedAt.localeCompare(a.updatedAt));return send(200,{tasks});}
   if(url.pathname==='/api/tasks'&&req.method==='POST'){
    const t=validateTask(await json()),file=path.join(root,'tasks',t.id+'.json');const old=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):null;
    if((old?.revision||0)!==t.revision)return send(409,{error:'任务已在另一个窗口更新，请重新加载'});
    const refs=[...(t.photo?[t.photo]:[]),...t.quotes.flatMap(q=>q.evidence)];if(refs.some(f=>!fs.existsSync(path.join(root,'files',f.id))))throw Error('凭证不存在，请重新采集或上传');
    if(!old&&fs.readdirSync(path.join(root,'tasks')).filter(n=>n.endsWith('.json')).length>=100)throw Error('最多保存100个任务');
    t.revision++;t.updatedAt=new Date().toISOString();saveJSON(file,t);return send(200,{task:t});
   }
   if(url.pathname==='/api/files'&&req.method==='POST'){
    const bytes=await readBody(6*1024*1024);const request=new Request(origin+req.url,{method:'POST',headers:{'Content-Type':req.headers['content-type']||''},body:bytes});const form=await request.formData(),file=form.get('file');if(!(file instanceof File))throw Error('请选择图片');const image=await saveImage(Buffer.from(await file.arrayBuffer()),file.name);const metaPath=path.join(root,'files',image.id+'.json');const meta=JSON.parse(fs.readFileSync(metaPath,'utf8'));meta.purpose=form.get('purpose')==='origin'?'origin':'input';saveJSON(metaPath,meta);return send(200,image);
   }
   const image=url.pathname.match(/^\/api\/files\/([a-f0-9-]{36})$/);if(image&&req.method==='GET'){const file=path.join(root,'files',image[1]);if(!fs.existsSync(file))return send(404,{error:'图片不存在'});const meta=JSON.parse(fs.readFileSync(file+'.json','utf8'));res.writeHead(200,{'Content-Type':meta.mime,'X-Content-Type-Options':'nosniff','Cache-Control':'private, max-age=60'});return fs.createReadStream(file).pipe(res);}
   if(url.pathname==='/api/demo/recognition-status'&&req.method==='GET')return send(200,recognizer.status());
   if(url.pathname==='/api/demo/recognize'&&req.method==='POST')return send(200,await recognizer.recognize(await json()));
   if(url.pathname==='/api/demo/status'&&req.method==='GET')return send(200,{busy:demo.busy,progress:demo.progress()});
   if(url.pathname==='/api/demo/cancel'&&req.method==='POST')return send(200,demo.cancel());
   if(url.pathname==='/api/demo/supplement'&&req.method==='POST'){const b=await json();return send(200,await demo.supplement({force:b.force===true,resume:b.resume===true,skip:b.skip===true}));}
   if(url.pathname==='/api/demo/supplement/open'&&req.method==='POST'){const platform=demo.latest()?.supplement?.pendingPlatform;if(!platform)throw Error('没有等待登录的平台');return send(200,await collector.open(platform));}
   if(url.pathname==='/api/demo/latest'&&req.method==='GET')return send(200,{result:demo.latest()});
   if(req.method==='POST'&&url.pathname==='/api/compare/open')return send(200,await compare.open(await json()));
   if(req.method==='POST'&&url.pathname==='/api/compare/resume')return send(200,await compare.resume(await json()));
   if(req.method==='POST'&&url.pathname==='/api/compare/close')return send(200,await compare.dismiss(await json()));
   if(url.pathname==='/api/compare/run'&&req.method==='POST')return send(200,await compare(await json()));
   if(url.pathname==='/api/demo/run'&&req.method==='POST')return send(200,await demo.run(await json()));
   if(url.pathname==='/api/demo/xlsx'&&req.method==='POST'){
    if(exporting)throw Error('正在生成Excel，请稍候');const b=await json(),result=demo.latest();if(!result||b.collectedAt!==result.collectedAt)throw Error('结果已变化，请恢复最新结果后导出');
    const options={};for(const k of ['title','unit','department','person','phone','company']){if(b[k]!==undefined&&(typeof b[k]!=='string'||b[k].length>150))throw Error('报价字段过长');options[k]=b[k]||'';}
    options.quantity=b.quantity;if(!Array.isArray(b.ids)||!b.ids.length||b.ids.length>40||b.ids.some(id=>typeof id!=='string'||!result.items.some(q=>q.id===id)))throw Error('请勾选有效的报价行');options.ids=b.ids;
    exporting=true;try{const output=path.join(root,'exports','latest-demo.xlsx');await exportDemoXlsx({result,root,output,options});res.writeHead(200,{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':"attachment; filename*=UTF-8''"+encodeURIComponent('平台比价测试报价单.xlsx'),'Cache-Control':'no-store'});res.end(fs.readFileSync(output));return;}finally{exporting=false;}
   }
   if(url.pathname==='/api/aggregate/status'&&req.method==='GET')return send(200,aggregator.status());
   if(url.pathname==='/api/aggregate/open'&&req.method==='POST')return send(200,await aggregator.open());
   if(url.pathname==='/api/aggregate/read'&&req.method==='POST')return send(200,await aggregator.read(await json()));
   if(url.pathname==='/api/aggregate/evidence'&&req.method==='POST')return send(200,await aggregator.evidence((await json()).id));
   if(url.pathname==='/api/collector/open'&&req.method==='POST')return send(200,await collector.open((await json()).platform));
   if(url.pathname==='/api/collector/jobs'&&req.method==='GET')return send(200,{jobs:collector.list(url.searchParams.get('taskId'))});
   if(url.pathname==='/api/collector/jobs'&&req.method==='POST'){const b=await json();return send(202,collector.start(validateTask(b.task),b.platforms,b.limit));}
   const job=url.pathname.match(/^\/api\/collector\/jobs\/([a-f0-9-]{36})(?:\/(resume|cancel))?$/);
   if(job){if(req.method==='GET'&&!job[2])return send(200,collector.snapshot(job[1]));if(req.method==='POST'&&job[2]==='resume')return send(202,await collector.resume(job[1],(await json()).platform));if(req.method==='POST'&&job[2]==='cancel')return send(200,collector.cancel(job[1]));}
   if(url.pathname==='/api/collector/detail'&&req.method==='POST'){const b=await json();return send(200,await collector.detail(b.platform,b.url));}
   if(url.pathname.startsWith('/api/'))return send(404,{error:'接口不存在'});
   const staticRoot=path.join(project,'local-dist');const rel=decodeURIComponent(url.pathname).replace(/^\/+/,''),candidate=path.resolve(staticRoot,rel||'index.html');
   if(!candidate.startsWith(staticRoot+path.sep))return send(403,{error:'路径无效'});
   const file=fs.existsSync(candidate)&&fs.statSync(candidate).isFile()?candidate:path.join(staticRoot,'index.html');
   if(!fs.existsSync(file))return send(503,{error:'请先运行 pnpm build:local'});
   const mime={'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.json':'application/json'}[path.extname(file)]||'application/octet-stream';res.writeHead(200,{'Content-Type':mime,'X-Content-Type-Options':'nosniff'});fs.createReadStream(file).pipe(res);
  }catch(e){send(e.status||400,{error:e.message.split('\n')[0]});}
 });return {server,collector,aggregator,compare,root};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const app=createApp();const port=Number(process.env.GQ_PORT||4317);app.server.listen(port,'127.0.0.1',()=>console.log(`贵昌三方比价平台：http://127.0.0.1:${port}\n需要登录时请在平台窗口中完成；本机资料不会自动上传。`));
 app.server.on('error',e=>{console.error(e.message);process.exitCode=1;});
 for(const signal of ['SIGINT','SIGTERM'])process.on(signal,async()=>{await app.compare.close();await app.collector.close();await app.aggregator.close();app.server.close(()=>process.exit(0));});
}

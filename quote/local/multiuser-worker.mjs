import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {timingSafeEqual} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {createApp} from './server.mjs';

export function createWorker({root,key,factory=createApp,maxUsers=64}){
 if(!key||key.length<32)throw Error('Internal authentication is required');
 const apps=new Map();
 const server=http.createServer((req,res)=>{
  const deny=(status,message)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({error:message}));};
  const supplied=Buffer.from(req.headers['x-gq-gateway']||''),expected=Buffer.from(key);
  if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected))return deny(403,'Internal gateway required');
  const identity=req.headers['x-gq-user'];
  if(!/^[a-f0-9]{32}$/.test(identity||''))return deny(403,'Invalid workspace');
  if(!req.url.startsWith('/api/'))return deny(404,'Not found');
  if(!apps.has(identity)){
   if(apps.size>=maxUsers)return deny(503,'当前比价工作区数量达到上限，请联系管理员');
   apps.set(identity,factory({root:path.join(root,'users',identity),gatewayKey:key,multiUser:true}));
  }
  apps.get(identity).server.emit('request',req,res);
 });
 return {server,apps,async close(){for(const app of apps.values()){await app.compare?.close();await app.collector?.close();await app.aggregator?.close();}await new Promise(resolve=>server.close(resolve));}};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
 const root=process.env.GQ_MULTIUSER_ROOT,key=process.env.GQ_GATEWAY_KEY,ready=process.env.GQ_READY_FILE;
 if(!root||!ready)throw Error('Missing private worker configuration');
 delete process.env.GQ_GWDANG_PROFILE;
 const worker=createWorker({root,key});
 worker.server.listen(0,'127.0.0.1',()=>fs.writeFileSync(ready,JSON.stringify({port:worker.server.address().port,pid:process.pid})));
 for(const signal of ['SIGINT','SIGTERM'])process.on(signal,async()=>{await worker.close();process.exit(0);});
 // The facade owns this worker. Exit if it disappears, avoiding orphan services.
 const parent=process.ppid;setInterval(()=>{try{process.kill(parent,0);}catch{process.exit(0);}},10000).unref();
}

import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
const names=['excel-batch.json','demo-latest.json'];
export function quoteDraftRevision(root){
 const hash=createHash('sha256');
 for(const name of names){hash.update(name);const file=path.join(root,name);hash.update(fs.existsSync(file)?fs.readFileSync(file):'<absent>');}
 return hash.digest('hex');
}
export function clearQuoteDrafts({root,batch,demo,busy=false},input){
 if(busy||batch.active||demo.busy)throw Error('有识别、采集或导出正在运行，请结束后再清空');
 if(input?.confirmation!=='清空三方比价草稿')throw Error('请先确认清空范围');
 if(input.revision!==quoteDraftRevision(root))throw Error('草稿已在其他窗口更新，请重新确认后清空');
 const existing=names.filter(name=>fs.existsSync(path.join(root,name)));
 const backup=path.join(root,'draft-trash',randomUUID()),moved=[];
 if(existing.length)fs.mkdirSync(backup,{recursive:true});
 try{for(const name of existing){fs.renameSync(path.join(root,name),path.join(backup,name));moved.push(name);}}
 catch(error){for(const name of moved.reverse())fs.renameSync(path.join(backup,name),path.join(root,name));throw error;}
 batch.job=null;demo.pendingKeyword='';
 return {cleared:true,message:'三方比价草稿已清空。已导出文件、原始资料及截图保留，原批量记录已留恢复副本。'};
}

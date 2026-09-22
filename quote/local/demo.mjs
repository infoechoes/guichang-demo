import {captureCandidates,candidateLimit} from './candidate-budget.mjs';
import {SupplementSearch} from './supplement.mjs';
import {MultiPageSearch} from './multi-page.mjs';
import {quoteEvidence} from '../public/quote-evidence.js';
import {filterCandidates} from './search-match.mjs';
import fs from 'node:fs';import path from 'node:path';
export class DemoRuns{
 constructor({root,aggregator,saveImage,collector}){this.file=path.join(root,'demo-latest.json');this.aggregator=aggregator;this.saveImage=saveImage;this.busy=false;this.scanner=new MultiPageSearch(aggregator);this.supplementer=collector?new SupplementSearch(collector):null;}
 latest(){try{const saved=JSON.parse(fs.readFileSync(this.file,'utf8')),filtered=filterCandidates(saved.items,saved.keyword||saved.input?.query);if(!this.busy&&saved.supplement?.state==='running')saved.supplement={...saved.supplement,state:'interrupted',message:'上次补查被中断，可继续；已有结果保留。'};return filtered.items.length||saved.supplement?{...saved,...filtered,platformCoverage:saved.platformCoverage?.map(p=>({...p,kept:filtered.items.filter(q=>q.platform===p.platform&&q.sourceKind!=='search').length})),excludedCount:(saved.excludedCount||0)+filtered.excludedCount}:null;}catch{return null;}}
 save(result){const out={...result,finishedAt:new Date().toISOString()};const temp=this.file+'.tmp';fs.writeFileSync(temp,JSON.stringify(out));fs.renameSync(temp,this.file);return out;}
 progress(){return this.supplementer?.progress?.state==='running'?this.supplementer.progress:this.scanner.progress;}
 cancel(){this.supplementer?.cancel();return this.scanner.cancel();}
 async supplement(options={}){if(this.busy)throw Error('采集进行中，请等待');if(!this.supplementer)throw Error('原商城采集未配置');const result=this.latest();if(!result)throw Error('请先按商品关键词搜索');this.busy=true;try{return await this.supplementer.run(result,options,r=>this.save(r));}finally{this.busy=false;}}
 async run(input){if(this.busy)throw Error('单次测试正在进行，请等待结果');if(!input||typeof input!=='object')throw Error('请求无效');if(input.keyword!==undefined&&(typeof input.keyword!=='string'||input.keyword.length>200))throw Error('检索词最多200字');if(input.url!==undefined&&(typeof input.url!=='string'||input.url.length>2000))throw Error('商品链接无效');
 let imageInput=null;if(input.input){const src=input.input,id=src.image?.id;if(!/^[a-f0-9-]{36}$/.test(id||'')||!fs.existsSync(path.join(path.dirname(this.file),'files',id)))throw Error('需求图片不存在，请重新上传');const fields={};for(const k of ['brand','model','name','spec','color','pack']){if(typeof src.fields?.[k]!=='string'||src.fields[k].length>100)throw Error('图片商品字段无效');fields[k]=src.fields[k];}if(typeof src.rawText!=='string'||src.rawText.length>160000||typeof src.query!=='string'||src.query.length>200)throw Error('图片识别信息无效');imageInput={image:{id,url:'/api/files/'+id},fields,rawText:src.rawText,query:src.query,recognizedAt:src.recognizedAt,engine:'本机文字识别与人工补充',reviewStatus:'search_clues_not_verified_product'};}
 const limit=candidateLimit(input.candidatesPerPlatform);const maxPages=input.maxPages??1;if(!Number.isInteger(maxPages)||maxPages<1||maxPages>5)throw Error('最多读取1至5页');
 this.scanner.cancelled=false;this.busy=true;const startedAt=new Date().toISOString();try{
 const keyword=input.keyword||imageInput?.query||(input.current?this.pendingKeyword||'':'');if(keyword)this.pendingKeyword=keyword;const previous=this.latest(),multi=maxPages>1||(input.current&&previous?.search?.resumable);if(!imageInput&&input.current&&previous?.keyword===keyword)imageInput=previous.input;const checkpoint=async r=>{if(r.items?.length){const tmp=this.file+'.tmp';fs.writeFileSync(tmp,JSON.stringify({...r,mode:'live',keyword,candidatesPerPlatform:limit,input:imageInput,startedAt,finishedAt:new Date().toISOString(),screenshot:null,screenshotError:'逐商品截图已保存',notice:'候选尚未核验选中SKU和成交条件。'}));fs.renameSync(tmp,this.file);}};const raw=multi?await this.scanner.run({keyword,url:input.url||'',current:input.current===true,maxPages,candidatesPerPlatform:limit},previous,checkpoint):await this.aggregator.read({keyword,url:input.url||'',current:input.current===true});const filtered=filterCandidates(raw.items||[],keyword);const result={...raw,...filtered,excludedCount:(raw.excludedCount||0)+filtered.excludedCount};if(raw.items?.length&&!result.items.length){result.state='no_match';result.message='没有找到符合检索条件的商品；无关结果已排除。';}
 if(result.cached){const previous=this.latest();for(const q of result.items){const old=previous?.items.find(o=>o.id===q.id&&o.collectedAt===q.collectedAt);if(old&&quoteEvidence(old))q.evidence=old.evidence;}}
 if(!result.items?.length&&!(input.supplement===true&&keyword&&result.state!=='verification_required'&&this.supplementer))return {...result,mode:'live',startedAt,finishedAt:new Date().toISOString()};
 let screenshot=null,screenshotError='';
 if(multi)screenshotError='多页采集按商品保存截图，请查看每条报价对应的图片。';
 else if(result.cached)screenshotError='缓存候选未重新打开页面，不附上旧页面的新截图。';
 else screenshotError='截图随对应商品保存，缺失会在商品行标明；不重复生成整页截图。';
 if(!multi)result.items=await captureCandidates(result.items,{limit,capture:result.cached||!this.aggregator.evidence?null:id=>this.aggregator.evidence(id),cancelled:()=>this.scanner.cancelled});
 const saved={...result,collectedAt:result.collectedAt||startedAt,mode:'live',candidatesPerPlatform:limit,keyword:keyword||result.keyword||'',input:imageInput,startedAt,finishedAt:new Date().toISOString(),screenshot,screenshotError,notice:'测试候选，尚未核实同款、库存和当前成交条件；聚合页面截图不等同于原商城报价凭证。'};
 const temp=this.file+'.tmp';fs.writeFileSync(temp,JSON.stringify(saved));fs.renameSync(temp,this.file);if(input.supplement===true&&keyword&&result.state!=='verification_required'&&!this.scanner.cancelled&&this.supplementer)return await this.supplementer.run(saved,{},r=>this.save(r));return saved;
 }finally{this.busy=false;}}
}

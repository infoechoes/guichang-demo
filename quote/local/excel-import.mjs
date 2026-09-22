import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

export const columns={
 name:['商品名称','物品名称','产品名称','品名','材料名称','名称'],
 brand:['品牌','品牌名称'],spec:['规格型号','需求参数/型号','需求参数','规格/型号','规格','型号'],
 quantity:['当前数量','采购数量','需求数量','原需求数量','数量'],unit:['当前单位','原需求单位','计量单位','单位'],
 department:['需求科室','科室','使用部门','需求部门','部门','送货地点'],sourceRow:['原行号','来源行号','序号'],
};
const normalized=v=>String(v??'').normalize('NFKC').replace(/\s/g,'').toLowerCase();
export function detectHeader(sheet){
 let best={row:sheet.rows[0]?.number||1,mapping:{},score:0};
 for(const row of sheet.rows.slice(0,40)){
  const mapping={};
  for(const [key,aliases] of Object.entries(columns)){
   mapping[key]=-1;
   for(const alias of aliases){const i=row.cells.findIndex(c=>normalized(c)===normalized(alias));if(i>=0){mapping[key]=i;break;}}
  }
  const score=(mapping.name>=0?10:0)+Object.values(mapping).filter(i=>i>=0).length;
  if(score>best.score)best={row:row.number,mapping,score};
 }
 return best;
}
export function extractRows(sheet,headerRow,mapping){
 if(!Number.isInteger(headerRow)||!sheet.rows.some(r=>r.number===headerRow))throw Error('请选择有效的表头行');
 for(const key of Object.keys(columns))if(!Number.isInteger(mapping[key])||mapping[key]<-1||mapping[key]>99)throw Error('列对应关系无效');
 if(mapping.name<0)throw Error('请指定商品名称所在列');
 const rows=[];
 for(const r of sheet.rows.filter(r=>r.number>headerRow)){
  const get=key=>mapping[key]<0?'':String(r.cells[mapping[key]]??'').trim();
  const name=get('name');if(!name||/^(合计|小计|总计|总金额)([：:\s]|$)/.test(name)||columns.name.some(a=>normalized(a)===normalized(name)))continue;
  const rawQuantity=get('quantity'),n=Number(rawQuantity.replaceAll(',',''));
  const quantity=rawQuantity!==''&&Number.isFinite(n)&&n>0?n:null;
  const row={id:sheet.name+':'+r.number,sheet:sheet.name,excelRow:r.number,sourceRow:get('sourceRow')||String(r.number),name,brand:get('brand'),spec:get('spec'),unit:get('unit'),department:get('department'),quantity,rawQuantity};
  row.query=[row.brand,row.name,row.spec].filter((v,i,a)=>v&&!a.slice(0,i).some(x=>x.includes(v))).join(' ');
  row.warnings=[...(!row.spec?['规格缺失']:[]),...(!row.unit?['单位缺失']:[]),...(quantity===null?['数量缺失或无效']:[]),...(row.query.length>200?['搜索词超过200字，请精简']:[])];
  rows.push(row);
 }
 if(rows.length>200)throw Error('一次最多识别200条商品，请拆分工作表');
 if(!rows.length)throw Error('所选表头下没有商品，请检查工作表、表头行和商品名称列');
 return rows;
}
export function groupRows(rows){
 const groups=new Map();
 for(const row of rows){
  const key=JSON.stringify(['name','brand','spec','unit','query'].map(k=>normalized(row[k])));
  if(!groups.has(key))groups.set(key,{id:randomUUID(),name:row.name,brand:row.brand,spec:row.spec,unit:row.unit,query:row.query,requests:[]});
  groups.get(key).requests.push(row);
 }
 return [...groups.values()].map(g=>({...g,quantity:g.requests.every(r=>r.quantity!==null)?g.requests.reduce((n,r)=>n+r.quantity,0):null,department:[...new Set(g.requests.map(r=>r.department).filter(Boolean))].join('；'),state:'queued',result:null,message:'等待比价'}));
}
export class ExcelImports{
 constructor(root){this.dir=path.join(root,'excel-imports');fs.mkdirSync(this.dir,{recursive:true});}
 read(id){if(!/^[a-f0-9-]{36}$/.test(id||''))throw Error('导入记录无效');try{return JSON.parse(fs.readFileSync(path.join(this.dir,id+'.json'),'utf8'));}catch{throw Error('导入记录不存在，请重新上传');}}
 async upload(bytes,name){
  if(!/\.xlsx?$/i.test(name))throw Error('请选择.xls或.xlsx文件');
  if(!bytes.length||bytes.length>10*1024*1024)throw Error('请选择10MB以内的Excel文件');
  const data=await readExcel(bytes),id=randomUUID();
  const sheets=data.sheets.map(s=>({...s,header:detectHeader(s)}));
  const selected=Math.max(0,sheets.findIndex(s=>!s.hidden&&s.header.mapping.name>=0));
  const saved={id,name,createdAt:new Date().toISOString(),sheets,selected};
  fs.writeFileSync(path.join(this.dir,id+'.json'),JSON.stringify(saved));return saved;
 }
 preview({id,sheetIndex,headerRow,mapping}){const book=this.read(id),sheet=book.sheets[sheetIndex];if(!sheet)throw Error('工作表不存在');return {rows:extractRows(sheet,headerRow,mapping)};}
}
export function readExcel(bytes){
 const python=process.env.GQ_EXCEL_PYTHON||path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
 const file=fileURLToPath(new URL('./excel_read.py',import.meta.url));
 return new Promise((resolve,reject)=>{
  const child=spawn(python,['-B',file],{windowsHide:true,stdio:['pipe','pipe','pipe']});let out='',done=false;
  const finish=(err,data)=>{if(done)return;done=true;clearTimeout(timer);err?reject(err):resolve(data);};
  const timer=setTimeout(()=>{child.kill();finish(Error('Excel识别超过30秒，请缩小工作表后重试'));},30000);
  child.on('error',()=>finish(Error('Excel读取引擎未配置，请设置GQ_EXCEL_PYTHON并安装openpyxl和xlrd')));
  child.stdout.setEncoding('utf8');child.stdout.on('data',s=>{out+=s;if(out.length>20*1024*1024){child.kill();finish(Error('表格内容过多，请拆分后上传'));}});
  child.stderr.on('data',()=>{});child.stdin.on('error',()=>{});
  child.on('close',code=>{try{const r=JSON.parse(out);if(r.error||code)throw Error(r.error||'Excel识别失败');finish(null,r);}catch(e){finish(Error(e.message.startsWith('Unexpected')?'Excel识别失败':e.message));}});child.stdin.end(bytes);
 });
}

const labels={name:'商品名称',brand:'品牌',spec:'规格型号',quantity:'数量',unit:'单位',department:'需求科室',sourceRow:'原行号'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function setupExcelInput({api,onLock,getOptions,download,onShow}){
 const $=id=>document.getElementById(id);let book=null,rows=[],batch=null,mapping={},externalBusy=false,working=false,timer=null;
 const note=(text,error=false)=>{$('excel-status').textContent=text;$('excel-status').classList.toggle('error',error);};
 function controls(){
  const locked=working||externalBusy||!!batch?.active;
  $('excel-file').disabled=locked;$('excel-sheet').disabled=locked;$('excel-header').disabled=locked;
  $('excel-preview').disabled=locked||!book;$('excel-start').disabled=locked||!rows.some(r=>r.selected);
  $('excel-stop').disabled=!batch?.active||working;
  $('excel-continue').hidden=!batch||!['paused','cancelled','interrupted','error'].includes(batch.state);$('excel-continue').disabled=locked;
  $('excel-skip').hidden=batch?.state!=='paused';$('excel-skip').disabled=locked;
  $('excel-login').hidden=batch?.state!=='paused';$('excel-login').disabled=locked;
  $('excel-export').disabled=locked||!batch;$('excel-title').disabled=locked;
  for(const input of $('excel-mapping').querySelectorAll('select'))input.disabled=locked;
  for(const input of $('excel-rows').querySelectorAll('input'))input.disabled=locked;
  $('excel-select-all').disabled=locked||!rows.length;
 }
 async function action(fn){if(working)return;working=true;onLock(true);controls();try{await fn();}catch(e){note(e.message,true);}finally{working=false;onLock(!!batch?.active);controls();}}
 function mapUI(){
  const sheet=book.sheets[Number($('excel-sheet').value)],header=sheet.rows.find(r=>r.number===Number($('excel-header').value));
  const cells=header?.cells||[];
  $('excel-mapping').innerHTML=Object.entries(labels).map(([key,label])=>`<label>${label}<select data-map="${key}"><option value="-1">未识别／不使用</option>${cells.map((v,i)=>`<option value="${i}" ${mapping[key]===i?'selected':''}>第${i+1}列：${esc(v||'空表头')}</option>`).join('')}</select></label>`).join('');
  for(const select of $('excel-mapping').querySelectorAll('select'))select.onchange=()=>{mapping[select.dataset.map]=Number(select.value);};
 }
 function rowUI(){
  $('excel-preview-area').hidden=false;
  $('excel-rows').innerHTML=rows.map((r,i)=>`<tr><td><input type="checkbox" data-index="${i}" data-field="selected" ${r.selected?'checked':''} aria-label="选择第${r.excelRow}行"><small>${esc(r.sheet)} · Excel第${r.excelRow}行／原行${esc(r.sourceRow)}</small></td>${['name','brand','spec','quantity','unit','department','query'].map(k=>`<td><input data-index="${i}" data-field="${k}" value="${esc(r[k])}" aria-label="第${r.excelRow}行${labels[k]||'搜索词'}" ${k==='quantity'?'type="number" min="0.01" step="any"':`maxlength="${k==='query'?200:500}"`}></td>`).join('')}<td>${esc(r.warnings.join('；')||'已读取字段')}</td></tr>`).join('');
  for(const input of $('excel-rows').querySelectorAll('input'))input.oninput=()=>{
   const r=rows[Number(input.dataset.index)],key=input.dataset.field;r[key]=key==='selected'?input.checked:input.value;
   if(['name','brand','spec'].includes(key)){r.query=[r.brand,r.name,r.spec].filter((v,i,a)=>v&&!a.slice(0,i).some(x=>x.includes(v))).join(' ');$('excel-rows').querySelector(`input[data-index="${input.dataset.index}"][data-field="query"]`).value=r.query;}
   controls();
  };
  controls();
 }
 async function preview(){
  const data=await api('demo/excel/preview',{id:book.id,sheetIndex:Number($('excel-sheet').value),headerRow:Number($('excel-header').value),mapping});
  rows=data.rows.map(r=>({...r,selected:true}));rowUI();note(`已识别 ${rows.length} 条需求。可直接开始，也可调整字段和搜索词；重复商品只检索一次。`);
 }
 async function selectSheet(){const sheet=book.sheets[Number($('excel-sheet').value)];mapping={...Object.fromEntries(Object.keys(labels).map(k=>[k,-1])),...sheet.header.mapping};$('excel-header').value=sheet.header.row;mapUI();await preview();}
 function showBatch(job){
  batch=job;if(!batch)return;onLock(working||!!batch.active);$('excel-batch-area').hidden=false;
  const done=batch.groups.filter(g=>!['queued','running','paused','cancelled'].includes(g.state)).length;
  $('excel-progress').textContent=`${batch.fileName}：${batch.rows.length}条需求，合并为${batch.groups.length}种商品，已处理${done}种。${batch.message}`;
  $('excel-progress-bar').max=batch.groups.length;$('excel-progress-bar').value=done;
  $('excel-batch-rows').innerHTML=batch.groups.map((g,i)=>`<tr><td>${i+1}</td><td>${esc(g.name)}<small>${esc(g.spec)}</small></td><td>${esc(g.message)}</td><td>${g.result?.items?.length?`<button class="secondary" data-group="${i}">查看候选</button>`:''}</td></tr>`).join('');
  for(const b of $('excel-batch-rows').querySelectorAll('button'))b.onclick=()=>onShow(batch.groups[Number(b.dataset.group)].result);
  controls();
  clearTimeout(timer);if(batch.active)timer=setTimeout(poll,2000);
 }
 async function poll(){try{const data=await api('demo/batch');if(data.batch)showBatch(data.batch);}catch(e){note('读取进度失败，可刷新恢复：'+e.message,true);timer=setTimeout(poll,5000);}}
 $('excel-file').onchange=()=>action(async()=>{
  const file=$('excel-file').files[0];if(!file)return;if(!/\.xlsx?$/i.test(file.name))throw Error('请上传.xls或.xlsx文件');if(file.size>10*1024*1024)throw Error('文件不能超过10MB');
  note('正在读取Excel表头和商品清单…');const form=new FormData();form.set('file',file);
  const response=await fetch('/api/demo/excel/import',{method:'POST',body:form}),data=await response.json();if(!response.ok)throw Error(data.error||'上传失败');
  book=data;rows=[];$('excel-rows').innerHTML='';$('excel-settings').hidden=false;
  $('excel-sheet').innerHTML=book.sheets.map((s,i)=>`<option value="${i}">${esc(s.name)}${s.hidden?'（隐藏表）':''}</option>`).join('');$('excel-sheet').value=book.selected;
  $('excel-title').value=file.name.replace(/\.xlsx?$/i,'')+'－三方比价汇总';await selectSheet();
 });
 $('excel-sheet').onchange=()=>action(selectSheet);$('excel-header').onchange=()=>{mapUI();rows=[];$('excel-rows').innerHTML='';controls();note('表头行已变化，请点击“重新识别清单”。');};
 $('excel-preview').onclick=()=>action(preview);
 $('excel-select-all').onchange=()=>{for(const r of rows)r.selected=$('excel-select-all').checked;rowUI();};
 $('excel-start').onclick=()=>action(async()=>{const data=await api('demo/batch/start',{importId:book.id,rows:rows.filter(r=>r.selected),...getOptions()});showBatch(data);note('已开始后台批量比价，刷新或关闭网页不影响当前队列。');});
 $('excel-stop').onclick=()=>action(async()=>{note((await api('demo/batch/cancel',{})).message);await poll();});
 $('excel-continue').onclick=()=>action(async()=>showBatch(await api('demo/batch/continue',{})));
 $('excel-skip').onclick=()=>action(async()=>showBatch(await api('demo/batch/continue',{skip:true})));
 $('excel-login').onclick=()=>action(async()=>note((await api('demo/batch/open',{})).message));
 $('excel-export').onclick=()=>action(async()=>{
  note('正在生成汇总Excel和逐商品截图…');const response=await fetch('/api/demo/batch/xlsx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:batch.id,title:$('excel-title').value})});
  if(!response.ok)throw Error((await response.json()).error||'导出失败');download(await response.blob(),'商品三方比价汇总.xlsx');note('已导出同一个Excel，包含报价单、截图凭证和需求清单。');
 });
 return {setBusy(value){externalBusy=value;controls();},async init(){await poll();controls();}};
}

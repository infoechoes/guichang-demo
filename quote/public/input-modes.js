export function setupInputModes(){
 const tabs=[...document.querySelectorAll('[data-mode]')];
 const names=tabs.map(t=>t.dataset.mode);
 function select(mode,{focus=false}={}){
  if(!names.includes(mode))mode='keyword';
  for(const tab of tabs){const active=tab.dataset.mode===mode;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;document.getElementById(tab.getAttribute('aria-controls')).hidden=!active;if(active&&focus)tab.focus();}
  try{sessionStorage.setItem('quote-input-mode',mode);}catch{}
 }
 let initial=new URLSearchParams(location.search).get('mode');
 if(!initial)try{initial=sessionStorage.getItem('quote-input-mode');}catch{}
 select(initial||'keyword');
 for(const [index,tab] of tabs.entries()){
  tab.onclick=()=>select(tab.dataset.mode);
  tab.onkeydown=e=>{const next=e.key==='ArrowRight'?(index+1)%tabs.length:e.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:e.key==='Home'?0:e.key==='End'?tabs.length-1:null;if(next!==null){e.preventDefault();select(names[next],{focus:true});}};
 }
 for(const button of document.querySelectorAll('[data-example]'))button.onclick=()=>{const input=document.getElementById('keyword');if(input.disabled||document.getElementById('run').disabled)return;input.value=button.dataset.example;input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();};
 document.addEventListener('quote:image-picked',()=>select('image'));
 for(const zone of document.querySelectorAll('[data-drop-input]')){
  const input=document.getElementById(zone.dataset.dropInput),label=document.getElementById(input.id==='excel-file'?'excel-file-name':'image-file-name');
  input.addEventListener('change',()=>{if(input.files[0])label.textContent=input.files[0].name;});
  zone.addEventListener('dragover',e=>{e.preventDefault();if(!input.disabled)zone.classList.add('dragging');});
  zone.addEventListener('dragleave',()=>zone.classList.remove('dragging'));
  zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('dragging');if(input.disabled||!e.dataTransfer.files.length)return;const data=new DataTransfer();data.items.add(e.dataTransfer.files[0]);input.files=data.files;input.dispatchEvent(new Event('change',{bubbles:true}));});
 }
 const update=()=>{document.getElementById('settings-summary').textContent=[document.getElementById('candidate-limit').value==='1'?'快速模式':'扩展模式',document.getElementById('max-pages').selectedOptions[0].textContent,document.getElementById('auto-supplement').checked?'自动补查':'仅聚合搜索'].join(' · ');};
 for(const id of ['max-pages','candidate-limit','auto-supplement'])document.getElementById(id).addEventListener('change',update);
 update();return {select};
}

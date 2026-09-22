"""Geometry-based extraction. No product catalogue, sample names or fixture values."""
import re, statistics

UNITS=r'(?:千克|公斤|kg|个|件|张|本|包|卷|台|套|盒|只|瓶|支|袋|箱|米|粒|双|桶|提|根|块)'

def expand_headers(tokens):
 """Separate fused header labels; positions come from the observed text box."""
 result=[]
 for token in tokens:
  text=token['text']
  parts=list(re.finditer(r'金额|单位|单(?!价)|生产厂家|生产厂|来源库位|目的库位',text))
  if not ((text.startswith('金额') or text.startswith('来源库位')) and len(parts)>=2):
   result.append(token);continue
  left,top,right,bottom=geometry(token)
  for match in parts:
   x1=left+(right-left)*match.start()/len(text);x2=left+(right-left)*match.end()/len(text)
   result.append({**token,'text':'单位' if match.group()=='单' else match.group(),'box':[[x1,top],[x2,top],[x2,bottom],[x1,bottom]]})
 return result

def geometry(t):
 p=t['box'];return min(x for x,y in p),min(y for x,y in p),max(x for x,y in p),max(y for x,y in p)
def number(text):
 text=re.sub(r'[,，￥¥元\s]','',text)
 return float(text) if re.fullmatch(r'-?\d+(?:\.\d+)?',text) else None
def classify(text):
 t=re.sub(r'[\s.。…，,:：]','',text)
 if any(x in t for x in ['待处理数量','已发数量','总数量','合计数量']):return None
 if t in ['产品','行产品','行号产品','行商品','商品','品名','名称','物品名称','商品名称','产品名称','材料名称']:return 'name'
 if any(x in t for x in ['规格','型号','需求参数']):return 'spec'
 if t in ['品牌','品']:return 'brand'
 if t in ['数量','需求数量','采购数量','订购数量','申请数量','处理数量']:return 'quantity'
 if t in ['参考价格','参考价','价格','单价','销售价','含税单价']:return 'price'
 if t in ['金额','金额单','金额单位','总价','含税金额'] or t.startswith('金额单生产厂'):return 'amount'
 if t in ['单位','计量单位','单']:return 'unit'
 if t in ['生产厂','生产厂家','厂家','供应商']:return 'manufacturer'
 if t in ['商品编码','产品编码','编码']:return 'code'
 if t.startswith(('来源库','目的库')):return 'ignore'
 return None

def extract_rows(tokens):
 if not tokens:return [],['未识别到文字，请换用清晰原图。']
 tokens=expand_headers(tokens)
 ts=[dict(t,x=(geometry(t)[0]+geometry(t)[2])/2,y=(geometry(t)[1]+geometry(t)[3])/2,h=geometry(t)[3]-geometry(t)[1]) for t in tokens]
 headers=[(i,t,classify(t['text'])) for i,t in enumerate(ts) if classify(t['text'])]
 # Header row may be tilted in a photograph. Group within 2.5 text heights.
 best=[]
 for _,anchor,kind in headers:
  if kind!='name':continue
  group=[(i,t,k) for i,t,k in headers if abs(t['y']-anchor['y'])<max(t['h'],anchor['h'])*2.5]
  if len(set(k for _,_,k in group))>len(set(k for _,_,k in best)):best=group
 if len(set(k for _,_,k in best))<3:return extract_plain_text(ts)
 # Fit header slope, so a skewed photo still groups each physical row.
 points=[(t['x'],t['y']) for _,t,_ in best];mx=statistics.mean(x for x,y in points);my=statistics.mean(y for x,y in points)
 denom=sum((x-mx)**2 for x,y in points);slope=sum((x-mx)*(y-my) for x,y in points)/denom if denom else 0
 baseline=my-slope*mx
 height=statistics.median(t['h'] for _,t,_ in best)
 columns=sorted([(t['x'],kind,geometry(t)[0],geometry(t)[2]) for _,t,kind in best])
 # Include ignored quantity column as a barrier so it cannot leak into requested quantity.
 for t in ts:
  if abs(t['y']-slope*t['x']-baseline)<height*1.5 and '待处理数量' in t['text']:columns.append((t['x'],'ignore',geometry(t)[0],geometry(t)[2]))
 columns.sort()
 first=columns[0][0];last=columns[-1][0]
 body=[t for t in ts if t['y']-slope*t['x']>baseline+height*.65]
 # Stop at the table footer; browser tabs, taskbar labels and document IDs below
 # the table are not product rows, even if their text resembles a product code.
 endings=[t['y']-slope*t['x'] for t in body if re.match(r'^(?:添加明细|通过模板添加|合计数量|合计金额|总计)',re.sub(r'\s','',t['text']))]
 if endings:body=[t for t in body if t['y']-slope*t['x']<min(endings)-height*.3]
 lines=[]
 for t in sorted(body,key=lambda t:t['y']-slope*t['x']):
  y=t['y']-slope*t['x']
  if lines and abs(y-lines[-1][0])<max(height*.65,t['h']*.55):lines[-1][1].append(t)
  else:lines.append([y,[t]])
 output=[]
 for _,line in lines:
  fields={}; used=[]
  for t in line:
   if t['x']<first-height*5 or t['x']>last+height*5:continue
   index=min(range(len(columns)),key=lambda i:abs(t['x']-columns[i][0]));key=columns[index][1]
   # Long product strings can cross the name/spec boundary: anchor bracketed IDs
   # by their left edge so the row survives and pixel-based cell splitting can run.
   if re.match(r'^\s*(?:\d+\s*)?[\[【(（][A-Za-z]{1,6}\d{3,}',t['text']):
    name_cols=[c for c in columns if c[1]=='name'];spec_cols=[c for c in columns if c[1]=='spec']
    if name_cols and spec_cols and geometry(t)[0]<spec_cols[0][2]:key='name'
   if key=='ignore':continue
   fields.setdefault(key,[]).append(t);used.append(t)
  values={k:' '.join(t['text'] for t in sorted(v,key=lambda t:t['x'])) for k,v in fields.items()}
  for numeric_key in ['quantity','price']:
   candidates=[t for t in fields.get(numeric_key,[]) if number(t['text']) is not None]
   values[numeric_key]=candidates[0]['text'] if len(candidates)==1 else ''
  # Read a numeric amount token separately from adjacent unit/manufacturer text.
  amount_tokens=[t['text'] for t in fields.get('amount',[]) if re.fullmatch(r'[\d,.]+\s*'+UNITS+'?',t['text'])]
  if len(amount_tokens)==1:values['amount']=amount_tokens[0]
  am=re.fullmatch(r'([\d,.]+)\s*('+UNITS+')',values.get('amount',''))
  if am:values['amount']=am.group(1);values['unit']=am.group(2)
  separate_units=[t['text'] for k in ('amount','unit') for t in fields.get(k,[]) if re.fullmatch(UNITS,t['text'])]
  if len(separate_units)==1:values['unit']=separate_units[0]
  if not re.fullmatch(UNITS,values.get('unit','')):values['unit']=''
  name=values.get('name','').strip();qty=number(values.get('quantity',''))
  if not name or any(x in name for x in ['添加明细','通过模板','合计','报价人','注意事项','总计']):continue
  # A separate row ordinal can be merged with the bracketed product code by OCR.
  name=re.sub(r'^\d+\s*(?=[\[【(（])|^\d+\s+(?=[A-Za-z]{1,6}\d{3,})','',name)
  code=values.get('code','');m=re.match(r'[\[【(（]?\s*([A-Za-z]{1,6}\d{3,})\s*[\]】)）]?\s*(.*)',name)
  if m:code=m.group(1);name=m.group(2).strip()
  if not name:continue
  price=number(values.get('price',''));amount=number(values.get('amount',''))
  # A name/code-looking string alone is not enough evidence for a table row.
  # Keep genuine incomplete items with specifications or other item fields.
  if qty is None and price is None and amount is None and not any(values.get(k) for k in ('spec','unit','brand')):continue
  issues=[]
  if qty is None:issues.append('数量未识别')
  if not values.get('unit'):issues.append('单位未识别')
  if not values.get('spec'):issues.append('规格未识别')
  if min((t['confidence'] for t in used),default=0)<.88:issues.append('部分文字清晰度较低')
  if '库位' in values.get('manufacturer',''):
   issues.append('厂家与库位文字连在一起，请复核')
  output.append({'name':name,'code':code,'spec':values.get('spec',''),'brand':values.get('brand',''),'quantity':qty,'unit':values.get('unit',''),'price':price,'amount':amount,'manufacturer':values.get('manufacturer',''),'notes':'；'.join(issues),'raw':' | '.join(t['text'] for t in sorted(line,key=lambda t:t['x'])),'confidence':round(min((t['confidence'] for t in used),default=0),3),'box':[min(geometry(t)[0] for t in used),min(geometry(t)[1] for t in used),max(geometry(t)[2] for t in used),max(geometry(t)[3] for t in used)]})
  output[-1]['_cells']=fields
  output[-1]['_columns']=columns
  output[-1]['_slope']=slope
  output[-1]['_height']=height
 return output,([] if output else ['已读到表头，但未可靠拆出商品行。请查看识别原文，手动补充或旋转图片重试。'])

def extract_plain_text(ts):
 text=''.join(t['text'] for t in sorted(ts,key=lambda t:(t['y'],t['x'])))
 output=[]
 matches=list(re.finditer(r'(?:购置|购买|采购)([^，。；：]{2,65}?)各\s*(\d+(?:\.\d+)?)\s*(个|件|张|本|包|卷|台|套|盒|只|瓶|支|袋|箱|米)',text))
 for match in matches:
  names=re.split(r'及|和|、',match.group(1));spec=re.search(r'建议\s*([Aa]\d)[^，。；）)]{0,10}',text[match.end():match.end()+35])
  for name in names:
   name=re.sub(r'^(?:相匹配的|配套的|相关的|相应的|对应的|与之匹配的)','',name).strip()
   if not 1<len(name)<35:continue
   output.append({'name':name,'code':'','spec':spec.group(0) if spec else '', 'brand':'','quantity':float(match.group(2)),'unit':match.group(3),'price':None,'amount':None,'manufacturer':'','notes':'按文字“各”拆分，配套关系与建议规格需人工确认','raw':match.group(0),'confidence':None,'box':None})
 if output:return output,['这是自由文本拆分结果，请核对商品边界、配套关系和建议规格。']
 return [],['该图片未检测到清晰商品表格。识别文字已保留，请对照原文手动添加商品；自由文本和手写内容需人工整理。']

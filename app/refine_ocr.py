"""Bounded recognition-only second pass. Candidates must come from image pixels."""
import re,time
from decimal import Decimal,ROUND_HALF_UP
import numpy as np
from table_parser import geometry,number,extract_rows,UNITS

def split_merged_cells(image,tokens,engine,max_regions=6):
 rows,_=extract_rows(tokens);jobs=[]
 for row in rows:
  columns=row.get('_columns',[])
  for key in ('name','spec'):
   indices=[i for i,c in enumerate(columns) if c[1]==key]
   if len(indices)!=1:continue
   index=indices[0]
   if index+1>=len(columns):continue
   next_key=columns[index+1][1]
   if next_key not in ('spec','brand'):continue
   boundary=columns[index+1][2]-3
   for token in row.get('_cells',{}).get(key,[]):
    x,y,r,b=geometry(token)
    if x<boundary-25 and r>boundary+20:
     jobs.append((token,key,next_key,(x,y,boundary,b),(boundary,y,r,b)))
 jobs=jobs[:max_regions]
 if not jobs:return tokens,[],0
 crops=[]
 for token,key,next_key,*boxes in jobs:
  for x,y,r,b in boxes:
   for pad in (1,3):crops.append(np.asarray(image.crop((max(0,x-pad),max(0,y-pad),min(image.width,r+pad),min(image.height,b+pad))))[:,:,::-1].copy())
 reads,_=engine.text_rec(crops);updated=list(tokens);changes=[]
 for i,(token,key,next_key,*boxes) in enumerate(jobs):
  pairs=[reads[i*4:i*4+2],reads[i*4+2:i*4+4]]
  if not all(len(p)==2 and p[0][0] and p[0][0]==p[1][0] and min(x[1] for x in p)>=.88 for p in pairs):continue
  if key=='name':
   code=re.search(r'[A-Za-z]{1,6}\d{3,}',token['text'])
   if code and code.group() not in pairs[0][0][0]:continue
  updated=[t for t in updated if not(t['text']==token['text'] and geometry(t)==geometry(token))]
  for pair,(x,y,r,b) in zip(pairs,boxes):
   updated.append({'text':pair[0][0],'confidence':round(float(pair[0][1]),3),'box':[[x,y],[r,y],[r,b],[x,b]]})
  changes.append({'field':key+'/'+next_key,'before':token['text'],'after':' | '.join(p[0][0] for p in pairs)})
 return updated,changes,len(jobs)*2

def inconsistent(row):
 if any(row.get(k) is None for k in ('quantity','price','amount')):return True
 q,p,a=(Decimal(str(row[k])) for k in ('quantity','price','amount'))
 return (q*p).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=a.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)

def refine(image,tokens,engine,max_regions=24):
 start=time.perf_counter()
 tokens,split_changes,split_regions=split_merged_cells(image,tokens,engine,max_regions=min(6,max_regions//4))
 rows,_=extract_rows(tokens);jobs=[];seen=set()
 for row in rows:
  cells=row.get('_cells',{});suspect=inconsistent(row)
  for key in ('quantity','price','amount','name','code','spec'):
   for token in cells.get(key,[]):
    text=token['text'];is_numeric=bool(re.fullmatch(r'[￥¥\d.,\s]+(?:个|件|张|本|包|卷|台|套|盒|只|瓶|支|袋|箱|米)?',text))
    if key in ('quantity','price','amount') and not suspect:continue
    if key in ('name','code') and not (token['confidence']<.94 and len(text)>2):continue
    if key=='spec' and not (re.search(r'\d',text) and token['confidence']<.99):continue
    box=geometry(token);identity=(text,tuple(box))
    if identity not in seen:jobs.append((token,box,key,False));seen.add(identity)
  # Recover omitted name/spec from known column positions in this same image.
  columns=row.get('_columns',[])
  for key in ('name','spec','quantity','price','amount','unit'):
   if row.get(key) and not (key=='name' and re.fullmatch(r'\d+',row[key])):continue
   if key in ('quantity','price','amount') and cells.get(key):continue
   indices=[i for i,c in enumerate(columns) if c[1]==key]
   if len(indices)!=1:continue
   idx=indices[0];left=columns[idx][2]-5
   starts=[geometry(t)[0] for other in rows for t in other.get('_cells',{}).get(key,[]) if len(t['text'])>2]
   if starts:left=min(starts)-3
   right=columns[idx+1][2]-5 if idx+1<len(columns) else row['box'][2]
   if key in ('quantity','price','amount'):
    left=columns[idx-1][3]+5 if idx else left
    right=(columns[idx][3]+columns[idx+1][2])/2 if idx+1<len(columns) else columns[idx][3]+row['_height']
   if right-left<20:continue
   x1,y1,x2,y2=row['box'];slope=row['_slope'];height=row['_height']
   numeric=[t for k in ('quantity','price','amount') for t in cells.get(k,[]) if number(t['text']) is not None]
   if not numeric:continue
   baseline=np.median([(t['y']-slope*t['x']) for t in numeric]);mid=(left+right)/2
   cy=baseline+slope*mid
   box=(left,cy-height*.65,right,cy+height*.65)
   token={'text':'','confidence':0,'box':[[left,box[1]],[right,box[1]],[right,box[3]],[left,box[3]]]}
   jobs.append((token,box,key,True))
 jobs=jobs[:max(0,max_regions-split_regions)]
 if not jobs:return tokens,{'regions':split_regions,'seconds':round(time.perf_counter()-start,3),'changes':split_changes}
 crops=[]
 for token,(x,y,r,b),key,synthetic in jobs:
  for pad in (2,5):
   crops.append(np.asarray(image.crop((max(0,x-pad),max(0,y-pad),min(image.width,r+pad),min(image.height,b+pad))))[:,:,::-1].copy())
 results,_=engine.text_rec(crops)
 # A third tight crop is limited to missing fields whose first two reads disagree.
 tie_jobs=[i for i,job in enumerate(jobs) if job[3] and results[i*2][0]!=results[i*2+1][0]]
 ties={}
 if tie_jobs:
  tie_crops=[]
  for i in tie_jobs:
   x,y,r,b=jobs[i][1]
   tie_crops.append(np.asarray(image.crop((max(0,x),max(0,y),min(image.width,r),min(image.height,b))))[:,:,::-1].copy())
  tie_results,_=engine.text_rec(tie_crops);ties=dict(zip(tie_jobs,tie_results))
 updated=[dict(t) for t in tokens];changes=list(split_changes)
 for i,(token,box,key,synthetic) in enumerate(jobs):
  pairs=results[i*2:i*2+2];old=token['text'];chosen=None
  if len(pairs)==2 and pairs[0][0]==pairs[1][0] and min(p[1] for p in pairs)>=.88:chosen=pairs[0]
  if i in ties and ties[i][1]>=.88:
   agreement=[p for p in pairs if p[0]==ties[i][0] and p[1]>=.88]
   if agreement:chosen=agreement[0]
  if key in ('quantity','price','amount'):
   # Only restore an explicitly recognized decimal to the same digit sequence.
   decimal_candidates=[p for p in pairs if p[1]>=.85 and '.' in p[0] and re.sub(r'[.￥¥\s]','',p[0])==re.sub(r'[.￥¥\s]','',old)]
   if '.' not in old and len({p[0] for p in decimal_candidates})==1:chosen=max(decimal_candidates,key=lambda p:p[1])
   if '.' in old and chosen and '.' not in chosen[0]:chosen=None
   if chosen and not re.fullmatch(r'[￥¥]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*'+UNITS+'?',chosen[0]):chosen=None
  if key=='unit' and chosen and not re.fullmatch(UNITS,chosen[0]):chosen=None
  if chosen and chosen[0] and chosen[0]!=old:
   if synthetic and (key=='name' and not re.search(r'[\u4e00-\u9fffA-Za-z]',chosen[0])):continue
   replacement={**token,'text':chosen[0],'confidence':round(float(chosen[1]),3)}
   if synthetic:updated.append(replacement)
   else:
    for j,t in enumerate(updated):
     if t['text']==old and geometry(t)==box:updated[j]=replacement;break
   changes.append({'field':key,'before':old,'after':chosen[0]})
 return updated,{'regions':len(jobs)+split_regions,'seconds':round(time.perf_counter()-start,3),'changes':changes}

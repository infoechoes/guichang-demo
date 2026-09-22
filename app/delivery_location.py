"""Extract image-level delivery remarks without guessing from department names."""
import re, statistics
from table_parser import geometry, classify

SECTIONS = re.compile(r'^(?:需求明细|审批进度|其他信息|添加明细|通过模板|合计|总计)')
LABELS = {'申请人','申请部门','需求部门','所属部门','公司','紧急','联系人','联系电话','制单人','处理人','处理日期','日期','单据号'}

def compact(text):
 return re.sub(r'\s+', '', text).strip(':：')

def extract_delivery(tokens):
 ts=[dict(t,left=geometry(t)[0],top=geometry(t)[1],right=geometry(t)[2],bottom=geometry(t)[3],y=(geometry(t)[1]+geometry(t)[3])/2,h=max(1,geometry(t)[3]-geometry(t)[1])) for t in tokens]
 # A remarks column inside the item table is not a document-level destination.
 headers=[]
 for t in ts:
  if classify(t['text'])=='name':
   kinds={classify(s['text']) for s in ts if abs(s['y']-t['y'])<max(s['h'],t['h'])*2.5}
   if len(kinds-{None})>=3:headers.append(t['top'])
 table_top=min(headers,default=float('inf'))
 anchors=[]
 for t in ts:
  match=re.match(r'^备\s*注\s*[:：]?\s*(.*)$',t['text'].strip())
  if match and t['top']<table_top:anchors.append((t,match.group(1).strip()))
 if not anchors:return {'value':'','raw':'','status':'not_found','confidence':None,'box':None}
 found=[]
 for anchor,inline in anchors:
  h=anchor['h']
  slopes=[(t['box'][1][1]-t['box'][0][1])/(t['box'][1][0]-t['box'][0][0]) for t in ts if abs(t['y']-anchor['y'])<h*6 and t['right']-t['left']>t['h']*3 and t['box'][1][0]-t['box'][0][0]>0]
  slope=statistics.median(slopes) if slopes else 0
  slope=max(-.15,min(.15,slope))
  anchor_x=(anchor['left']+anchor['right'])/2
  def y(t):return t['y']-slope*((t['left']+t['right'])/2-anchor_x)
  stops=[y(t)-t['h']/2 for t in ts if y(t)>anchor['y']+h*.65 and (SECTIONS.match(compact(t['text'])) or compact(t['text']) in LABELS or re.match(r'^备\s*注',t['text']))]
  stop=min([table_top,anchor['bottom']+h*5]+stops)
  # Do not cross another labelled field to the right of remarks on the same line.
  right=min((t['left'] for t in ts if t['left']>anchor['right'] and abs(y(t)-anchor['y'])<h*.6 and compact(t['text']) in LABELS),default=float('inf'))
  candidates=[t for t in ts if t is not anchor and t['left']>=(anchor['left'] if inline else anchor['right']-h*.2) and t['left']<right and y(t)-t['h']/2<stop and y(t)>=anchor['y']-h*.6 and not SECTIONS.match(compact(t['text'])) and compact(t['text']) not in LABELS]
  first=[t for t in candidates if abs(y(t)-anchor['y'])<max(h,t['h'])*.8]
  chosen=list(first)
  # Wrapped remarks remain within the value column and adjacent text lines.
  if first or inline:
   value_left=min((t['left'] for t in first),default=anchor['left'] if inline else anchor['right'])
   last_y=max((y(t) for t in first),default=anchor['y'])
   for t in sorted(candidates,key=lambda t:(y(t),t['left'])):
    if t in first:continue
    if t['left']>=value_left-h and y(t)>last_y+h*.55 and y(t)-last_y<=h*1.8:
     chosen.append(t);last_y=y(t)
  elif candidates:
   # Some forms place the value directly below the label.
   near=min(candidates,key=y)
   if y(near)-anchor['y']<=h*1.8:
    chosen=[t for t in candidates if abs(y(t)-y(near))<h*.6]
  value=' '.join(([inline] if inline else [])+[t['text'].strip() for t in sorted(chosen,key=lambda t:(round((y(t)-anchor['y'])/h),t['left']))]).strip()
  evidence=[anchor]+chosen
  confidence=min(float(t.get('confidence',0)) for t in evidence)
  found.append({'value':value,'raw':value,'status':'recognized' if value else 'empty','confidence':round(confidence,3),'box':[min(t['left'] for t in evidence),min(t['top'] for t in evidence),max(t['right'] for t in evidence),max(t['bottom'] for t in evidence)]})
 nonempty=[v for v in found if v['value']]
 if len({v['value'] for v in nonempty})>1:
  return {'value':'','raw':'\n'.join(v['raw'] for v in nonempty),'status':'ambiguous','confidence':None,'box':None}
 return nonempty[0] if nonempty else found[0]

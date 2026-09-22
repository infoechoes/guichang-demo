"""Local-only OCR service. Uploaded images remain in memory and are not retained."""
import base64, io, json, sys, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
APP_VERSION=json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
sys.path.insert(0,str(ROOT/'python-deps'))
from PIL import Image, ImageOps
import numpy as np
from rapidocr_onnxruntime import RapidOCR
from table_parser import extract_rows
from refine_ocr import refine
from image_orientation import detect_orientation
from delivery_location import extract_delivery
import workflow_service
from urllib.parse import urlsplit
import re

ENGINE=RapidOCR(det_limit_side_len=2000,rec_batch_num=6,intra_op_num_threads=4,inter_op_num_threads=2)
LOCK=threading.Lock()
def read_tokens(image):
 result,_=ENGINE(np.asarray(image))
 return [{'text':text,'confidence':round(float(confidence),3),'box':[[round(float(x),1),round(float(y),1)] for x,y in box]} for box,text,confidence in result or []]

def recognize(data, rotation=0, enhanced=True, auto_rotate=True):
 image=ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
 if image.width*image.height>40_000_000: raise ValueError('图片超过4000万像素，请缩小后重试。')
 if rotation: image=image.rotate(-rotation,expand=True)
 image.thumbnail((2600,2600))
 started=time.time()
 with LOCK:
  tokens=read_tokens(image)
  orientation={'rotation':0,'confident':False,'crops':0,'seconds':0,'reason':'manual'}
  if auto_rotate:
   try:
    orientation=detect_orientation(image,tokens,ENGINE)
    if orientation['rotation']:
     corrected=image.rotate(-orientation['rotation'],expand=True)
     corrected_tokens=read_tokens(corrected)
     image,tokens=corrected,corrected_tokens
   except Exception as e:
    print('Orientation fallback:',type(e).__name__,str(e),file=sys.stderr,flush=True)
    orientation.update(rotation=0,confident=False,reason='failed')
  original_tokens=tokens
  refinement={'regions':0,'seconds':0,'changes':[]}
  if enhanced:
   try:tokens,refinement=refine(image,tokens,ENGINE)
   except Exception as e:
    print('Refinement fallback:',type(e).__name__,str(e),file=sys.stderr,flush=True)
    refinement['failed']=True
 rows, warnings=extract_rows(tokens)
 delivery=extract_delivery(tokens)
 for row in rows:
  row['deliveryLocation']=delivery['value']
  for key in list(row):
   if key.startswith('_'):del row[key]
 if refinement.get('failed'):warnings.append('局部复识别未完成，已保留初次识别结果，请核对。')
 elif refinement['regions']:warnings.append(f"已局部复识别 {refinement['regions']} 处，更新 {len(refinement['changes'])} 处文字（增加约 {refinement['seconds']:.1f} 秒）；仍请对照原图核对。")
 if orientation['rotation']:
  direction='逆时针90°' if orientation['rotation']==270 else f"顺时针{orientation['rotation']}°"
  warnings.insert(0,f'已自动旋转{direction}，按转正后的图片识别。')
 elif orientation['reason'] in ('uncertain','mixed_directions','failed'):
  warnings.insert(0,'未能可靠判断图片方向，已保留当前方向；若文字侧转，请手动旋转后重试。')
 return {'rows':rows,'delivery':delivery,'deliveryLocation':delivery['value'],'tokens':tokens,'original_tokens':original_tokens,'refinement':refinement,'orientation':orientation,'rotation':(rotation+orientation['rotation'])%360,'warnings':warnings,'width':image.width,'height':image.height,'seconds':round(time.time()-started,1),'engine':'RapidOCR（本机·表格分列·局部复识别）','pipelineVersion':'local-20260909-v2'}

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def allowed(self):
  hosts={f'{host}:{port}' for host in ('127.0.0.1','localhost') for port in (self.server.server_port,4186)}
  return self.headers.get('Host') in hosts and self.headers.get('Origin') in (None,*('http://'+host for host in hosts))
 def send_json(self,status,data):
  raw=json.dumps(data,ensure_ascii=False).encode('utf-8');self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  if not self.allowed(): self.send_json(403,{'error':'仅限本机同源访问'});return
  path=urlsplit(self.path).path
  if path.startswith(('/api/workflow/','/api/yonyou/')):
   try:
    match=re.fullmatch(r'/api/workflow/batches/([^/]+)/files/([^/]+)',path)
    if match:
     raw=workflow_service.STORE.file(match[1],match[2]);self.send_response(200)
     self.send_header('Content-Type','application/vnd.ms-excel' if match[2].endswith('.xls') else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
     self.send_header('Content-Disposition',f'attachment; filename="{match[2]}"')
     self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw);return
    self.send_json(200,workflow_service.get(path))
   except ValueError as e:self.send_json(400,{'error':str(e)[:300]})
   except Exception:self.send_json(503,{'error':'本机用友流程暂不可用，已保存文件不会丢失，请稍后继续'})
   return
  if self.path=='/api/health': self.send_json(200,{'ready':True,'engine':'RapidOCR','local':True,'version':APP_VERSION})
  else:self.send_json(404,{'error':'Not found'})
 def do_POST(self):
  if not self.allowed(): self.send_json(403,{'error':'仅限本机同源访问'});return
  path=urlsplit(self.path).path
  if path.startswith(('/api/workflow/','/api/yonyou/')):
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<16_000_000:raise ValueError('请求为空或超过文件大小限制')
    if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('需要JSON请求')
    self.send_json(200,workflow_service.post(path,json.loads(self.rfile.read(size))))
   except ValueError as e:self.send_json(400,{'error':str(e)[:300]})
   except Exception as error:
    if path=='/api/workflow/drafts/retire':
     print('draft_retirement_failed',type(error).__name__,getattr(error,'errno',None),file=sys.stderr,flush=True)
     self.send_json(503,{'error':'本机草稿清理未完成，请检查服务日志后重新打开清理窗口。输入草稿仍保留；此操作不调用用友保存。'})
    else:self.send_json(503,{'error':'处理未完成；文件保存失败时不会进入用友，保存结果不明时请核查订单，不要重试'})
   return
  if self.path!='/api/recognize': self.send_json(404,{'error':'Not found'});return
  try:
   size=int(self.headers.get('Content-Length','0'))
   if not 0<size<22_000_000:raise ValueError('单张图片最大15MB。')
   req=json.loads(self.rfile.read(size));raw=base64.b64decode(req['image'],validate=True)
   if len(raw)>15_000_000:raise ValueError('单张图片最大15MB。')
   rotation=int(req.get('rotation',0))
   if rotation not in (0,90,180,270):raise ValueError('旋转角度不合法。')
   auto_rotate=req.get('autoRotate',True)
   if not isinstance(auto_rotate,bool):raise ValueError('自动转正参数不合法。')
   self.send_json(200,recognize(raw,rotation,auto_rotate=auto_rotate))
  except (ValueError,KeyError,TypeError) as e:self.send_json(400,{'error':str(e)})
  except Exception as e:
   print(type(e).__name__,str(e),file=sys.stderr,flush=True)
   self.send_json(500,{'error':'图片识别失败，请确认图片可正常打开或换一张更清晰的图片。'})

if __name__=='__main__':
 print('Local OCR ready on 127.0.0.1:4187',flush=True)
 ThreadingHTTPServer(('127.0.0.1',4187),Handler).serve_forever()

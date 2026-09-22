from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import sqlite3,json,os,argparse,threading,base64,hashlib,uuid,csv,io,mimetypes
from .domain import ROOT,seed,summary,validate,transition,amount,now
from .attachments import extract,search
DATA=Path(os.environ.get('DEMO_DATA_DIR',ROOT/'data'))
DB=DATA/'demo.sqlite';LOCK=threading.RLock()
def conn():
 c=sqlite3.connect(DB);c.execute('pragma journal_mode=WAL');return c
def init(source=None):
 with conn() as c:
  c.execute('create table if not exists orders (id text primary key, body text not null)')
  c.execute('create table if not exists attachments (id text primary key, body text not null)')
  if not c.execute('select 1 from orders').fetchone():
   o=seed(source);c.execute('insert into orders values (?,?)',(o['id'],json.dumps(o,ensure_ascii=False)))
def order():
 with conn() as c:return json.loads(c.execute('select body from orders where id=?',('food40',)).fetchone()[0])
def save(o):
 with conn() as c:c.execute('update orders set body=? where id=?',(json.dumps(o,ensure_ascii=False),o['id']))
def docs():
 with conn() as c:return [json.loads(r[0]) for r in c.execute('select body from attachments order by rowid desc')]
def packed(o):return dict(order=o,summary=summary(o))
def safe_cell(v):
 s='' if v is None else str(v)
 return "'"+s if s.startswith(('=','+','-','@','\t','\r')) else s
def csvbytes(rows):
 f=io.StringIO();w=csv.writer(f)
 for r in rows:w.writerow([safe_cell(v) for v in r])
 return ('\ufeff'+f.getvalue()).encode('utf-8')
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,status,body,kind='application/json; charset=utf-8',filename=None):
  if isinstance(body,(dict,list)):body=json.dumps(body,ensure_ascii=False).encode()
  if isinstance(body,str):body=body.encode()
  self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
  self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
  if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
  self.end_headers();self.wfile.write(body)
 def do_GET(self):
  try:
   u=urlparse(self.path);p=u.path
   if p=='/api/order':return self.reply(200,packed(order()))
   if p=='/api/health':return self.reply(200,{'ok':True,'storage':'SQLite','upstream_writes':False})
   if p=='/api/attachments':return self.reply(200,[{k:v for k,v in d.items() if k!='records'} for d in docs()])
   if p=='/api/search':return self.reply(200,search(docs(),parse_qs(u.query).get('q',[''])[0]))
   if p=='/api/integration':
    f=ROOT/'evidence/sdp-readonly.json'
    return self.reply(200,json.loads(f.read_text()) if f.exists() else {'environment':'厂商测试环境 cc_thirdparty','status':'凭据已收到；鉴权及商品/订单读取未执行，待明确调试范围授权','base_url':'https://scm.sdongpo.com/cc_thirdparty','sites':[],'products':[],'orders':[],'evidence':[]})
   if p.startswith('/api/files/'):
    ident=p.split('/')[-1];d=next((d for d in docs() if d['id']==ident),None)
    if not d:return self.reply(404,{'error':'附件不存在'})
    return self.reply(200,(DATA/'files'/ident).read_bytes(),'application/octet-stream','attachment'+Path(d['filename']).suffix)
   if p in ['/api/export/statement','/api/export/purchase']:
    o=order();s=summary(o)
    if p.endswith('statement'):
     status='已本地确认' if o['statement_confirmed_at'] else ('待本地确认' if s['ready'] else '未完整草稿')
     rows=[['对账状态',status,'来源',o['source']['kind']],['订单号',o['order_no'],'客户',o['customer'],'主体',o['subject']],['商品原名','确认商品','原订购数量','单位','加工要求','仓库实收','发出量','客户实收','暂价','最终价','金额','定价依据','差异说明']]
     rows += [[r['raw_name'],r['name'],r['ordered_qty'],r['unit'],r['note'],r['warehouse_qty'],r['delivered_qty'],r['received_qty'],r['provisional_price'],r['final_price'],amount(r['received_qty'],r['final_price']),r['price_note'],r['difference_note']] for r in o['rows']]
     rows += [['已补齐行小计',s['sale_total'],'已补量价行',s['priced_rows'],'总行数',s['total_rows']],['待核项','；'.join(s['missing'])],['提示','本地Demo结果，不代表实际收付款或外部系统结算；空值不是0。']]
    else:
     rows=[['订单号',o['order_no'],'主体',o['subject']],['商品','原需求','单位','加工要求','供应商','采购单号','采购量','仓库实收','采购价','采购实收金额']]
     rows += [[r['name'],r['ordered_qty'],r['unit'],r['note'],r['supplier'],r['purchase_no'],r['purchase_qty'],r['warehouse_qty'],r['purchase_price'],amount(r['warehouse_qty'],r['purchase_price'])] for r in o['rows']]
    return self.reply(200,csvbytes(rows),'text/csv; charset=utf-8','statement.csv' if p.endswith('statement') else 'purchase.csv')
   allowed={'/':'index.html','/app.js':'app.js','/base.css':'base.css','/app.css':'app.css'}
   if p in allowed:
    f=ROOT/'static'/allowed[p];return self.reply(200,f.read_bytes(),mimetypes.guess_type(f)[0] or 'text/plain')
   return self.reply(404,{'error':'页面不存在'})
  except ValueError as e:self.reply(400,{'error':str(e)})
  except Exception:self.reply(500,{'error':'读取失败，请检查本地文件或重试'})
 def do_POST(self):
  try:
   origin=self.headers.get('Origin');host=self.headers.get('Host','')
   if origin and origin!=self.server.origin:return self.reply(403,{'error':'请从本地Demo页面操作'})
   if not self.headers.get('Content-Type','').startswith('application/json'):raise ValueError('需要JSON请求')
   length=int(self.headers.get('Content-Length',0))
   if length>20_000_000:raise ValueError('请求过大；附件最大12MB')
   data=json.loads(self.rfile.read(length))
   with LOCK:
    if self.path=='/api/order':
     o=validate(order(),data);save(o);return self.reply(200,packed(o))
    if self.path=='/api/action':
     o=order()
     if data.get('revision')!=o['revision']:raise RuntimeError('订单已更新，请刷新')

     if data.get('action')=='close_statement':
      if not o['rows']:raise ValueError('没有订单明细')
      o=transition(o,'confirm_order');o=transition(o,'confirm_purchase');o=transition(o,'confirm_delivery');o=transition(o,'confirm_statement')
     else:o=transition(o,data.get('action'))
     save(o);return self.reply(200,packed(o))
    if self.path=='/api/attachments':
     for key,label in [('payment_no','用友付款单号'),('subject','主体'),('period','附件日期范围')]:
      if not str(data.get(key,'')).strip():raise ValueError('请填写'+label)
     name=Path(str(data.get('filename',''))).name
     raw=base64.b64decode(data['content'],validate=True)
     if len(raw)>12_000_000:raise ValueError('单附件最大12MB')
     digest=hashlib.sha256(raw).hexdigest()
     if any(d['sha256']==digest and d['payment_no']==data['payment_no'].strip() for d in docs()):raise ValueError('该付款单已导入同一附件，无需重复导入')
     records,gaps=extract(name,raw);ident=uuid.uuid4().hex
     d=dict(id=ident,filename=name,sha256=digest,records=records,gaps=gaps,record_count=len(records),created_at=now(),
       payment_no=data['payment_no'].strip()[:150],subject=data['subject'].strip()[:150],period=data['period'].strip()[:150],source='人工从付款单下载并导入，归属待财务核对')
     (DATA/'files').mkdir(exist_ok=True);(DATA/'files'/ident).write_bytes(raw)
     with conn() as c:c.execute('insert into attachments values (?,?)',(ident,json.dumps(d,ensure_ascii=False)))
     return self.reply(201,{k:v for k,v in d.items() if k!='records'})
    return self.reply(404,{'error':'接口不存在'})
  except RuntimeError as e:self.reply(409,{'error':str(e)})
  except (ValueError,KeyError,TypeError) as e:self.reply(400,{'error':str(e)[:400]})
  except Exception:self.reply(500,{'error':'保存失败：文件无法解析或本地存储异常，未标记成功'})
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=4198);args=parser.parse_args();init()
 print(f'蔬东坡衔接Demo http://127.0.0.1:{args.port} / SQLite persisted / upstream writes disabled',flush=True)
 ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()

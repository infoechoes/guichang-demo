"""Synthetic, isolated fixtures only; no production users or customer files."""
import base64,io,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import openpyxl
from price_batch_bridge import PriceBatchBridge
class PriceEntryTest(unittest.TestCase):
 def test_entry_flow_and_existing_accounts_scope(self):
  with tempfile.TemporaryDirectory() as folder:
   bridge=PriceBatchBridge(folder);owner='a'*32;other='b'*32
   rule={'name':'Test rule','purpose':'purchase','basis':'low_price','adjustment_kind':'rate','adjustment':'0.1','fee':'0','fee_order':'before','rounding':'ROUND','fixed_price':'0'}
   rule=bridge.dispatch(owner,'projects',{'project':rule})
   book=openpyxl.Workbook();sheet=book.active;sheet.append(['品名','单位','单价']);sheet.append(['Test item','斤',None]);sheet.append(['Test item','斤',7]);raw=io.BytesIO();book.save(raw)
   upload=bridge.dispatch(owner,'upload',{'file_base64':base64.b64encode(raw.getvalue()).decode()});sid=upload['id']
   setup={'id':sid,'mapping':{'name':0,'unit':1,'quantity':-1,'spec':-1},'date':'2026-09-22','project_id':rule['id']}
   bridge.dispatch(owner,'setup',setup)
   engine=bridge.engine(owner)
   engine.lookup=lambda *_:{'items':[{'name':'Test item','spec':'test','unit':'斤','origin':'test fixture','low_price':'10','average_price':'12','high_price':'14','published_at':'2026-09-22','reference_url':'http://www.xinfadi.com.cn/priceDetail.html'}]}
   bridge.dispatch(owner,'query',{'id':sid,'row':2});up=bridge.dispatch(owner,'confirm',{'id':sid,'row':2,'index':0,'factor':1});self.assertEqual(up['price_decimal'],'11.00')
   protected=bridge.dispatch(owner,'query',{'id':sid,'row':3});self.assertEqual(protected['original_price'],7)
   printed=bridge.dispatch(owner,'print',{'id':sid,'rows':[2,3]});self.assertIn('非网站截图',printed['html']);self.assertIn('2026-09-22',printed['html'])
   exported=bridge.dispatch(owner,'export',{'id':sid});out=openpyxl.load_workbook(io.BytesIO(base64.b64decode(exported['download_base64'])));self.assertEqual(out.active['C2'].value,11);self.assertEqual(out.active['C3'].value,7)
   down=bridge.dispatch(owner,'confirm',{'id':sid,'row':2,'index':0,'factor':1,'adjustment':'-0.05'});self.assertEqual(down['price_decimal'],'9.50')
   self.assertEqual(bridge.dispatch(other,'projects',{}),{})
   with self.assertRaises(ValueError):bridge.dispatch(other,'handoff',{'id':sid})
   reopened=PriceBatchBridge(folder).dispatch(owner,'projects',{});self.assertEqual(reopened[rule['id']]['adjustment'],'0.1')
if __name__=='__main__':unittest.main()

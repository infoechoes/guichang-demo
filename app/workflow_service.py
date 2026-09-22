"""Local Excel archive and the bounded integration gateway; never handles API keys."""
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from workflow_excel import review_excel
from decimal import Decimal
from workflow_split import validate_rows, assert_orderable

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT
EXPORTS = ROOT / 'exports'
ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
HASH = re.compile(r'^[0-9a-f]{64}$')
ACTIONS = {'status', 'order-query', 'preview', 'confirm', 'save', 'invalidate', 'resolve-order'}
LOCK = threading.RLock()


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf8')

def browser_safe(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value)>9007199254740991: return str(value)
    if isinstance(value, dict): return {key:browser_safe(item) for key,item in value.items()}
    if isinstance(value, list): return [browser_safe(item) for item in value]
    return value


def checked_excel(encoded):
    if not isinstance(encoded, str) or len(encoded) > 13_400_000: raise ValueError('Excel 最大10MB')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 10_000_000: raise ValueError('Excel 最大10MB')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 2000 or sum(f.file_size for f in archive.infolist()) > 40_000_000:
                raise ValueError('Excel 解压大小超限')
            if not {'xl/workbook.xml', '[Content_Types].xml'}.issubset(archive.namelist()): raise ValueError('需要有效 XLSX 文件')
            if any('..' in f.filename.split('/') or '\\' in f.filename or f.filename.startswith('/') for f in archive.infolist()):
                raise ValueError('Excel 文件路径无效')
    except (zipfile.BadZipFile, TypeError) as error:
        raise ValueError('需要有效 XLSX 文件') from error
    return raw


class WorkflowStore:
    def __init__(self, root=EXPORTS, gateway=None):
        self.root = Path(root).resolve()
        self.gateway = gateway

    def folder(self, batch_id):
        if not isinstance(batch_id, str) or not ID.fullmatch(batch_id): raise ValueError('批次编号无效')
        target = (self.root / 'batches' / batch_id).resolve()
        if not target.is_relative_to(self.root): raise ValueError('批次路径无效')
        return target

    def get(self, batch_id):
        path = self.folder(batch_id) / 'batch.json'
        if not path.is_file(): raise ValueError('找不到已保存批次')
        return json.loads(path.read_text(encoding='utf8'))

    def persist(self, record):
        record['updatedAt'] = time.time()
        atomic_write(self.folder(record['batchId']) / 'batch.json', json_bytes(record))
        return record

    def require_active(self, record):
        if record.get('draftRetiredAt'):
            raise ValueError('此旧草稿已清理，请重新导入需求；原件和恢复副本仍保留')
        return record

    def retire_drafts(self):
        """Recoverable cleanup; retain source files and every receipt-linked family."""
        with LOCK:
            records = [self.get(path.parent.name) for path in (self.root/'batches').glob('*/batch.json')]
            protected = {r['batchId'] for r in records if r.get('receipt')}
            # Parent/child reservations must never be removed around a saved/uncertain order.
            changed = True
            while changed:
                changed = False
                for record in records:
                    family = {record['batchId'],record.get('parentBatchId')} | {child.get('batchId') for child in record.get('children',[])}
                    family.discard(None)
                    if family & protected and not family <= protected:
                        protected.update(family); changed = True
            retired = []
            candidates = [record for record in records if record['batchId'] not in protected and not record.get('draftRetiredAt')]
            # Prepare every recovery copy before changing any active manifest.
            for record in candidates:
                # Keep one original manifest for recovery, outside the active listing.
                backup = self.root/'draft-trash'/record['batchId']/'batch.json'
                try:
                    if not backup.exists(): atomic_write(backup,json_bytes(record))
                except OSError as error:
                    raise ValueError('无法写入草稿恢复副本，本次尚未清理任何批次。请检查本机服务的目录写入权限及磁盘空间。') from error
            for record in candidates:
                record.update(draft=None,draftHash=None,draftRetiredAt=time.time())
                try:
                    self.persist(record)
                except OSError as error:
                    raise ValueError(f'草稿清理中断：本次已处理 {len(retired)} 个批次，其余保留。恢复副本已保存；请检查本机服务写入权限及磁盘空间，重新打开清理窗口查看列表。未调用用友保存。') from error
                retired.append(record['batchId'])
            return {'retiredCount':len(retired),'protectedCount':len([r for r in records if r['batchId'] in protected]),'batchIds':retired}

    def save_source(self, data):
        batch_id = data.get('batchId')
        folder = self.folder(batch_id)
        raw = checked_excel(data.get('excel'))
        snapshot = data.get('snapshot')
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('rows'), list) or not 1 <= len(snapshot['rows']) <= 200:
            raise ValueError('需要1至200条来源商品')
        if len(json_bytes(snapshot)) > 2_000_000: raise ValueError('来源信息过大')
        sha = hashlib.sha256(raw).hexdigest()
        snapshot_sha = hashlib.sha256(json_bytes(snapshot)).hexdigest()
        with LOCK:
            if (folder / 'batch.json').is_file():
                previous = self.get(batch_id)
                self.require_active(previous)
                if previous['snapshotHash'] != snapshot_sha: raise ValueError('此批次已保存其他内容，请创建新批次')
                # Repeated Excel generation can differ in ZIP timestamps. Reuse the first archive.
                return previous
            atomic_write(folder / 'source.xlsx', raw)
            return self.persist({'batchId': batch_id, 'createdAt': time.time(), 'snapshot': snapshot,
                'snapshotHash': snapshot_sha, 'fileHash': sha, 'path': str(folder / 'source.xlsx'),
                'downloadUrl': f'/api/workflow/batches/{batch_id}/files/source.xlsx',
                'phase': 'excel_saved', 'source': None, 'draft': None, 'final': None, 'receipt': None})

    def register(self, batch_id, gateway):
        with LOCK:
            record = self.get(batch_id)
            self.require_active(record)
            result = gateway.register_source(Path(record['path']))
            if result.get('fileHash') != record['fileHash']: raise ValueError('用友模块读取的文件与已保存 Excel 不一致')
            if record['snapshot'].get('sourceType')=='customer_excel' and len(result.get('rows',[]))!=len(record['snapshot']['rows']):
                raise ValueError('客户明细交接行数不一致，已保留原表，请核对空行或合计行后重新整理')
            record.update(source=result, phase='matching')
            return self.persist(record)

    def save_draft(self, batch_id, draft):
        if not isinstance(draft, dict) or not isinstance(draft.get('rows'), list) or not 1 <= len(draft['rows']) <= 200:
            raise ValueError('草稿商品数量无效')
        if not isinstance(draft.get('header'), dict) or draft.get('mode') not in ('live', 'mock'):
            raise ValueError('草稿格式无效')
        if len(json_bytes(draft)) > 2_000_000: raise ValueError('草稿过大')
        with LOCK:
            record = self.get(batch_id)
            self.require_active(record)
            if record.get('receipt'): raise ValueError('已尝试保存的批次不能覆盖草稿，请先核查结果')
            validate_rows(record, draft)
            record['draft'] = draft
            record['draftHash'] = hashlib.sha256(json_bytes(draft)).hexdigest()
            return self.persist(record)

    def save_final(self, batch_id, data):
        raw = checked_excel(data.get('excel'))
        digest, input_hash = data.get('digest'), data.get('inputHash')
        if not isinstance(digest, str) or not HASH.fullmatch(digest) or not isinstance(input_hash, str) or not HASH.fullmatch(input_hash):
            raise ValueError('确认版本摘要无效')
        if not isinstance(data.get('planId'), str) or not data['planId']: raise ValueError('缺少订单预览版本')
        with LOCK:
            record = self.get(batch_id)
            self.require_active(record)
            assert_orderable(record)
            if not record.get('source') or record['source']['sourceId'] != data.get('sourceId'): raise ValueError('来源批次不一致')
            if record.get('receipt'): raise ValueError('已尝试保存，不能覆盖确认文件')
            filename = f"final-{hashlib.sha256(data['planId'].encode()).hexdigest()}.xlsx"
            path = self.folder(batch_id) / filename
            if not path.exists(): atomic_write(path, raw)
            else: raw = path.read_bytes()
            record['final'] = {'planId': data['planId'], 'digest': digest, 'inputHash': input_hash,
                'sourceId': data['sourceId'], 'path': str(path), 'fileHash': hashlib.sha256(raw).hexdigest(),
                'downloadUrl': f'/api/workflow/batches/{batch_id}/files/{filename}'}
            record['phase'] = 'final_excel_saved'
            return self.persist(record)

    def require_final(self, batch_id, data):
        record = self.get(batch_id)
        self.require_active(record)
        assert_orderable(record)
        final = record.get('final') or {}
        if any(final.get(k) != data.get(k) for k in ['sourceId', 'planId', 'digest', 'inputHash']):
            raise ValueError('请先保存与当前预览完全一致的确认 Excel')
        if not final.get('path') or hashlib.sha256(Path(final['path']).read_bytes()).hexdigest() != final.get('fileHash'):
            raise ValueError('确认 Excel 文件缺失或已被修改')
        return record

    def by_source(self, source_id):
        for path in (self.root / 'batches').glob('*/batch.json'):
            record = self.get(path.parent.name)
            if (record.get('source') or {}).get('sourceId') == source_id:
                return record
        return None

    def list_batches(self):
        paths = sorted((self.root / 'batches').glob('*/batch.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for path in paths:
            try:
                record = self.get(path.parent.name)
                if record.get('draftRetiredAt'): continue
                result.append({k: record.get(k) for k in ['batchId','createdAt','updatedAt','phase','path','receipt']})
                if len(result)>=20: break
            except (ValueError, OSError): continue
        return result

    def file(self, batch_id, filename):
        if filename=='draft.xlsx':
            from workflow_mapping import draft_excel
            return draft_excel(self.require_active(self.get(batch_id)))
        if filename not in ('source.xlsx','customer-original.xlsx','customer-original.xls') and not re.fullmatch(r'final-[0-9a-f]{64}\.xlsx', filename): raise ValueError('文件名无效')
        path = self.folder(batch_id) / filename
        if not path.is_file(): raise ValueError('找不到已保存的 Excel')
        return path.read_bytes()


STORE = WorkflowStore()
_gateway = None
_defaults = None


def default_store():
    global _defaults
    with LOCK:
        if _defaults is not None: return _defaults
        candidates = [ROOT / 'yonyou' / 'defaults_store.py', ROOT.parent / 'yonyou-import' / 'defaults_store.py']
        module_path = next((p for p in candidates if p.is_file()), None)
        if module_path is None: raise ValueError('默认信息模块尚未就绪，请稍后重试')
        sys.path.insert(0,str(module_path.parent))
        spec = importlib.util.spec_from_file_location('guichang_defaults_store', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _defaults = module.DefaultStore(DATA_ROOT / 'local-state' / 'order-defaults')
        return _defaults


def gateway():
    global _gateway
    with LOCK:
        if _gateway is not None: return _gateway
        candidates = [ROOT / 'yonyou' / 'integration_gateway.py', ROOT.parent / 'yonyou-import' / 'integration_gateway.py']
        module_path = next((p for p in candidates if p.is_file()), None)
        if module_path is None: raise ValueError('用友接入模块尚未就绪；Excel 已保存，可稍后继续')
        sys.path.insert(0, str(module_path.parent))
        spec = importlib.util.spec_from_file_location('guichang_integration_gateway', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gateway = module.Gateway(source_roots=[EXPORTS], state_dir=DATA_ROOT / 'local-state' / 'yonyou-gateway', base_url='http://127.0.0.1:4193')
        return _gateway


def post(path, data):
    if not isinstance(data, dict): raise ValueError('请求格式无效')
    match = re.fullmatch(r'/api/workflow/invoices/(status|sample|start-preview|job|prepare-save|save|check|history|draft-read|draft-save)', path)
    if match: return browser_safe(gateway().invoice(match[1], data))
    if path == '/api/workflow/drafts/retire':
        if data.get('confirmation') != '清理本机未完成草稿，保留原件与回执': raise ValueError('需要明确确认清理草稿')
        return STORE.retire_drafts()
    if path in ('/api/workflow/customer-excel/inspect','/api/workflow/customer-excel/archive'):
        import workflow_customer_excel
        if path.endswith('/inspect'): return workflow_customer_excel.inspect(data,sys.modules[__name__])[1]
        return workflow_customer_excel.archive(data,sys.modules[__name__])
    pricing_match=re.fullmatch(r'/api/workflow/order-pricing/(projects|start|query|confirm|apply)',path)
    if pricing_match:
        from order_pricing import OrderPricing
        with LOCK:
            if not hasattr(STORE,'order_pricing'):STORE.order_pricing=OrderPricing(STORE)
            return STORE.order_pricing.dispatch(pricing_match[1],data)
    if path in ('/api/workflow/mappings/lookup','/api/workflow/mappings/apply'):
        import workflow_mapping
        with LOCK:return workflow_mapping.apply(STORE,data) if path.endswith('/apply') else workflow_mapping.lookup(STORE,data)
    if path == '/api/workflow/archives/search': return browser_safe(gateway().dispatch('archives',data))
    if path == '/api/workflow/stock/search': return browser_safe(gateway().dispatch('stock',data))
    if path == '/api/workflow/defaults/preview':
        encoded = data.get('file')
        if not isinstance(encoded,str) or len(encoded)>1_340_000: raise ValueError('默认信息文件最大1MB')
        raw = base64.b64decode(encoded,validate=True)
        if len(raw)>1_000_000: raise ValueError('默认信息文件最大1MB')
        return default_store().preview(raw,data.get('filename'))
    if path == '/api/workflow/defaults/save':
        return default_store().save(data.get('name'),data.get('header'))
    if path == '/api/workflow/batches': return STORE.save_source(data)
    match = re.fullmatch(r'/api/workflow/batches/([^/]+)/(handoff|draft|final|split-ready)', path)
    if match:
        batch_id, action = match.groups()
        STORE.require_active(STORE.get(batch_id))
        if action == 'handoff': return STORE.register(batch_id, gateway())
        if action == 'draft': return STORE.save_draft(batch_id, data)
        if action == 'split-ready':
            import workflow_split
            return workflow_split.split(STORE, batch_id, data.get('draftHash'), sys.modules[__name__])
        review = gateway().get_review(data.get('planId'))
        if any(review.get(k) != data.get(k) for k in ('sourceId','digest','inputHash')): raise ValueError('预览版本不一致')
        record = STORE.get(batch_id)
        encoded = base64.b64encode(review_excel(review, record)).decode('ascii')
        record = STORE.save_final(batch_id, {**data,'excel':encoded})
        from workflow_mapping import confirmed_mapping
        record['confirmedMapping']=confirmed_mapping(review,record)
        STORE.persist(record)
        gateway().bind_final_excel(data['planId'], record['final']['path'])
        return record
    match = re.fullmatch(r'/api/yonyou/([a-z-]+)', path)
    if match and match[1] in ('products', 'price'):
        raise ValueError('当前商品流程仅使用本机知识库；系统取价待补齐接口档案关联')
    if not match or match[1] not in ACTIONS: raise ValueError('未开放此操作')
    action = match[1]
    if action in ('preview', 'confirm', 'save'):
        with LOCK:
            owner = STORE.by_source(data.get('sourceId'))
            if owner:
                STORE.require_active(owner)
                assert_orderable(owner)
                if action in ('confirm', 'save') and owner['batchId'] != data.get('batchId'):
                    raise ValueError('来源与批次不一致')
                if owner.get('parentBatchId') and action in ('preview', 'save'):
                    validate_rows(owner, data)
    if action in ('confirm', 'save'):
        with LOCK:
            record = STORE.require_final(data.get('batchId'), data)
            if record.get('receipt'): raise ValueError('此批次已有保存尝试，请核查回执，不要重复发送')
            if action == 'save':
                record.update(phase='dispatch_started', receipt={'state':'dispatch_started','message':'已开始保存处理，等待结果；禁止重复发送'})
                STORE.persist(record)
    try:
        result = gateway().dispatch(action, {k:v for k,v in data.items() if k != 'batchId'})
    except Exception as error:
        if action == 'save':
            with LOCK:
                record = STORE.get(data['batchId'])
                if isinstance(error, getattr(_gateway,'PreflightError',())):
                    record.update(phase='matching', receipt=None)
                else:
                    record.update(phase='uncertain', receipt={'state':'uncertain','message':'保存结果未确认，请核查用友订单，勿重试'})
                STORE.persist(record)
        raise
    if action == 'save':
        with LOCK:
            record = STORE.get(data['batchId'])
            record.update(receipt=result, phase=result.get('state','uncertain'))
            STORE.persist(record)
    return browser_safe(result)


def get(path):
    if path == '/api/workflow/knowledge-bases': return default_store().knowledge_bases()
    if path == '/api/workflow/defaults': return default_store().list()
    if path == '/api/yonyou/status': return gateway().dispatch('status', {})
    if path == '/api/workflow/batches': return {'batches':STORE.list_batches()}
    match = re.fullmatch(r'/api/workflow/batches/([^/]+)', path)
    if match: return STORE.require_active(STORE.get(match[1]))
    raise ValueError('未开放此操作')

"""Durable, disjoint child batches built only from a saved parent draft."""
import copy
import hashlib
import re
import time
import uuid
from decimal import Decimal
from workflow_excel import tables_excel


def number(value, precision=8, zero=False, maximum='1000000000'):
    value = str(value if value is not None else '').strip()
    if not re.fullmatch(r'\d+(\.\d+)?', value) or len(value.partition('.')[2]) > precision:
        return False
    parsed = Decimal(value)
    return (parsed >= 0 if zero else parsed > 0) and parsed <= Decimal(maximum)


def ready(row):
    return (bool(row.get('productId') or (row.get('catalogSource') or {}).get('recordKey'))
            and all(str(row.get(k) or '').strip() for k in ('name', 'unit'))
            and number(row.get('quantity')) and number(row.get('price'), precision=6)
            and number(row.get('taxRate'), zero=True, maximum='1'))


def validate_rows(record, draft):
    expected = [str(row.get('id') or i + 1) for i, row in enumerate(record['snapshot']['rows'])]
    actual = [row.get('sourceLineId') for row in draft['rows']]
    if len(set(expected)) != len(expected) or actual != expected:
        raise ValueError('草稿来源行必须与原批次逐行对应，不能添加、删除或更换来源编号')
    frozen = record.get('splitReservations') or {}
    previous = {row['sourceLineId']: row for row in (record.get('draft') or {}).get('rows', [])}
    if any(row != previous.get(row['sourceLineId']) for row in draft['rows'] if row['sourceLineId'] in frozen):
        raise ValueError('已拆分的商品明细已锁定，请打开对应子批次修改')


def assert_orderable(record):
    if record.get('splitReservations'):
        raise ValueError('此母批次已拆分，请在独立子批次中预览和保存，避免重复录入')


def split(store, batch_id, draft_hash, service):
    with service.LOCK:
        parent = store.get(batch_id)
        store.require_active(parent)
        if parent.get('parentBatchId'):
            raise ValueError('子批次不能继续拆分')
        if parent.get('receipt'):
            raise ValueError('此批次已尝试保存，不能拆分，请先核查结果')
        draft = parent.get('draft')
        if not draft or not draft_hash or draft_hash != parent.get('draftHash'):
            raise ValueError('草稿版本已变化，请保存当前草稿后重新拆分')
        validate_rows(parent, draft)
        # Persist the reservation before any independently usable child exists.
        # An interrupted write retries this same plan; rows are never released automatically.
        pending = next((child for child in parent.get('children', []) if child['state'] == 'reserved'), None)
        if not pending:
            allocated = parent.get('splitReservations') or {}
            rows = [copy.deepcopy(row) for row in draft['rows'] if row['sourceLineId'] not in allocated and ready(row)]
            if not rows:
                raise ValueError('没有尚未拆分且业务信息齐全的商品')
            child_id = str(uuid.uuid4())
            ids = [row['sourceLineId'] for row in rows]
            originals = {str(row.get('id') or i + 1): row for i, row in enumerate(parent['snapshot']['rows'])}
            snapshot = {**copy.deepcopy(parent['snapshot']), 'parentBatchId': batch_id,
                        'rows': [{**copy.deepcopy(originals[key]), 'id': key} for key in ids]}
            pending = {'batchId': child_id, 'state': 'reserved', 'sourceLineIds': ids,
                       'createdAt': time.time(), 'snapshot': snapshot,
                       'draft': {**copy.deepcopy(draft), 'header': {**draft['header'], 'reviewed': False}, 'rows': rows}}
            parent['children'] = [*parent.get('children', []), pending]
            parent['splitReservations'] = {**allocated, **dict.fromkeys(ids, child_id)}
            parent['phase'] = 'split_parent'
            store.persist(parent)
        child_id = pending['batchId']
        if parent.get('source'):
            bridge = service.gateway()
            source_id = parent['source']['sourceId']
            # A restarted gateway has no old in-memory plans or confirmations.
            if source_id in bridge.sources:
                bridge.dispatch('invalidate', {'sourceId': source_id})
        path = store.folder(child_id) / 'batch.json'
        if path.exists():
            child = store.get(child_id)
            if child.get('parentBatchId') != batch_id:
                raise ValueError('子批次来源冲突，需维护人员核查')
        else:
            rows = pending['draft']['rows']
            table = [['商品名称', '数量', '规格', '单位', '单价', '送货科室', '来源行编号', '原工作表', '原行号']]
            table += [[r.get(k, '') for k in ('name', 'quantity', 'spec', 'unit', 'price', 'deliveryLocation', 'sourceLineId', 'sourceSheet', 'sourceRow')] for r in rows]
            raw = tables_excel([('业务录单', table), ('拆分来源', [['母批次', batch_id], ['子批次', child_id]])])
            folder = store.folder(child_id)
            service.atomic_write(folder / 'source.xlsx', raw)
            snapshot = pending['snapshot']
            child = {'batchId': child_id, 'parentBatchId': batch_id, 'createdAt': time.time(),
                     'snapshot': snapshot, 'snapshotHash': hashlib.sha256(service.json_bytes(snapshot)).hexdigest(),
                     'fileHash': hashlib.sha256(raw).hexdigest(), 'path': str(folder / 'source.xlsx'),
                     'downloadUrl': f'/api/workflow/batches/{child_id}/files/source.xlsx',
                     'phase': 'excel_saved', 'source': None, 'final': None, 'receipt': None,
                     'draft': pending['draft'], 'draftHash': hashlib.sha256(service.json_bytes(pending['draft'])).hexdigest()}
            store.persist(child)
        pending['state'] = 'created'
        # Keep the frozen plan for recovery and provenance; allocation remains on the parent.
        store.persist(parent)
    return {'parent': parent, 'child': child}

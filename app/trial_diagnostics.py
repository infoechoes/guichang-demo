"""Bounded support metadata. Never log request bodies, credentials or raw errors."""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

BUILD = '2026.09.22-public-preview'


def route_name(path):
    path = path.split('?', 1)[0]
    if not path.startswith('/api/'): return 'static'
    if re.fullmatch(r'/api/workflow/batches/[a-f0-9-]{36}(?:/[a-z-]+)?', path):
        return re.sub(r'[a-f0-9-]{36}', ':batch', path)
    allowed = {'/api/session','/api/login','/api/logout','/api/change-password','/api/health',
               '/api/recognize','/api/workspace/image-draft','/api/workspace/knowledge-draft',
               '/api/knowledge-bases/inspect','/api/knowledge-bases/publish',
               '/api/admin/users','/api/admin/audit','/api/admin/disable',
               '/api/admin/enable','/api/admin/reset-password',
               '/api/workflow/batches','/api/workflow/knowledge-bases','/api/workflow/defaults',
               '/api/workflow/customer-excel/inspect','/api/workflow/customer-excel/archive',
               '/api/workflow/defaults/preview','/api/workflow/defaults/save',
               '/api/workflow/archives/search','/api/workflow/stock/search',
               '/api/yonyou/status','/api/yonyou/resolve-order','/api/yonyou/preview',
               '/api/yonyou/confirm','/api/yonyou/save','/api/yonyou/invalidate'}
    return path if path in allowed else 'other'


class RequestLog:
    def __init__(self, root):
        self.folder=Path(root)/'logs'
        self.lock=threading.Lock()

    def write(self, request_id, method, path, status, elapsed, error_kind=''):
        record={'at':datetime.now(timezone.utc).isoformat(),'build':BUILD,
                'requestId':request_id,'method':method if method in ('GET','POST') else 'OTHER',
                'route':route_name(path),'status':int(status),'durationMs':max(0,int(elapsed*1000)),
                'category':error_kind if error_kind in ('validation','authorization','internal','rate_limited') else ''}
        # Logging failure must never turn an acknowledged save into a failure.
        try:
            with self.lock:
                self.folder.mkdir(parents=True,exist_ok=True)
                target=self.folder/'requests.jsonl'
                if target.exists() and target.stat().st_size>1_000_000:
                    for number in (2,1):
                        old=self.folder/f'requests.{number}.jsonl'
                        if old.exists():old.replace(self.folder/f'requests.{number+1}.jsonl')
                    target.replace(self.folder/'requests.1.jsonl')
                with target.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n')
        except OSError:
            pass

"""Human-reviewed workflow facade over the existing credential-owning loopback service.

No credential copying, arbitrary proxy routes, automatic saves, or automatic retries.
The hosting HTTP service MUST enforce its own loopback Host/Origin and body limits.
"""
import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from core import build_plan, read_xlsx, browser_safe, wire, YonSuiteError
from archive_candidates import search as search_archives, ProductSearchCache
from inventory_query import query_inventory
from history_defaults import reuse_history, apply_default_tax, HeaderReferenceCache
from order_resolver import text
from order_header_contract import check_header_delivery, local_field_warnings

REVIEW_TEXT = '我已核对本版本，允许进入保存步骤'
SAVE_TEXT = '确认仅保存，不提交不审核'
VERSION = 'workflow-review-v1'


class PreflightError(ValueError):
    """This invocation was rejected before any upstream save was dispatched."""
    state = 'preflight_rejected'


def digest(value):
    return hashlib.sha256(wire(value).encode('utf-8')).hexdigest()


class Gateway:
    PreflightError = PreflightError

    def __init__(self, base_url='http://127.0.0.1:4193', state_dir=None,
                 source_roots=None, transport=None, clock=time.time, order_namespace=None):
        if base_url not in ('http://127.0.0.1:4193', 'http://localhost:4193'):
            raise ValueError('只允许连接既有本机4193用友服务')
        self.base_url = base_url
        self.state = Path(state_dir or Path(__file__).parent / 'workflow-state').resolve()
        self.roots = [Path(p).resolve() for p in (source_roots or [])]
        self.transport = transport or self._request
        self.clock = clock
        self.order_namespace = order_namespace
        self.lock = threading.RLock()
        self.sources, self.plans, self.confirmations = {}, {}, {}
        self.header_references=HeaderReferenceCache(clock=self.clock)
        self.product_search_cache=ProductSearchCache(clock=self.clock)

    def _request(self, path, data=None):
        diagnostic_read = data is None and bool(re.fullmatch(r'/api/diagnostic\?digest=[a-f0-9]{64}',path))
        invoice_route = path in ('/api/invoice/status', '/api/invoice/start-preview', '/api/invoice/job', '/api/invoice/prepare-save', '/api/invoice/save', '/api/invoice/check', '/api/invoice/history')
        if not diagnostic_read and not invoice_route and path not in ('/api/status', '/api/products', '/api/price', '/api/stock', '/api/order-query', '/api/preview', '/api/save', '/api/resolve-order', '/api/references', '/api/submit-preview', '/api/submit'):
            raise ValueError('接口不在允许清单')
        req = urllib.request.Request(self.base_url + path,
            data=None if data is None else wire(data).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Origin': self.base_url},
            method='GET' if data is None else 'POST')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('本机服务重定向被拒绝')
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=100) as response:
                raw = response.read(10_000_001)
            if len(raw) > 10_000_000: raise ValueError('本机响应超过限制')
            result = json.loads(raw)
            if not isinstance(result, dict): raise ValueError('本机响应结构错误')
            return result
        except urllib.error.HTTPError as exc:
            # The connector wraps safe diagnostics in error JSON. Carry only
            # rate-limit identity/delay, never arbitrary messages, URLs or secrets.
            diagnostic={}
            try:
                with exc:raw=exc.read(16385)
                value=json.loads(raw) if len(raw)<=16384 else {}
                if isinstance(value,dict):
                    value=value.get('error',value)
                    if isinstance(value,str):value=json.loads(value)
                    if isinstance(value,dict):diagnostic=value
            except (ValueError,TypeError,OSError):pass
            if (exc.code==429 or diagnostic.get('category')=='rate_limited'
                    or str(diagnostic.get('httpStatus'))=='429' or str(diagnostic.get('apiCode'))=='310050'):
                delay=diagnostic.get('retryAfterSeconds')
                delay=delay if type(delay) is int and 1<=delay<=86400 else 30
                raise YonSuiteError('rate_limited','用友调用频率达到阈值，请等待后继续补齐未完成项',
                                    http_status=429,retry_after=delay,
                                    api_code='310050' if str(diagnostic.get('apiCode'))=='310050' else None) from None
            raise ValueError(f'本机用友服务拒绝请求（HTTP {exc.code}）；请检查配置或既有保存记录') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError('本机4193用友服务未连接或超时，不自动重试') from None

    def _write(self, relative, value):
        target = self.state / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + '.' + secrets.token_hex(6) + '.tmp')
        with tmp.open('x', encoding='utf-8') as f:
            f.write(wire(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        return target

    def _file(self, path):
        target = Path(path).resolve(strict=True)
        if not any(target.is_relative_to(root) for root in self.roots):
            raise ValueError('Excel必须位于已配置的本机批次输出目录')
        if not target.is_file() or target.suffix.lower() != '.xlsx':
            raise ValueError('需要已落盘的xlsx文件')
        if target.stat().st_size > 10_000_000: raise ValueError('Excel超过10MB')
        raw = target.read_bytes()
        parsed = read_xlsx(raw)
        return target, raw, parsed

    def register_source(self, path):
        target, raw, parsed = self._file(path)
        with self.lock:
            for sid, source in self.sources.items():
                if source['path'] == str(target) and source['fileHash'] == parsed['fileHash']:
                    return {**parsed, 'sourceId': sid, 'revision': source['revision']}
            sid = secrets.token_urlsafe(24)
            self.sources[sid] = {'path': str(target), 'fileHash': parsed['fileHash'],
                                 'revision': 0, 'planId': None}
            self._write(Path('sources') / (sid + '.json'), self.sources[sid])
            return {**parsed, 'sourceId': sid, 'revision': 0}

    def _source(self, sid):
        if not isinstance(sid, str) or sid not in self.sources:
            raise ValueError('Excel批次未登记或服务已重启，请从已落盘批次重新载入')
        return self.sources[sid]

    def _invalidate(self, sid):
        source = self._source(sid)
        source['revision'] += 1
        source['planId'] = None
        for key in list(self.confirmations):
            if self.confirmations[key]['sourceId'] == sid: del self.confirmations[key]
        return source

    def _active(self, data):
        sid, pid = data.get('sourceId'), data.get('planId')
        source = self._source(sid)
        plan = self.plans.get(pid) if isinstance(pid, str) else None
        if not plan or source['planId'] != pid or plan['sourceId'] != sid:
            raise ValueError('预览版本已失效，请重新检查')
        if self.clock() - plan['created'] > 1800:
            raise ValueError('预览已过期，请重新检查')
        if data.get('digest') != plan['digest'] or data.get('inputHash') != plan['inputHash']:
            raise ValueError('订单摘要不匹配，请重新预览')
        _, _, parsed = self._file(source['path'])
        if parsed['fileHash'] != source['fileHash']:
            self._invalidate(sid)
            raise ValueError('来源Excel已被修改，请重新载入批次')
        return plan

    def bind_final_excel(self, plan_id, path):
        """Called by the trusted batch service after its final workbook is persisted.

        The caller must build it from get_review(plan_id)['input'], not browser-supplied
        XLSX bytes. The canonical JSON snapshot is the exact authoritative save record.
        """
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan: raise ValueError('预览不存在')
            self._active(plan)
            target, _, parsed = self._file(path)
            # The business sheet must preserve row order and actual order quantity/price.
            if len(parsed['rows']) != len(plan['input']['rows']):
                raise ValueError('最终Excel明细行数与预览不一致')
            from decimal import Decimal, InvalidOperation
            for saved, row in zip(parsed['rows'], plan['input']['rows']):
                try:
                    matches = (saved['name'] == str(row['name'])
                        and Decimal(saved['quantity']) == Decimal(str(row['quantity']))
                        and Decimal(saved['sourcePrice']) == Decimal(str(row['price']))
                        and saved.get('unit', '') == str(row.get('unit', '')))
                except (InvalidOperation, KeyError, TypeError): matches = False
                if not matches: raise ValueError('最终Excel商品/数量/单位/成交价与预览不一致')
            evidence = {'path': str(target), 'fileHash': parsed['fileHash'], 'inputHash': plan['inputHash']}
            if plan.get('finalExcel') != evidence:
                for key in list(self.confirmations):
                    if self.confirmations[key]['planId'] == plan_id:
                        del self.confirmations[key]
            plan['finalExcel'] = evidence
            self._write(Path('reviews') / (plan_id + '.json'), plan)
            return evidence

    def get_review(self, plan_id):
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan: raise ValueError('预览不存在')
            self._active(plan)
            return copy.deepcopy(browser_safe(plan))

    def _staff_after_permission_denial(self, data, result, original_header, instance, generation, checked_history=None):
        """Reuse an exact current-name historical reference; never requery rows."""
        def denied(issue):
            if issue.get('field') != 'salespersonName': return False
            diagnostic = issue.get('diagnostic') or {}
            if isinstance(diagnostic, dict) and (str(diagnostic.get('httpStatus')) == '403' or str(diagnostic.get('apiCode')) == '310037'):
                return True
            message = str(issue.get('message') or '')
            import re
            return bool(re.search(r'"httpStatus"\s*:\s*403|"apiCode"\s*:\s*"?310037|HTTP\s+403', message))
        header = result.get('header') or {}
        issues = result.get('issues') or []
        if not header.get('salespersonName') or header.get('corpContact') or not any(denied(issue) for issue in issues):
            return result
        historical = checked_history
        if historical is None:
            try:
                # No per-order staff replacement choices: the current name must match.
                historical = reuse_history(self.transport, {**data, 'header': header, 'rows': result.get('rows') or []})
            except ValueError:
                pass
        if historical and not historical.get('issues') and historical['header'].get('corpContact'):
            patched = copy.deepcopy(result)
            patched['header']['corpContact'] = historical['header']['corpContact']
            patched['issues'] = [issue for issue in issues if not denied(issue)]
            h = patched['header']
            required = ('agentId','salesOrgId','transactionTypeId','settlementOrgId','invoiceAgentId','currencyId','natCurrencyId','exchangeRateType','stockOrgId')
            complete = all(text(h.get(key)) for key in required)
            complete = complete and (not h.get('salespersonName') or bool(h.get('corpContact'))) and (not h.get('departmentName') or bool(h.get('saleDepartmentId')))
            complete = complete and bool(patched.get('rows')) and all(
                all(text(row.get(key)) for key in ('productId','unitId','taxId')) and row.get('sameUnitConfirmed') is True
                for row in patched['rows'])
            patched['ready'] = bool(complete and not patched['issues'])
            patched['historyStaffFallback'] = historical.get('historyStaffFallback') or {
                'orderId': historical['history']['orderId'], 'orderCode': historical['history']['orderCode'],
                'salespersonName': h['salespersonName'], 'corpContact': h['corpContact'],
                'basis': '同客户、销售组织、交易类型已审核历史订单中的当前业务员姓名', 'additionalDetailsRead': 0}
            self.header_references.remember(original_header, historical, patched, instance, generation)
            return patched
        patched = copy.deepcopy(result)
        for issue in patched.get('issues') or []:
            if denied(issue):
                issue['code'] = 'current_staff_reference_unresolved'
                issue['message'] = f'销售业务员“{text(header.get("salespersonName"))}”尚未匹配：员工档案查询权限不足，历史核对也未取得唯一对应关系；请管理员补充查询权限或选择明确档案'
        return patched

    def invoice(self, action, data):
        """Workspace identity comes from the hosting service, never from the browser."""
        from invoice_workflow import InvoiceService, sha
        if not isinstance(data, dict): raise ValueError('发票请求需要JSON对象')
        scope = sha(self.order_namespace or 'local-workspace')
        draft_path = self.state / 'invoice-draft.json'
        if action == 'draft-read':
            with self.lock:
                return {'draft': json.loads(draft_path.read_text(encoding='utf-8')) if draft_path.exists() else None}
        if action == 'draft-save':
            value = data.get('draft')
            if (not isinstance(value, dict) or set(value)-{'mode','header','rows','sourceName'}
                    or value.get('mode') not in ('mock','live') or not isinstance(value.get('header'),dict)
                    or not isinstance(value.get('rows'),list) or len(value['rows'])>200
                    or len(wire(value))>1_000_000): raise ValueError('发票草稿格式无效或过大')
            with self.lock:
                self._write(Path('invoice-draft.json'), value)
            return {'saved':True}
        if action not in ('status','sample','start-preview','job','prepare-save','save','check','history'):
            raise ValueError('未开放此发票操作')
        mode = data.get('mode')
        if mode not in ('mock','live'): raise ValueError('请选择模拟或真实模式')
        scoped = {**data, 'scope': scope}
        if mode == 'mock': return InvoiceService(None, self.state/'invoice-demo', mock=True).dispatch(action, scoped)
        if action == 'sample': raise ValueError('演示仅限模拟模式')
        return self.transport('/api/invoice/'+action, scoped)

    def dispatch(self, action, data=None):
        data = data or {}
        if not isinstance(data, dict): raise ValueError('请求需要JSON对象')
        if action == 'status':
            try:
                status = self.transport('/api/status')
                if status.get('appId') != 'guichang-yonyou-import-demo':
                    raise ValueError('4193不是预期用友服务')
                return {**status, 'gatewayVersion': VERSION, 'reviewRequired': True, 'connected': True}
            except ValueError as e:
                return {'gatewayVersion': VERSION, 'connected': False, 'configured': False,
                        'liveSaveEnabled': False, 'reviewRequired': True, 'message': str(e)}
        if action == 'archives':
            if data.get('kind') != 'product':
                status = self.transport('/api/status')
                if status.get('version') == 'diagnostics-v6-order-references':
                    return self.transport('/api/references', data)
            return search_archives(self.transport, data, self.product_search_cache)
        if action == 'stock':
            return query_inventory(self.transport, data, self.clock)
        if action == 'resolve-order':
            status = self.transport('/api/status')
            if status.get('version') != 'diagnostics-v6-order-references':
                raise ValueError('用友连接服务需要加载新版自动关联功能，请在本机重新连接用友后再检查')
            data = apply_default_tax(data)
            original_header=copy.deepcopy(data.get('header') or {})
            force=data.get('forceRefreshArchives',False)
            if not isinstance(force,bool):raise ValueError('强制刷新选项无效')
            h,reused,cache_generation=self.header_references.apply(original_header,status.get('serviceInstanceId'),force)
            data={**data,'header':h}
            required_refs = ('agentId','salesOrgId','transactionTypeId','settlementOrgId','invoiceAgentId','currencyId','natCurrencyId','exchangeRateType','stockOrgId')
            needs_history = any(not h.get(k) for k in required_refs)
            # Staff/department names are current order inputs; missing IDs alone
            # use the current reference APIs, never trigger a 14-day history scan.
            if not needs_history:
                result=self.transport('/api/resolve-order', data)
                result=self._staff_after_permission_denial(data,result,original_header,status.get('serviceInstanceId'),cache_generation)
                return {**result,'headerReuse':reused} if reused else result
            choices_path = Path(__file__).parent / 'local-state' / 'history-choices.json'
            choices = json.loads(choices_path.read_text(encoding='utf8')) if choices_path.exists() else {}
            historical = reuse_history(self.transport, data, choices)
            # Unsafe history scope/ambiguous stock defaults remain blocking.
            # Named-field differences are warnings; resolve those current names.
            if historical['issues']: return {k:v for k,v in historical.items() if k!='_headerReuse'}
            result = self.transport('/api/resolve-order', {**data, 'header':historical['header'], 'rows':historical['rows']})
            result = self._staff_after_permission_denial(data,result,original_header,status.get('serviceInstanceId'),cache_generation,checked_history=historical)
            self.header_references.remember(original_header,historical,result,status.get('serviceInstanceId'),cache_generation)
            return {**result, 'history':historical['history'], 'businessPatch':historical['businessPatch'],
                    'warnings': list(historical.get('warnings') or []) + list(result.get('warnings') or []),
                    'historyStaffFallback': historical.get('historyStaffFallback')}
        if action in ('products', 'price', 'order-query'):
            return self.transport('/api/' + action, data)
        if action == 'save': return self._save(data)
        with self.lock:
            if action == 'invalidate':
                s = self._invalidate(data.get('sourceId'))
                return {'state': 'review_invalidated', 'revision': s['revision']}
            if action == 'preview': return self._preview(data)
            if action == 'confirm':
                plan = self._active(data)
                if not plan['ready']: raise ValueError('缺项未完成，不能确认')
                if data.get('confirmation') != REVIEW_TEXT: raise ValueError('需要人工核对本版本')
                self._check_evidence(plan)
                cid = secrets.token_urlsafe(32)
                approval = {'confirmationId': cid, 'sourceId': plan['sourceId'], 'planId': plan['planId'],
                            'digest': plan['digest'], 'inputHash': plan['inputHash'], 'expiresAt': self.clock() + 900}
                for key in list(self.confirmations):
                    if self.confirmations[key]['sourceId'] == plan['sourceId']: del self.confirmations[key]
                self.confirmations[cid] = approval
                self._write(Path('approvals') / (plan['planId'] + '.json'),
                            {k: v for k, v in approval.items() if k != 'confirmationId'})
                return {**approval, 'state': 'confirmed_not_saved'}
        raise ValueError('未开放此操作')

    def _preview(self, data):
        sid = data.get('sourceId')
        source = self._invalidate(sid)
        # Deep-copy only data; no implicit defaults from any historical customer or order.
        inp = copy.deepcopy({k: data.get(k) for k in ('rows', 'header', 'mode')})
        if self.order_namespace:
            inp['orderIdentity'] = hashlib.sha256((self.order_namespace + ':' + source['path']).encode()).hexdigest()
        if inp['mode'] not in ('live', 'mock'): raise ValueError('必须明确选择live或mock')
        if not isinstance(inp['rows'], list) or not isinstance(inp['header'], dict):
            raise ValueError('订单字段结构无效')
        local = check_header_delivery(build_plan(inp['rows'], inp['header'], inp['mode'], inp.get('orderIdentity')), inp['header'], inp['mode'])
        upstream_instance = None
        if local['ready'] and inp['mode'] == 'live':
            upstream = self.transport('/api/preview', inp)
            if upstream.get('digest') != local['digest'] or not upstream.get('ready'):
                raise ValueError('本机用友服务预览版本不一致，请检查适配器版本')
            upstream_id = upstream['planId']
            upstream_instance = upstream.get('serviceInstanceId')
        else: upstream_id = None
        pid = secrets.token_urlsafe(24)
        plan = {**local, 'sourceId': sid, 'planId': pid, 'revision': source['revision'],
                'inputHash': digest(inp), 'input': inp, 'sourceHash': source['fileHash'],
                'upstreamPlanId': upstream_id, 'upstreamInstanceId': upstream_instance, 'created': self.clock(), 'finalExcel': None}
        # Retain a bounded in-memory set; durable review files remain as audit evidence.
        for key in list(self.plans):
            if self.clock() - self.plans[key]['created'] > 1800: del self.plans[key]
        if len(self.plans) >= 200: raise ValueError('预览数量达到限制，请稍后再试')
        self.plans[pid] = plan
        source['planId'] = pid
        self._write(Path('reviews') / (pid + '.json'), plan)
        return browser_safe({k: v for k, v in plan.items() if k not in ('upstreamPlanId', 'created')})

    def _check_evidence(self, plan):
        evidence = plan.get('finalExcel')
        if not evidence: raise ValueError('最终确认Excel尚未落盘并绑定此版本')
        _, _, parsed = self._file(evidence['path'])
        if parsed['fileHash'] != evidence['fileHash']:
            raise ValueError('最终Excel已变化，请重新生成并确认')

    def _prepare_save(self, data):
        with self.lock:
            plan = self._active(data)
            if data.get('confirmation') != SAVE_TEXT: raise ValueError('需要单独确认保存，不提交不审核')
            cid = data.get('confirmationId')
            approval = self.confirmations.get(cid) if isinstance(cid, str) else None
            if not approval or approval['planId'] != plan['planId'] or approval['expiresAt'] <= self.clock():
                raise ValueError('人工确认不存在、已过期或已使用')
            current = {k: data.get(k) for k in ('rows', 'header', 'mode')}
            if plan['input'].get('orderIdentity'): current['orderIdentity'] = plan['input']['orderIdentity']
            if current.get('mode') == 'live' and local_field_warnings(current.get('header') or {}):
                raise ValueError('存在尚未接入用友的本机字段，请先完成字段映射后重新预览')
            if digest(current) != plan['inputHash']:
                self._invalidate(plan['sourceId'])
                raise ValueError('确认后订单内容发生变化，必须重新检查')
            self._check_evidence(plan)
            if not plan['ready']: raise ValueError('订单仍有缺项')
            if plan['mode'] == 'live':
                try: status = self.transport('/api/status')
                except Exception: raise ValueError('连接状态未确认，尚未发送保存请求，请稍后重新检查') from None
                if not status.get('configured') or not status.get('liveSaveEnabled'):
                    raise ValueError('用友连接或真实保存尚未开放，请重新检查连接状态')
                if status.get('serviceInstanceId') and plan.get('upstreamInstanceId') != status['serviceInstanceId']:
                    raise ValueError('用友连接已重启，请重新预览并确认当前订单')
                scopes = status.get('saveScopes')
                header = current.get('header') or {}
                if scopes is not None and not any(s.get('salesOrgId') == header.get('salesOrgId') and s.get('transactionTypeId') == header.get('transactionTypeId') for s in scopes):
                    raise ValueError('当前组织或交易类型尚未开放保存，请核实订单配置')
            # An exclusive persistent marker protects duplicate clicks and process restarts.
            marker = self.state / 'attempts' / (plan['digest'] + '.json')
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open('x', encoding='utf-8') as f:
                    json.dump({'state': 'dispatch_started', 'planId': plan['planId'],
                               'inputHash': plan['inputHash'], 'at': self.clock()}, f)
                    f.flush()
                    os.fsync(f.fileno())
            except FileExistsError:
                raise ValueError('相同订单已尝试保存，禁止重复发送；请先核查回执') from None
            del self.confirmations[cid]
            return plan

    def _save(self, data):
        try:
            plan = self._prepare_save(data)
        except ValueError as error:
            raise PreflightError(str(error)) from None
        if plan['mode'] == 'mock':
            result = {'state': 'mock_saved', 'id': 'MOCK-' + plan['digest'][:12],
                      'message': '模拟流程完成，未连接用友、未创建真实订单'}
        else:
            try:
                result = self.transport('/api/save', {'planId': plan['upstreamPlanId'], 'confirmation': SAVE_TEXT})
            except Exception:
                result = {'state': 'uncertain', 'message': '保存结果未确认，禁止自动重试；请核查既有订单与本机服务回执'}
        result = {**result, 'planId': plan['planId'], 'digest': plan['digest'],
                  'inputHash': plan['inputHash'], 'finalExcelHash': plan['finalExcel']['fileHash']}
        with self.lock:
            self._write(Path('attempts') / (plan['digest'] + '.json'), result)
        return browser_safe(result)

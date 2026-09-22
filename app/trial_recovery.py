"""Reconcile only a server-owned batch's existing outcome. Never resend writes."""
import json
import re
import time

STATES={'dispatch_started','saved_readback_pending','saved_verified','saved_needs_review','uncertain','not_dispatched','mock_saved'}


def check_outcome(workspace, batch_id):
    batch=workspace.STORE.get(batch_id)
    receipt=batch.get('receipt') or {}
    if not receipt:raise ValueError('此批次没有保存尝试，请继续原录单流程')
    final=batch.get('final') or {}
    digest=final.get('digest')
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
        raise ValueError('缺少本批次确认版本，请联系管理员核查原记录')
    gateway=workspace.gateway()
    check={'checkedAt':time.time(),'readOnly':True,'state':'needs_admin',
           'message':'当前结果仍需管理员核查，请勿新建或重复发送同一订单。'}
    recovered=None
    if receipt.get('state') not in ('saved_verified','mock_saved'):
        # A successful gateway result may exist even when the browser timed out.
        marker=gateway.state/'attempts'/(digest+'.json')
        try:local=json.loads(marker.read_text(encoding='utf-8'))
        except (OSError,ValueError):local={}
        if local.get('digest')==digest and local.get('state')=='saved_verified':recovered=local
        else:
            try:
                diagnostic=gateway.transport('/api/diagnostic?digest='+digest)
                if isinstance(diagnostic,dict) and diagnostic.get('state') in STATES:recovered=diagnostic
            except Exception:
                check['message']='暂时无法读取连接服务回执，原保存记录已保留。请稍后手动核查或联系管理员，勿重复保存。'
    if recovered:
        # Keep the original receipt separately and copy only known outcome fields.
        safe={k:recovered[k] for k in ('state','id','code','status','stage','mismatch') if k in recovered}
        if safe.get('state')=='saved_verified' and not re.fullmatch(r'[0-9]{1,20}',str(safe.get('id',''))):
            safe['state']='uncertain'
        receipt={**receipt,**safe,'digest':digest,
                 'message':'已读取既有保存回执；本次没有重新保存或提交。'}
    identity=str(receipt.get('id') or '')
    if re.fullmatch(r'[0-9]{1,20}',identity):
        try:
            response=gateway.transport('/api/order-query',{'id':identity})
            order=response.get('data') or {}
            if str(response.get('code'))!='200' or str(order.get('id'))!=identity:
                raise ValueError('Order identity unavailable')
            status=str(order.get('status',''))
            check.update(state='found',code=str(order.get('code',''))[:80],status=status,
                message='已找到原订单，当前状态：'+{'0':'开立','1':'已审核','3':'审批中'}.get(status,'请管理员核对')+'。未重复保存或提交。')
            if receipt.get('state')=='saved_needs_review':
                check['message']+=' 原回读差异仍需管理员核对，不能据此直接提交。'
            if batch.get('submission') and status in ('1','3'):
                check['submission']={**batch['submission'],'state':'submitted_verified','status':status,
                                     'message':'已只读核查：原订单处于已提交后的状态，未再次提交。'}
        except Exception:
            check['message']='已保留原单号，但暂时无法确认用友当前状态；请联系管理员核查，勿重复保存或提交。'
    elif receipt.get('state')=='mock_saved':
        check.update(state='mock',message='这是模拟保存记录，没有真实用友订单。')
    elif recovered:
        check['message']='已读取保存尝试记录，但尚无确切订单编号。请管理员核对原批次与连接服务回执，勿重新建单。'
    with workspace.LOCK:
        current=workspace.STORE.get(batch_id)
        # A save may finish while this read-only check is waiting on the connector.
        # Never overwrite that newer result with an earlier pending/error record.
        if current.get('receipt')!=batch.get('receipt') or current.get('submission')!=batch.get('submission'):
            # Discard the whole stale observation, including its old order/status.
            return {'receipt':current.get('receipt'),'submission':current.get('submission'),
                    'outcomeCheck':{'checkedAt':time.time(),'readOnly':True,'state':'changed',
                    'message':'本批次在核查期间已有更新，已保留最新回执；本次旧核查结果未应用。请查看回执，必要时稍后重新核查。'}}
        current.setdefault('originalReceipt',current.get('receipt'))
        current['receipt']=receipt
        current['phase']=receipt.get('state',current.get('phase'))
        current['outcomeCheck']={k:v for k,v in check.items() if k!='submission'}
        if check.get('submission') and current.get('submission')==batch.get('submission'):
            current['submission']=check['submission']
        workspace.STORE.persist(current)
    return {'receipt':receipt,'submission':current.get('submission'),'outcomeCheck':current['outcomeCheck']}

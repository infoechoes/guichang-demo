"""Explicit submission of an order with this connector's successful save receipt.

Official API c5dc0c54f08a41aeb3b838efa1c4d14d: batchsubmit data=[{id}].
No retries, arbitrary order IDs, approval, withdrawal or delete operations.
"""
import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path
from core import wire, browser_safe
from save_policy import require_save_allowed

CONFIRM = '确认提交此订单，按用友审批流程处理'


def fingerprint(order):
    fields = ('id','code','agentId','salesOrgId','transactionTypeId','payMoney','status','pubts','ts','orderDetails','orderPrices')
    return hashlib.sha256(wire({k:order.get(k) for k in fields}).encode()).hexdigest()


class Submissions:
    def __init__(self, state, client, clock=time.time):
        self.state, self.client, self.clock = Path(state), client, clock
        self.plans, self.lock = {}, threading.RLock()

    def order(self, identity):
        response = self.client.call('detail', query={'id':identity})
        order = response.get('data') or {}
        if str(response.get('code')) != '200' or str(order.get('id')) != identity:
            raise ValueError('订单不存在或无法读取，请核查是否已删除')
        return order

    def preview(self, digest):
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest): raise ValueError('保存记录无效')
        try: receipt = json.loads((self.state/(digest+'.json')).read_text(encoding='utf-8'))
        except (OSError,ValueError): raise ValueError('没有此订单的本机成功保存记录') from None
        if receipt.get('state') != 'saved_verified': raise ValueError('请先完成保存及回读核对')
        identity = str(receipt['id'])
        order = self.order(identity)
        require_save_allowed(self.state,order)
        if str(order.get('status')) != '0': raise ValueError('当前订单已不是开立状态，不能重复提交')
        marker = self.state/'submissions'/(identity+'.json')
        if marker.exists(): raise ValueError('此订单已有提交尝试，请核查结果，不自动重试')
        with self.lock:
            self.plans = {k:v for k,v in self.plans.items() if v['expires']>self.clock()}
            if len(self.plans)>=100: raise ValueError('提交预览过多，请稍后再试')
            token=secrets.token_urlsafe(32)
            self.plans[token]={'id':identity,'digest':digest,'fingerprint':fingerprint(order),'expires':self.clock()+300}
        return browser_safe({'token':token,'id':identity,'code':order.get('code'),'customer':order.get('agentId_name'),
                             'amount':order.get('payMoney'),'items':len(order.get('orderDetails') or []),'confirmation':CONFIRM})

    def submit(self, token, confirmation):
        if confirmation != CONFIRM: raise ValueError('需要明确确认提交订单')
        with self.lock:
            plan=self.plans.pop(token,None)
        if not plan or plan['expires']<=self.clock(): raise ValueError('提交预览已失效，请重新核对')
        order=self.order(plan['id'])
        require_save_allowed(self.state,order)
        if fingerprint(order)!=plan['fingerprint']: raise ValueError('用友订单已变化，请重新预览后确认')
        folder=self.state/'submissions';folder.mkdir(exist_ok=True)
        marker=folder/(plan['id']+'.json')
        try:
            with marker.open('x',encoding='utf-8') as f:
                json.dump({'state':'submission_started','id':plan['id'],'at':self.clock()},f)
                f.flush()
                import os
                os.fsync(f.fileno())
        except FileExistsError: raise ValueError('此订单已有提交尝试，不会重复提交') from None
        result={'state':'submission_uncertain','id':plan['id'],'code':order.get('code'),'message':'提交结果待核查，请勿重复提交'}
        submission_sent=False
        try:
            response=self.client.call('submit',{'data':[{'id':int(plan['id'])}]})
            submission_sent=True
            data=response.get('data') or {}
            if str(response.get('code'))=='200' and str(data.get('sucessCount'))=='1' and str(data.get('failCount'))=='0':
                current=self.order(plan['id'])
                if str(current.get('status')) in ('1','3'):
                    result.update(state='submitted_verified',status=str(current['status']),message='已提交，并回读确认用友状态')
                else: result.update(message='用友已返回提交成功，当前状态需核查，请勿重复提交')
            else: result.update(message='用友未确认提交成功，请核查接口授权及订单状态')
        except Exception as error:
            diagnostic=getattr(error,'diagnostic',{})
            if not submission_sent and diagnostic.get('requestDispatched') is False:
                result.update(state='submission_not_dispatched',message='提交请求尚未发出，已保留处理记录；请联系维护人员处理后再操作。')
            # A dispatched write or failed readback stays uncertain; never retry.
        result['at']=self.clock()
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');temporary.replace(marker)
        return result

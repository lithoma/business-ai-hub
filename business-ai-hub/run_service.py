"""Shared manual/scheduled execution and durable email review; no automatic timers.

SQL storage reuses the existing runs table. SQLite uses a unique index; Delta must
have ONE process dispatching/claiming runs (MERGE is not a uniqueness constraint).
All business calls are injected adapters under an explicit, owner-bound identity.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import date, datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
import base64
import hashlib
import json
import threading
from uuid import uuid4

from fastapi import HTTPException
from hub_auth import current_identity, owns, ownership_params, ownership_sql, _context, verified_identity
from local_trials import TrialRequest, SendRequest
from task_execution import table_name
from telemetry import record_action


def stamp():return datetime.now(timezone.utc).isoformat()
def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def runtime(row):
    data=json.loads(row.get('artifacts_json') or '[]')
    return next((item for item in data if isinstance(item,dict) and item.get('kind')=='hub_run'),{})


class RunRepository:
    def __init__(self, query, table, *, sqlite=False):
        self.query,self.table,self.sqlite=query,table_name(table),sqlite
        self.lock=threading.RLock()

    def get(self,key):
        rows=self.query(f'SELECT * FROM {self.table} WHERE run_id=:key AND {ownership_sql("config_json")}',
                        {'key':key,**ownership_params()}) or []
        if len(rows)!=1:raise HTTPException(404,'运行记录不存在或不属于当前用户。')
        return rows[0]

    def find(self,source,task_id,slot):
        rows=self.query(f'''SELECT * FROM {self.table} WHERE source=:source AND task_id=:task_id
            AND scheduled_slot=:slot AND {ownership_sql("config_json")}''',
            dict(source=source,task_id=task_id,slot=slot,**ownership_params())) or []
        if len(rows)>1:raise HTTPException(409,'执行记录重复，已停止调用；请检查单执行器配置。')
        return rows[0] if rows else None

    def insert(self,row):
        row=dict(row)
        if not self.sqlite:row.setdefault('attempt_id',uuid4().hex)
        columns=','.join(row);values=','.join(':'+key for key in row)
        if self.sqlite:
            self.query(f'INSERT OR IGNORE INTO {self.table} ({columns}) VALUES ({values})',row)
        else:
            projection=','.join(':'+key+' '+key for key in row)
            self.query(f'''MERGE INTO {self.table} t USING (SELECT {projection}) s ON t.run_id=s.run_id
                WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({','.join('s.'+key for key in row)})''',row)
        return self.get(row['run_id'])

    def update(self,key,changes,*,expected=None):
        params={'key':key,**changes,**ownership_params()}
        where=''
        for field,value in (expected or {}).items():
            where+=' AND '+field+'=:old_'+field;params['old_'+field]=value
        rows=self.query(f'''UPDATE {self.table} SET {','.join(key+'=:'+key for key in changes)}
            WHERE run_id=:key AND {ownership_sql("config_json")}{where}''',params)
        return bool(rows and int(rows[0].get('num_affected_rows',0))==1)


class RunService:
    def __init__(self,repo,load_tasks,condition,report,preview,send=None,*,sender='',recipient='',mode='configured',legacy=None,creator_resolver=None):
        self.repo,self.load_tasks=repo,load_tasks
        self.condition,self.report,self.preview_message,self.send_message=condition,report,preview,send
        self.sender,self.recipient,self.mode,self.legacy=sender,recipient,mode,legacy
        self.creator_resolver=creator_resolver
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='hub-run')
        self.pending=0
        self.instance=uuid4().hex
        self.recovered=set()

    def recover_interrupted(self):
        """One exclusive API/dispatcher process; never restart a remote side effect."""
        owner=current_identity().key
        with self.repo.lock:
            if owner in self.recovered:return
            rows=self.repo.query(f'''SELECT * FROM {self.repo.table}
                WHERE source IN ('manual','scheduled') AND {ownership_sql('config_json')}
                AND (status='RUNNING' OR notification_status='EMAIL_SENDING')''',ownership_params()) or []
            for row in rows:
                meta=runtime(row)
                if not meta or meta.get('worker')==self.instance:continue
                changes={}
                if row['status']=='RUNNING':
                    meta.update(phase='failed',error='服务重启中断；没有自动重试。')
                    changes.update(status='FAILED',error_code='Interrupted',finished_at=stamp())
                if row['notification_status']=='EMAIL_SENDING':
                    changes.update(notification_status='EMAIL_UNKNOWN',notification_error_code='InterruptedSend')
                changes['artifacts_json']=json.dumps([meta],ensure_ascii=False)
                self.repo.update(row['run_id'],changes)
            self.recovered.add(owner)

    def settings(self):
        return {'enabled':True,'sender':self.sender,'recipient':self.recipient,
                'mail_available':self.send_message is not None,'identity_mode':self.mode,
                'api':'runs','scheduler_enabled':False,'single_dispatcher_required':True}

    def task(self,key):
        rows=[t for t in self.load_tasks() if t.get('task_id')==key and t.get('_source')=='registry' and owns(t)]
        if len(rows)!=1:raise HTTPException(404,'任务不存在或不属于当前运行身份。')
        return rows[0]

    def start(self,task_id,request):
        if not request.confirm:raise HTTPException(400,'请确认真实查询与报告生成；试运行不会发送邮件。')
        with self.repo.lock:
            task=self.task(task_id)
            if str(task['updated_at'])!=request.expected_updated_at:raise HTTPException(409,'任务已修改，请刷新后重试。')
            if task['trigger']['type']=='unconfigured' or not task['report'].get('prompt','').strip():
                raise HTTPException(422,'请补齐触发条件与报告要求。')
            day=request.business_date.isoformat()
            slot='manual:'+digest(str(task['updated_at']))[:20]+':'+day
            if request.retry_of:
                parent=self.repo.get(request.retry_of)
                if parent['task_id']!=task_id or parent['status']!='FAILED' or runtime(parent).get('business_date')!=day or runtime(parent).get('version')!=str(task['updated_at']):
                    raise HTTPException(409,'只能明确重试同一任务版本、同一日期的失败记录。')
                slot='retry:'+request.retry_of
            request_id=digest(current_identity().key+':manual:'+str(request.request_key))
            try:replay=self.repo.get(request_id)
            except HTTPException as exc:
                if exc.status_code!=404:raise
                replay=None
            if replay:
                if replay['task_id']!=task_id or replay['scheduled_slot']!=slot:raise HTTPException(409,'请求编号已用于另一操作。')
                return {'run_id':request_id,'reused':True}
            return self._start(task,'manual',slot,day,request_id)

    def _start(self,task,source,slot,day,key=None,dispatch_date=None,execution_context=None):
        # Called while holding the repository lock; no model call occurs before claim.
        existing=self.repo.find(source,task['task_id'],slot)
        if existing:return {'run_id':existing['run_id'],'reused':True}
        if self.pending>=2:raise HTTPException(429,'已有两次执行在处理，请稍后再试。')
        key=key or digest(current_identity().key+':'+source+':'+task['task_id']+':'+slot)
        token=uuid4().hex
        meta={'kind':'hub_run','phase':'queued','version':str(task['updated_at']),
              'business_date':day,'timings':{},'error':'','claim':token,'worker':self.instance}
        if execution_context is not None:meta['execution']=dict(execution_context)
        row=dict(run_id=key,task_id=task['task_id'],task_name=task['name'],source=source,
            scheduled_slot=slot,dispatch_date=dispatch_date or date.today().isoformat(),status='RUNNING',
            trigger_reason='',report_text='',error_code='',notification_status='NOT_QUEUED',
            started_at=stamp(),artifacts_json=json.dumps([meta]),config_json=json.dumps(task,ensure_ascii=False,default=str))
        saved=self.repo.insert(row)
        if runtime(saved).get('claim')!=token:return {'run_id':key,'reused':True}
        self.pending+=1
        try:self.pool.submit(copy_context().run,self.work,key)
        except Exception:
            self.pending-=1
            self._save(key,phase='failed',error='工作线程不可用，未调用模型。',status='FAILED',error_code='WorkerUnavailable',finished_at=stamp())
            raise HTTPException(503,'执行未启动；记录已保留，可明确重试。') from None
        return {'run_id':key,'reused':False}

    def _save(self,key,*,phase=None,error=None,timings=None,**changes):
        with self.repo.lock:
            row=self.repo.get(key);meta=runtime(row)
            if phase is not None:meta['phase']=phase
            if error is not None:meta['error']=error
            if timings is not None:meta['timings']=timings
            changes['artifacts_json']=json.dumps([meta],ensure_ascii=False)
            if not self.repo.update(key,changes):raise RuntimeError('Run update was not confirmed')

    def work(self,key):
        import time
        started=time.monotonic();timings={};phase='condition'
        try:
            row=self.repo.get(key);task=json.loads(row['config_json'])
            live=self.task(task['task_id'])
            if str(live['updated_at'])!=runtime(row)['version']:
                raise ValueError('排队后任务已修改；未运行过期配置，请明确重新发起。')
            if row['source']=='scheduled' and (live.get('enabled') is not True or live.get('lifecycle_status')!='active'):
                raise ValueError('排队后任务已暂停，未运行。')
            with record_action('task_run'):
                self._authorize_creator(task,row['source'])
                self._save(key,phase=phase)
                clock=time.monotonic();result,values=self.condition(task,runtime(row)['business_date'])
                if type(result.get('should_trigger')) is not bool:raise ValueError('条件不是严格布尔值；不会生成报告。')
                timings['condition_seconds']=round(time.monotonic()-clock,3)
                if row['source']=='scheduled':values['dispatch_date']=str(row['dispatch_date'])
                task['_execution_context']=values
                self._save(key,trigger_reason=result['reason'],config_json=json.dumps(task,ensure_ascii=False,default=str),timings=timings)
                if result['should_trigger']:
                    phase='report';self._save(key,phase=phase)
                    self._authorize_creator(task,row['source'])
                    clock=time.monotonic();report=self.report(task,values,result['reason'])
                    timings['report_seconds']=round(time.monotonic()-clock,3)
                    if not isinstance(report,str) or not report.strip() or len(report)>12000:raise ValueError('报告为空或超过 12000 字符，请缩小范围。')
                    status='REPORT_CREATED'
                else:status,report='NOT_TRIGGERED',''
                timings['total_seconds']=round(time.monotonic()-started,3)
                self._save(key,phase='completed',timings=timings,status=status,report_text=report,finished_at=stamp())
        except Exception as exc:
            timings['total_seconds']=round(time.monotonic()-started,3)
            detail=str(exc)[:1000] if isinstance(exc,ValueError) else type(exc).__name__
            self._save(key,phase='failed',error=phase+'：'+detail,timings=timings,status='FAILED',error_code=type(exc).__name__,finished_at=stamp())
        finally:
            with self.repo.lock:self.pending-=1

    def _authorize_creator(self,task,source):
        user=current_identity()
        if user.mode in ('apps_obo','oauth_user','obo_user'):
            from task_authorization import verify_creator
            if source=='scheduled':
                if self.creator_resolver is None:raise PermissionError('Creator resolver required')
                resolved=self.creator_resolver(task)
                if resolved is None or resolved.key!=user.key:
                    raise PermissionError('Creator execution identity mismatch')
                user=resolved
            verify_creator(task,user)
            # Each work() is run in its own copied Context; never update a global SDK.
            _context.set(user)

    def status(self,key):
        row=self.repo.get(key);meta=runtime(row)
        if not meta and self.legacy:return self.legacy.status(key)
        if not meta:raise HTTPException(404,'该记录没有交互运行状态。')
        result={name:row.get(name,'') for name in ('run_id','task_id','status','trigger_reason','report_text','notification_status','provider_message_id','notification_error_code')}
        return dict(result,phase=meta['phase'],error=meta.get('error',''),timings=meta.get('timings',{}),cost=None)

    def preview(self,key):
        with self.repo.lock:
            row=self.repo.get(key);meta=runtime(row)
            if not meta and self.legacy:return self.legacy.preview(key)
            if row['status']!='REPORT_CREATED':raise HTTPException(409,'报告未生成，不能预览邮件。')
            if not meta.get('mime'):
                try:raw=self.preview_message(row,json.loads(row['config_json']))
                except ValueError as exc:raise HTTPException(422,str(exc)[:500]) from None
                if len(raw)>256000:raise HTTPException(422,'交互邮件暂不支持大附件；请先使用文本报告。')
                message=BytesParser(policy=policy.default).parsebytes(raw)
                if (self.sender and str(message['From'])!=self.sender) or (self.recipient and (str(message['To'])!=self.recipient or message.get('Cc') or message.get('Bcc'))):
                    raise HTTPException(409,'邮件地址与批准的通道不一致。')
                meta.update(mime=base64.b64encode(raw).decode(),preview_hash=hashlib.sha256(raw).hexdigest(),
                    sender=str(message['From']),recipient=str(message['To']),subject=str(message['Subject']))
                if not self.repo.update(key,{'artifacts_json':json.dumps([meta],ensure_ascii=False)}):raise HTTPException(409,'预览保存失败，请重新核对。')
            return dict(run_id=key,sender=meta['sender'],recipient=meta['recipient'],subject=meta['subject'],body=row['report_text'],
                preview_hash=meta['preview_hash'],can_send=bool(self.send_message) and row['notification_status']=='NOT_QUEUED',
                notification_status=row['notification_status'],timings=meta.get('timings',{}))

    def send(self,key,request):
        with self.repo.lock:
            row=self.repo.get(key)
            if not runtime(row) and self.legacy:return self.legacy.send(key,request)
            if not request.confirm:raise HTTPException(400,'请确认发送这一封邮件。')
            # A send never creates its own preview. The preview must already be durable.
            meta=runtime(row)
            if not meta.get('mime') or meta.get('preview_hash')!=request.preview_hash:raise HTTPException(409,'请先预览并核对这封邮件。')
            if not self.send_message:raise HTTPException(503,'发信通道未配置。')
            if row['status']!='REPORT_CREATED' or not self.repo.update(key,{'notification_status':'EMAIL_SENDING'},expected={'notification_status':'NOT_QUEUED','status':'REPORT_CREATED'}):
                raise HTTPException(409,'邮件已尝试发送，不会重复发送。')
            raw=base64.b64decode(meta['mime']);started=datetime.now(timezone.utc)
        try:
            message_id=self.send_message(raw)
            if not isinstance(message_id,str) or not message_id:raise RuntimeError('Missing provider receipt')
            status,error='EMAIL_ACCEPTED',''
        except Exception as exc:
            from report_email import EmailRejected
            status,error,message_id=('EMAIL_FAILED','EmailRejected','') if isinstance(exc,EmailRejected) else ('EMAIL_UNKNOWN','ProviderOutcomeUnknown','')
        timings=meta.get('timings',{});timings['email_seconds']=round((datetime.now(timezone.utc)-started).total_seconds(),3)
        self._save(key,timings=timings,notification_status=status,provider_message_id=message_id,notification_error_code=error)
        return {'notification_status':status,'provider_message_id':message_id,'error':error}

    def schedule_plan(self,now):
        from schedule_core import plan
        rows=[]
        for task in self.load_tasks():
            if task.get('_source')!='registry' or not owns(task):continue
            try:
                value=plan(task['schedule'],now)
                value.update(task_id=task['task_id'],name=task['name'],active=task.get('enabled') is True and task.get('lifecycle_status')=='active')
                value['already_claimed']=bool(value['slot'] and self.repo.find('scheduled',task['task_id'],value['slot']))
            except (ValueError,KeyError) as exc:
                value={'task_id':task['task_id'],'name':task['name'],'error':str(exc),'due':False,'active':False}
            rows.append(value)
        return {'scheduler_enabled':False,'mode':'per_task','checked_at':now.isoformat(),'tasks':rows,
                'note':'这是计划预览；没有启动调度、查询业务数据或调用模型。'}

    def dispatch_once(self,now,resolve_identity,*,enabled=False,execution_context=None):
        """Explicit external tick only. No internal timers, automatic email or retries."""
        if not enabled:raise RuntimeError('Scheduler disabled; no run claimed or business call made')
        if resolve_identity is None:raise PermissionError('Explicit execution identity resolver required')
        from schedule_core import plan
        outcomes=[]
        for snapshot in self.load_tasks():
            if snapshot.get('_source')!='registry' or snapshot.get('enabled') is not True or snapshot.get('lifecycle_status')!='active':continue
            try:
                if not plan(snapshot['schedule'],now)['due']:continue
            except (ValueError,KeyError):
                outcomes.append({'task_id':snapshot['task_id'],'status':'FAILED','error':'InvalidSchedule','claimed':False})
                continue
            try:
                identity=resolve_identity(snapshot)
                if identity is None or not owns(snapshot,identity):raise PermissionError('Task execution identity mismatch')
                if identity.mode in ('apps_obo','oauth_user','obo_user'):
                    from task_authorization import require_creator
                    require_creator(snapshot,identity)
            except Exception:
                # No owner authority: do not impersonate the owner to write their ledger.
                outcomes.append({'task_id':snapshot['task_id'],'status':'FAILED','error':'ExecutionIdentityUnavailable','claimed':False})
                continue
            marker=_context.set(identity)
            try:
                self.recover_interrupted()
                task=self.task(snapshot['task_id'])
                if task.get('enabled') is not True or task.get('lifecycle_status')!='active':continue
                value=plan(task['schedule'],now)
                if not value['due']:continue
                day=(date.fromisoformat(value['dispatch_date'])-timedelta(days=1)).isoformat()
                with self.repo.lock:
                    slot=value['slot'];existing=self.repo.find('scheduled',task['task_id'],slot)
                    if existing:outcomes.append({'run_id':existing['run_id'],'reused':True});continue
                    try:
                        if identity.mode=='databricks_app':
                            from apps_auth import verified_app_identity
                            actual=verified_app_identity(identity.client)
                        else:actual=verified_identity(identity.client,'scheduled_user')
                        if actual.key!=identity.key:raise PermissionError('Resolved SDK identity mismatch')
                        errors=[];error_code='InvalidConfiguration'
                    except Exception:
                        errors=['无法验证任务创建者的有效 Databricks 授权；未查询业务数据。']
                        error_code='ExecutionIdentityUnavailable'
                    if not errors:
                        from task_execution import readiness
                        errors=readiness(task,for_execution=True)['errors']
                    if errors:
                        key=digest(identity.key+':scheduled:'+task['task_id']+':'+slot)
                        meta={'kind':'hub_run','phase':'failed','error':'；'.join(errors),'business_date':day,'version':str(task['updated_at']),'timings':{}}
                        self.repo.insert(dict(run_id=key,task_id=task['task_id'],task_name=task['name'],source='scheduled',scheduled_slot=slot,
                            dispatch_date=value['dispatch_date'],status='FAILED',trigger_reason='',report_text='',error_code=error_code,
                            notification_status='NOT_QUEUED',started_at=stamp(),finished_at=stamp(),config_json=json.dumps(task,default=str),artifacts_json=json.dumps([meta],ensure_ascii=False)))
                        outcomes.append({'run_id':key,'status':'FAILED'});continue
                    try:outcomes.append(self._start(task,'scheduled',value['slot'],day,dispatch_date=value['dispatch_date'],execution_context=execution_context))
                    except HTTPException as exc:
                        if exc.status_code!=429:raise
                        outcomes.append({'task_id':task['task_id'],'status':'DEFERRED','claimed':False})
            finally:_context.reset(marker)
        return {'runs':outcomes,'automatic_email':False}

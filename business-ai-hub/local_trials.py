"""Opt-in loopback trial UI. No scheduling, cloud writes or implicit email delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from hub_auth import owns, current_identity
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.routing import Mount
from telemetry import record_action, span


def now(): return datetime.now(timezone.utc).isoformat()


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    business_date: date
    expected_updated_at: str
    request_key: UUID
    confirm: bool = False
    retry_of: str | None = None


class SendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_hash: str
    confirm: bool = False


class LocalTrials:
    def __init__(self, database, artifacts, load_tasks, condition, report, preview, sender, recipient, send=None):
        self.database, self.artifacts = Path(database), Path(artifacts)
        self.load_tasks, self.condition, self.report = load_tasks, condition, report
        self.preview_message, self.send_message = preview, send
        self.sender, self.recipient = sender, recipient
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='hub-manual-trial')
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS local_trials (
                run_id TEXT PRIMARY KEY, request_key TEXT UNIQUE, parent_id TEXT UNIQUE,
                task_id TEXT, business_date TEXT, config_version TEXT, phase TEXT,
                config_json TEXT, timings_json TEXT, error TEXT, created_at TEXT)''')
            # Interrupted runs require a new explicitly requested attempt; never replay on startup.
            interrupted=[r['run_id'] for r in db.execute("SELECT run_id FROM local_trials WHERE phase IN ('queued','condition','report')")]
            for key in interrupted:
                db.execute("UPDATE local_trials SET phase='failed',error='本地服务重启中断；没有自动重试。' WHERE run_id=?",(key,))
                db.execute("UPDATE task_runs SET status='FAILED',error_code='Interrupted',finished_at=? WHERE run_id=?",(now(),key))
            db.execute("UPDATE task_runs SET notification_status='EMAIL_UNKNOWN',notification_error_code='InterruptedSend' WHERE source='manual_local' AND notification_status='EMAIL_SENDING'")

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.database,timeout=10)
        connection.row_factory=sqlite3.Row
        try:
            with connection: yield connection
        finally: connection.close()

    def import_report(self, run_id, snapshot, timings=None):
        with self.db() as db:
            row=db.execute("SELECT * FROM task_runs WHERE run_id=? AND source='manual_local' AND status='REPORT_CREATED'",(run_id,)).fetchone()
            if row is None or not snapshot.get('report',{}).get('security'): return
            db.execute('UPDATE task_runs SET config_json=? WHERE run_id=?',(json.dumps(snapshot,ensure_ascii=False),run_id))
            db.execute('INSERT OR IGNORE INTO local_trials VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (run_id,'import:'+run_id,None,row['task_id'],snapshot['_execution_context']['business_date'],
                 'imported','completed',json.dumps(snapshot,ensure_ascii=False),json.dumps(timings or {}),'',row['started_at']))

    def get(self, key):
        with self.db() as db:
            row=db.execute('''SELECT t.*,r.status,r.report_text,r.trigger_reason,r.notification_status,
                r.provider_message_id,r.notification_error_code FROM local_trials t
                JOIN task_runs r ON r.run_id=t.run_id WHERE t.run_id=?''',(key,)).fetchone()
        if row is None or not owns(json.loads(row['config_json'])):
            raise HTTPException(404,'试运行记录不存在或不属于当前用户。')
        return dict(row)

    def status(self, key):
        row=self.get(key)
        return {name:row[name] for name in ('run_id','task_id','business_date','phase','status','error',
            'trigger_reason','report_text','notification_status','provider_message_id','notification_error_code')} | {
            'timings':json.loads(row['timings_json']), 'cost':None}

    def start(self, task_id, request):
        if not request.confirm: raise HTTPException(400,'请确认真实查询和报告生成；试运行不会发送邮件。')
        tasks=[task for task in self.load_tasks() if task['task_id']==task_id and task.get('_source')=='registry']
        if len(tasks)!=1: raise HTTPException(404,'本地 Registry 任务不存在。')
        task=tasks[0]
        if not owns(task): raise HTTPException(404,'任务不存在。')
        if task['updated_at']!=request.expected_updated_at: raise HTTPException(409,'任务已更新，请刷新后试运行。')
        if task['trigger']['type']=='unconfigured': raise HTTPException(422,'请先补充触发条件。')
        if not task['report'].get('prompt','').strip(): raise HTTPException(422,'请先填写报告要求。')
        key=uuid4().hex
        day=request.business_date.isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT * FROM local_trials WHERE request_key=?',(str(request.request_key),)).fetchone()
            if previous:
                if not owns(json.loads(previous['config_json'])): raise HTTPException(404,'请求不存在。')
                if previous['task_id']!=task_id or previous['business_date']!=day: raise HTTPException(409,'请求编号已用于另一任务或日期。')
                return {'run_id':previous['run_id'],'reused':True}
            if request.retry_of:
                parent=db.execute('SELECT * FROM local_trials WHERE run_id=?',(request.retry_of,)).fetchone()
                if not parent or not owns(json.loads(parent['config_json'])) or parent['phase']!='failed' or parent['task_id']!=task_id or parent['business_date']!=day or parent['config_version']!=task['updated_at']:
                    raise HTTPException(409,'只能明确重试同一配置、同一日期的失败试运行。')
                child=db.execute('SELECT run_id FROM local_trials WHERE parent_id=?',(request.retry_of,)).fetchone()
                if child: return {'run_id':child['run_id'],'reused':True}
            else:
                previous=db.execute('''SELECT run_id FROM local_trials WHERE task_id=? AND business_date=?
                    AND config_version=? ORDER BY created_at DESC LIMIT 1''',(task_id,day,task['updated_at'])).fetchone()
                if previous: return {'run_id':previous['run_id'],'reused':True}
            if db.execute("SELECT COUNT(*) FROM local_trials WHERE phase IN ('queued','condition','report')").fetchone()[0]>=2:
                raise HTTPException(429,'已有两次试运行在处理，请等待完成。')
            db.execute('INSERT INTO local_trials VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (key,str(request.request_key),request.retry_of,task_id,day,task['updated_at'],'queued',json.dumps(task,ensure_ascii=False),'{}','',now()))
            db.execute('''INSERT INTO task_runs (run_id,task_id,task_name,source,scheduled_slot,dispatch_date,
                status,trigger_reason,report_text,error_code,notification_status,started_at,finished_at,artifacts_json,config_json)
                VALUES (?,?,?,?,?,?,'RUNNING','','','','NOT_QUEUED',?,'','[]',?)''',
                (key,task_id,task['name'],'manual_local','manual-ui:'+day,date.today().isoformat(),now(),json.dumps(task,ensure_ascii=False)))
        try:
            self.pool.submit(copy_context().run,self.work,key)
        except Exception:
            with self.db() as db:
                db.execute("UPDATE local_trials SET phase='failed',error='本地工作线程未能接收任务；未调用模型。' WHERE run_id=?",(key,))
                db.execute("UPDATE task_runs SET status='FAILED',error_code='WorkerUnavailable',finished_at=? WHERE run_id=?",(now(),key))
            raise HTTPException(503,'试运行未能启动；记录已保留，可明确重试。') from None
        return {'run_id':key,'reused':False}

    def work(self, key):
        row=self.get(key); task=json.loads(row['config_json']); timings={}; started=time.monotonic()
        phase='condition'
        try:
            with record_action('local_ui_task_trial'):
                with self.db() as db: db.execute("UPDATE local_trials SET phase='condition' WHERE run_id=?",(key,))
                stamp=time.monotonic()
                result,variables=self.condition(task,row['business_date'])
                if type(result.get('should_trigger')) is not bool:
                    raise ValueError('触发条件必须是严格布尔值；无法判断时不会生成报告。')
                timings['condition_seconds']=round(time.monotonic()-stamp,3)
                task['_execution_context']=variables
                with self.db() as db:
                    db.execute('UPDATE task_runs SET trigger_reason=? WHERE run_id=?',(result['reason'],key))
                    db.execute('UPDATE local_trials SET config_json=?,timings_json=? WHERE run_id=?',
                        (json.dumps(task,ensure_ascii=False),json.dumps(timings),key))
                if result['should_trigger']:
                    phase='report'
                    with self.db() as db: db.execute("UPDATE local_trials SET phase='report' WHERE run_id=?",(key,))
                    stamp=time.monotonic()
                    with span('manual_ui_report',kind='report',provider=task['report']['agent'],run_id=key):
                        report=self.report(task,variables,result['reason'])
                    timings['report_seconds']=round(time.monotonic()-stamp,3)
                    if not report.strip() or len(report)>12000: raise ValueError('报告为空或超过本地 12000 字符上限，请缩小任务范围。')
                    status='REPORT_CREATED'
                else: report='';status='NOT_TRIGGERED'
            timings['total_seconds']=round(time.monotonic()-started,3)
            with self.db() as db:
                db.execute('UPDATE task_runs SET status=?,report_text=?,finished_at=? WHERE run_id=?',(status,report,now(),key))
                db.execute("UPDATE local_trials SET phase='completed',timings_json=? WHERE run_id=?",(json.dumps(timings),key))
        except Exception as exc:
            timings['total_seconds']=round(time.monotonic()-started,3)
            detail=str(exc)[:1500] if isinstance(exc,ValueError) else type(exc).__name__
            with self.db() as db:
                db.execute("UPDATE task_runs SET status='FAILED',error_code=?,finished_at=? WHERE run_id=?",(type(exc).__name__,now(),key))
                db.execute("UPDATE local_trials SET phase='failed',error=?,timings_json=? WHERE run_id=?",(phase+'：'+detail,json.dumps(timings),key))

    def preview(self, key):
        row=self.get(key)
        if row['status']!='REPORT_CREATED': raise HTTPException(409,'报告尚未生成，不能预览邮件。')
        if not self.recipient or not self.sender: raise HTTPException(503,'本地发件人与测试收件人尚未配置。')
        folder=self.artifacts/key;folder.mkdir(mode=0o700,exist_ok=True)
        path=folder/'message.eml'
        # Raw bytes are stable across preview/send; MIME boundaries must not change.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not path.exists():
                raw=self.preview_message(row,json.loads(row['config_json']))
                temp=folder/(uuid4().hex+'.tmp')
                with temp.open('xb') as stream: stream.write(raw)
                temp.chmod(0o600);temp.replace(path)
        raw=path.read_bytes(); message=BytesParser(policy=policy.default).parsebytes(raw)
        if str(message['From'])!=self.sender or str(message['To'])!=self.recipient or message.get('Cc') or message.get('Bcc'):
            raise HTTPException(409,'预览收件人与当前本地测试配置不一致。')
        return {'run_id':key,'sender':self.sender,'recipient':self.recipient,'subject':str(message['Subject']),
            'body':row['report_text'],'preview_hash':hashlib.sha256(raw).hexdigest(),
            'can_send':self.send_message is not None and row['notification_status']=='NOT_QUEUED',
            'notification_status':row['notification_status'],'timings':json.loads(row['timings_json'])}

    def send(self, key, request):
        from report_email import EmailRejected
        if not request.confirm: raise HTTPException(400,'请明确确认发送这一封邮件。')
        preview=self.preview(key)
        if preview['preview_hash']!=request.preview_hash: raise HTTPException(409,'邮件预览已改变，请重新预览。')
        if not preview['can_send']: raise HTTPException(409,'此邮件已尝试发送或通道未配置，不会自动重发。')
        with self.db() as db:
            changed=db.execute("UPDATE task_runs SET notification_status='EMAIL_SENDING' WHERE run_id=? AND notification_status='NOT_QUEUED'",(key,)).rowcount
            if changed!=1: raise HTTPException(409,'邮件已被认领，不会重复发送。')
        started=time.monotonic()
        try:
            with span('manual_ui_email',kind='mail_adapter',provider='gmail',run_id=key):
                message_id=self.send_message((self.artifacts/key/'message.eml').read_bytes())
            status,error='EMAIL_ACCEPTED',''
        except EmailRejected:
            status,error,message_id='EMAIL_FAILED','EmailRejected',''
        except Exception:
            status,error,message_id='EMAIL_UNKNOWN','ProviderOutcomeUnknown',''
        with self.db() as db:
            row=db.execute('SELECT timings_json FROM local_trials WHERE run_id=?',(key,)).fetchone()
            timings=json.loads(row[0]);timings['email_seconds']=round(time.monotonic()-started,3)
            db.execute('UPDATE local_trials SET timings_json=? WHERE run_id=?',(json.dumps(timings),key))
            db.execute('UPDATE task_runs SET notification_status=?,provider_message_id=?,notification_error_code=? WHERE run_id=?',(status,message_id,error,key))
        return {'notification_status':status,'provider_message_id':message_id,'error':error}


def register_routes(app, service, *, legacy_only=False):
    @app.get('/api/local-trials/settings')
    def settings():
        return {'enabled':True,'sender':service.sender,'recipient':service.recipient,
                'mail_available':bool(service.send_message),'identity_mode':'single_user_local'}

    if not legacy_only:
        @app.post('/api/tasks/{task_id}/trial',status_code=202)
        def start(task_id: str, request: TrialRequest): return service.start(task_id,request)

    @app.get('/api/local-trials/{run_id}')
    def status(run_id: str): return service.status(run_id)

    @app.get('/api/local-trials/{run_id}/email-preview')
    def preview(run_id: str): return service.preview(run_id)

    @app.post('/api/local-trials/{run_id}/send')
    def send(run_id: str, request: SendRequest): return service.send(run_id,request)

    # The launcher imports an App whose frontend catch-all already exists.
    # Keep these explicit API routes ahead of that catch-all mount.
    mounts=[route for route in app.router.routes if isinstance(route,Mount) and route.path in ('','/')]
    app.router.routes[:]=[route for route in app.router.routes if route not in mounts]+mounts

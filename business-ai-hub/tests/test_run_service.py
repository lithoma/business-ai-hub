"""Focused offline execution tests: real SQLite, fixture business/mail adapters."""
from datetime import datetime
from contextlib import closing
from email.message import EmailMessage
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from hub_auth import Identity, _context, install_auth
ENGINE=Path(__file__).resolve().parents[2]/'business-task-manager-v1/business-task-manager-v1'
sys.path.insert(0,str(ENGINE))
from local_trials import SendRequest
from run_service import RunRepository, RunService
from run_routes import register_run_routes
from schedule_core import plan
from task_execution import readiness


class RunTests(unittest.TestCase):
    def setUp(self):
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.database=Path(folder.name)/'runs.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('''CREATE TABLE task_runs (run_id TEXT PRIMARY KEY,task_id TEXT,task_name TEXT,
                source TEXT,scheduled_slot TEXT,dispatch_date TEXT,status TEXT,trigger_reason TEXT,
                report_text TEXT,error_code TEXT,notification_status TEXT,started_at TEXT,finished_at TEXT,
                artifacts_json TEXT,provider_message_id TEXT,notification_error_code TEXT,config_json TEXT)''')
            db.execute('CREATE UNIQUE INDEX run_slot ON task_runs(source,task_id,scheduled_slot)')
        self.identity=Identity('fixture','https://fixture.example','fixture@example.com','local_cli',SimpleNamespace())
        marker=_context.set(self.identity);self.addCleanup(_context.reset,marker)
        self.task={'task_id':'fixture','name':'离线任务','updated_at':'v1','_source':'registry',
            'enabled':True,'lifecycle_status':'active',
            'schedule':{'frequency':'daily','time':'14:35','timezone':'Asia/Shanghai'},
            'trigger':{'type':'always'},'report':{'prompt':'离线报告','agent':'genie_one','security':self.identity.security}}
        self.condition=Mock(return_value=({'should_trigger':True,'reason':'fixture'},{'business_date':'2026-10-21'}))
        self.report=Mock(return_value='fixture report');self.send=Mock(return_value='fixture-receipt')
        def preview(row,snapshot):
            message=EmailMessage();message['From']='sender@example.com';message['To']='recipient@example.com'
            message['Subject']='fixture';message.set_content(row['report_text']);return message.as_bytes()
        def query(sql,params=None):
            def field(raw,path):
                value=json.loads(raw or '{}')
                for part in path.removeprefix('$.').split('.'):value=value.get(part,{})
                return value if isinstance(value,(str,int,float)) else json.dumps(value)
            with closing(sqlite3.connect(self.database,timeout=5)) as db, db:
                db.row_factory=sqlite3.Row;db.create_function('get_json_object',2,field)
                cursor=db.execute(sql.replace('workspace.douyin.runs','task_runs'),params or {})
                return [dict(row) for row in cursor.fetchall()] if cursor.description else [{'num_affected_rows':cursor.rowcount}]
        self.repo=RunRepository(query,'workspace.douyin.runs',sqlite=True)
        self.service=RunService(self.repo,lambda:[self.task],self.condition,self.report,preview,self.send,
            sender='sender@example.com',recipient='recipient@example.com')
        self.service.pool.shutdown(wait=True)
        self.service.pool=SimpleNamespace(submit=lambda fn,*args:fn(*args))
        self.app=FastAPI();install_auth(self.app)
        self.app.state.auth.mode='local_cli';self.app.state.auth.resolve=lambda request:self.identity
        self.app.state.run_service=self.service
        register_run_routes(self.app,query,lambda:[self.task],lambda:{},ENGINE,'fixture-warehouse')
        self.client=TestClient(self.app)

    def start(self,**extra):
        return self.client.post('/api/tasks/fixture/trial',json={'business_date':'2026-10-21',
            'expected_updated_at':'v1','request_key':str(uuid4()),'confirm':True,**extra})

    def test_canonical_api_deduplicates_and_only_sends_reviewed_bytes_once(self):
        self.assertEqual(self.start(confirm=False).status_code,400)
        self.assertEqual(self.start(expected_updated_at='old').status_code,409)
        result=self.start();self.assertEqual(result.status_code,202,result.text)
        key=result.json()['run_id'];self.assertEqual(self.start().json()['run_id'],key)
        self.assertEqual(self.client.get('/api/runs/'+key).json()['status'],'REPORT_CREATED')
        self.condition.assert_called_once();self.report.assert_called_once();self.send.assert_not_called()
        preview=self.client.get('/api/runs/'+key+'/email-preview').json()
        body={'confirm':True,'preview_hash':preview['preview_hash']}
        self.assertEqual(self.client.post('/api/runs/'+key+'/send',json={**body,'preview_hash':'wrong'}).status_code,409)
        self.assertEqual(self.client.post('/api/runs/'+key+'/send',json=body).json()['notification_status'],'EMAIL_ACCEPTED')
        self.assertEqual(self.client.post('/api/runs/'+key+'/send',json=body).status_code,409)
        self.send.assert_called_once()

    def test_unknown_and_false_conditions_explicit_retry_do_not_report_or_send(self):
        self.condition.side_effect=[({'should_trigger':None,'reason':'unknown'},{}),({'should_trigger':False,'reason':'not met'}, {})]
        key=self.start().json()['run_id'];self.assertEqual(self.service.status(key)['status'],'FAILED')
        self.assertEqual(self.start().json()['run_id'],key)
        retry=self.start(retry_of=key).json()['run_id'];self.assertNotEqual(key,retry)
        self.assertEqual(self.service.status(retry)['status'],'NOT_TRIGGERED')
        self.assertEqual(self.start(retry_of=key).json()['run_id'],retry)
        self.assertEqual(self.client.get('/api/runs/'+retry+'/email-preview').status_code,409)
        self.assertEqual(self.condition.call_count,2);self.report.assert_not_called();self.send.assert_not_called()

    def test_unknown_email_and_interrupted_restart_cannot_replay(self):
        self.send.side_effect=TimeoutError('offline fixture')
        key=self.start().json()['run_id'];preview=self.service.preview(key)
        req=SendRequest(confirm=True,preview_hash=preview['preview_hash'])
        self.assertEqual(self.service.send(key,req)['notification_status'],'EMAIL_UNKNOWN')
        with self.assertRaises(HTTPException):self.service.send(key,req)
        self.send.assert_called_once()
        self.repo.update(key,{'status':'RUNNING','notification_status':'EMAIL_SENDING'})
        self.service.instance='restarted-fixture';self.service.recovered.clear();self.service.recover_interrupted()
        state=self.service.status(key)
        self.assertEqual((state['status'],state['notification_status']),('FAILED','EMAIL_UNKNOWN'))
        self.assertEqual(self.condition.call_count,1)

    def test_dispatch_gate_same_slot_once_and_no_automatic_mail(self):
        now=datetime.fromisoformat('2026-10-22T14:36:00+08:00');resolver=Mock(return_value=self.identity)
        with self.assertRaises(RuntimeError):self.service.dispatch_once(now,resolver)
        resolver.assert_not_called();self.condition.assert_not_called()
        with patch('run_service.verified_identity',return_value=self.identity) as verify:
            first=self.service.dispatch_once(now,resolver,enabled=True)['runs'][0]
            second=self.service.dispatch_once(now,resolver,enabled=True)['runs'][0]
        self.assertEqual(first['run_id'],second['run_id']);self.assertTrue(second['reused'])
        self.assertEqual(self.service.status(first['run_id'])['status'],'REPORT_CREATED')
        self.assertEqual(self.condition.call_args.args[1],'2026-10-21')
        verify.assert_called_once();self.condition.assert_called_once();self.report.assert_called_once();self.send.assert_not_called()
        self.assertTrue(self.service.schedule_plan(now)['tasks'][0]['already_claimed'])

    def test_revoked_identity_has_failed_ledger_and_owner_isolation(self):
        with patch('run_service.verified_identity',side_effect=PermissionError('fixture revoked')):
            result=self.service.dispatch_once(datetime.fromisoformat('2026-10-22T14:36:00+08:00'),lambda task:self.identity,enabled=True)['runs'][0]
        self.assertEqual(self.repo.get(result['run_id'])['error_code'],'ExecutionIdentityUnavailable')
        self.condition.assert_not_called();self.report.assert_not_called();self.send.assert_not_called()
        marker=_context.set(Identity('other','https://fixture.example','other@example.com','fixture',None))
        try:
            with self.assertRaises(HTTPException):self.service.status(result['run_id'])
        finally:_context.reset(marker)

    def test_disabled_api_has_no_storage_or_sdk_access(self):
        del self.app.state.run_service
        with patch.dict(os.environ,{'HUB_MANUAL_RUNS_ENABLED':'false'}),patch('run_routes.create_service') as factory:
            self.assertFalse(self.client.get('/api/runs/settings').json()['enabled'])
            self.assertEqual(self.start().status_code,503);factory.assert_not_called()

    def test_obo_manual_run_keeps_creator_and_rechecks_before_report(self):
        user=Identity(self.identity.subject,self.identity.workspace,'Creator','apps_obo',object())
        self.identity=user;self.task['report']['security']=user.security
        other=Identity('other',user.workspace,'Other','apps_obo',object())
        with patch('task_authorization.verified_identity',side_effect=[user,other]):
            key=self.start().json()['run_id']
        self.assertEqual(self.service.status(key)['status'],'FAILED')
        self.condition.assert_called_once();self.report.assert_not_called();self.send.assert_not_called()

    def test_obo_scheduled_run_resolves_creator_again_in_worker(self):
        user=Identity(self.identity.subject,self.identity.workspace,'Creator','apps_obo',object())
        self.identity=user;self.task['report']['security']=user.security
        resolver=Mock(return_value=user);self.service.creator_resolver=resolver
        marker=_context.set(user)
        try:
            with patch('run_service.verified_identity',return_value=user), \
                 patch('task_authorization.verified_identity',return_value=user):
                result=self.service.dispatch_once(datetime.fromisoformat('2026-10-22T14:36:00+08:00'),resolver,enabled=True)['runs'][0]
            self.assertEqual(self.service.status(result['run_id'])['status'],'REPORT_CREATED')
        finally:_context.reset(marker)
        # Dispatch, before condition, before report: same creator all three times.
        self.assertEqual(resolver.call_count,3)
        self.condition.assert_called_once();self.report.assert_called_once();self.send.assert_not_called()


class CalendarTests(unittest.TestCase):
    def test_times_weekly_interval_dst_and_activation(self):
        now=datetime.fromisoformat('2026-10-22T14:36:00+08:00')
        daily={'frequency':'daily','time':'14:35','timezone':'Asia/Shanghai'}
        self.assertTrue(plan(daily,now)['due'])
        self.assertEqual(plan(daily,now)['next_run_at'],'2026-10-23T14:35:00+08:00')
        weekly={**daily,'frequency':'weekly','weekday':'friday'}
        self.assertFalse(plan(weekly,now)['due'])
        self.assertTrue(plan(weekly,datetime.fromisoformat('2026-10-23T14:35:00+08:00'))['due'])
        interval={**daily,'frequency':'interval','start_date':'2026-10-21','interval_days':3}
        self.assertEqual(plan(interval,now)['next_run_at'],'2026-10-24T14:35:00+08:00')
        dst={'frequency':'daily','time':'01:30','timezone':'America/New_York'}
        self.assertEqual(plan(dst,datetime.fromisoformat('2026-11-01T05:31:00+00:00'))['slot'],plan(dst,datetime.fromisoformat('2026-11-01T06:31:00+00:00'))['slot'])
        self.assertTrue(plan({**dst,'time':'02:30'},datetime.fromisoformat('2026-03-08T07:00:00+00:00'))['due'])
        task={'schedule':daily,'trigger':{'type':'always'},'report':{'agent':'genie_one','prompt':'fixture'}}
        with patch.dict(os.environ,{'TASK_EXECUTION_ENABLED':'true','TASK_SCHEDULER_MODE':'per_task'}):self.assertTrue(readiness(task)['can_activate'])


if __name__=='__main__':unittest.main()

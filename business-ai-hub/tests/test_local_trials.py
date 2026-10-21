"""Focused offline state-machine checks. All remote/report/mail adapters are fixtures."""
from email.message import EmailMessage
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles
from local_trials import LocalTrials, register_routes
from hub_auth import Identity, _context, install_auth
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'business-task-manager-v1/business-task-manager-v1'))


class LocalTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.database=root/'dev.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.execute('''CREATE TABLE task_runs (run_id TEXT PRIMARY KEY,task_id TEXT,task_name TEXT,
                source TEXT,scheduled_slot TEXT,dispatch_date TEXT,status TEXT,trigger_reason TEXT,
                report_text TEXT,error_code TEXT,notification_status TEXT,started_at TEXT,finished_at TEXT,
                artifacts_json TEXT,provider_message_id TEXT,notification_error_code TEXT,config_json TEXT)''')
        db.close()
        self.task={'task_id':'fixture','name':'离线测试','updated_at':'v1','_source':'registry',
            'trigger':{'type':'always'},'report':{'prompt':'fixture report','agent':'genie_one'}}
        identity=Identity('fixture','https://fixture.example','fixture@example.com','local_cli',SimpleNamespace())
        self.task['report']['security']=identity.security
        marker=_context.set(identity);self.addCleanup(_context.reset,marker)
        self.condition=Mock(return_value=({'should_trigger':True,'reason':'fixture'}, {'business_date':'2025-06-24'}))
        self.report=Mock(return_value='fixture report')
        self.send=Mock(return_value='fixture-mail-id')
        def preview(row,snapshot):
            message=EmailMessage();message['From']='sender@example.com';message['To']='recipient@example.com'
            message['Subject']='fixture';message.set_content(row['report_text']);return message.as_bytes()
        self.service=LocalTrials(self.database,root/'artifacts',lambda:[self.task],self.condition,
            self.report,preview,'sender@example.com','recipient@example.com',self.send)
        self.service.pool.shutdown(wait=True)
        self.service.pool=SimpleNamespace(submit=lambda function,*args:function(*args))
        app=FastAPI();install_auth(app);app.state.auth.mode='local_cli';app.state.auth.resolve=lambda request: identity
        app.mount('/',StaticFiles(directory=root,html=True))
        register_routes(app,self.service);self.client=TestClient(app)

    def start(self,**extra):
        return self.client.post('/api/tasks/fixture/trial',json={'business_date':'2025-06-24',
            'expected_updated_at':'v1','request_key':str(uuid4()),'confirm':True,**extra})

    def test_real_local_ledger_deduplicates_trials_and_requires_preview_then_one_explicit_send(self):
        self.assertEqual(self.start(confirm=False).status_code,400)
        self.assertEqual(self.start(expected_updated_at='old').status_code,409)
        result=self.start();self.assertEqual(result.status_code,202,result.text)
        key=result.json()['run_id']
        self.assertEqual(self.start().json()['run_id'],key)
        self.condition.assert_called_once();self.report.assert_called_once();self.send.assert_not_called()
        state=self.client.get('/api/local-trials/'+key).json()
        self.assertEqual(state['status'],'REPORT_CREATED')
        preview=self.client.get('/api/local-trials/'+key+'/email-preview').json()
        self.assertTrue(preview['can_send'])
        self.assertEqual(preview['recipient'],'recipient@example.com')
        self.assertEqual(self.client.post('/api/local-trials/'+key+'/send',json={'preview_hash':'changed','confirm':True}).status_code,409)
        body={'preview_hash':preview['preview_hash'],'confirm':True}
        accepted=self.client.post('/api/local-trials/'+key+'/send',json=body)
        self.assertEqual(accepted.json()['notification_status'],'EMAIL_ACCEPTED')
        self.assertEqual(self.client.post('/api/local-trials/'+key+'/send',json=body).status_code,409)
        self.send.assert_called_once()

    def test_false_condition_skips_report_and_failed_condition_only_retries_explicitly(self):
        self.condition.side_effect=[ValueError('fixture: missing data'),
            ({'should_trigger':False,'reason':'fixture: not met'},{'business_date':'2025-06-24'})]
        key=self.start().json()['run_id']
        self.assertEqual(self.service.status(key)['phase'],'failed')
        self.assertEqual(self.start().json()['run_id'],key)
        self.condition.assert_called_once()
        retry=self.start(retry_of=key).json()['run_id']
        self.assertNotEqual(retry,key)
        self.assertEqual(self.service.status(retry)['status'],'NOT_TRIGGERED')
        self.assertEqual(self.start(retry_of=key).json()['run_id'],retry)
        self.assertEqual(self.condition.call_count,2)
        self.report.assert_not_called();self.send.assert_not_called()
        self.assertEqual(self.client.get('/api/local-trials/'+retry+'/email-preview').status_code,409)

    def test_unknown_mail_outcome_is_persisted_and_never_automatically_replayed(self):
        self.send.side_effect=TimeoutError('fixture transport timeout')
        key=self.start().json()['run_id']
        preview=self.client.get('/api/local-trials/'+key+'/email-preview').json()
        body={'preview_hash':preview['preview_hash'],'confirm':True}
        self.assertEqual(self.client.post('/api/local-trials/'+key+'/send',json=body).json()['notification_status'],'EMAIL_UNKNOWN')
        self.assertFalse(self.client.get('/api/local-trials/'+key+'/email-preview').json()['can_send'])
        self.assertEqual(self.client.post('/api/local-trials/'+key+'/send',json=body).status_code,409)
        self.send.assert_called_once()


if __name__=='__main__': unittest.main()

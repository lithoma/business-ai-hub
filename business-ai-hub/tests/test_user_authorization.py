"""Focused offline access-boundary tests, using real SQLite and mocked identities only."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from hub_auth import Identity, AuthManager, _context
from conversation_workflow import AnalysisTickets
from condition_sql import sign_test, verified_test, extract_trigger_sql
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]


class AuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('auth_local_dev',ROOT/'scripts/dev-local.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        cls.temp=TemporaryDirectory(); cls.database=Path(cls.temp.name)/'dev.sqlite3'
        cls.app=module.build_app(cls.database)
        cls.users={key:Identity(key,'https://company.example',key+'@example.com','local_cli',SimpleNamespace()) for key in ('alice','bob')}
        cls.app.state.auth.resolve=lambda request: cls.users[request.headers.get('x-test-user','alice')]
        cls.client=TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
        os.environ.pop('HUB_TEST_METRICS_DB',None)

    def test_task_crud_identity_is_server_controlled_and_cross_user_ids_are_hidden(self):
        payload={'task_id':'ownership','name':'fixture','schedule':{'frequency':'daily'},'trigger':{'type':'always'},
                 'report':{'agent':'genie_one','prompt':'fixture','security':self.users['bob'].security},'owner':'bob@example.com'}
        response=self.client.post('/api/tasks/create',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        task=self.client.get('/api/tasks').json()['tasks'][0]
        self.assertEqual(task['owner'],'alice@example.com')
        self.assertEqual(task['report']['security'],self.users['alice'].security)
        for path,method,body in [('/api/tasks/ownership/readiness','get',None),('/api/tasks/ownership','put',{'name':'steal'}),
                                 ('/api/tasks/ownership','delete',{'confirm':True}),
                                 ('/api/tasks/ownership/lifecycle','post',{'action':'activate','confirm':True,'reviewed':True,'expected_updated_at':task['updated_at']})]:
            response=self.client.request(method,path,headers={'x-test-user':'bob'},**({'json':body} if body else {}))
            self.assertEqual(response.status_code,404,response.text)
        self.assertEqual(self.client.get('/api/tasks',headers={'x-test-user':'bob'}).json()['tasks'],[])
        response=self.client.put('/api/tasks/ownership',json={'report':{**task['report'],'security':self.users['bob'].security},'owner':'bob'})
        self.assertEqual(response.status_code,200,response.text)
        saved=self.client.get('/api/tasks').json()['tasks'][0]
        self.assertEqual(saved['report']['security'],self.users['alice'].security)

    def test_report_ownership_filters_before_pagination_and_legacy_unowned_rows_hidden(self):
        with sqlite3.connect(self.database) as db:
            for user,stamp in [('alice','1'),('bob','9'),('legacy','8')]:
                config={'report':{'security':self.users[user].security}} if user!='legacy' else {}
                db.execute('INSERT INTO task_runs (run_id,task_id,status,started_at,config_json) VALUES (?,?,?,?,?)',
                           ('report-'+user,'fixture','REPORT_CREATED',stamp,json.dumps(config)))
        result=self.client.get('/api/reports?limit=1').json()
        self.assertEqual([r['run_id'] for r in result['runs']],['report-alice'])
        self.assertIsNone(result['next_offset'])
        self.assertEqual(self.client.get('/api/reports?task_id=fixture',headers={'x-test-user':'bob'}).json()['runs'][0]['run_id'],'report-bob')

    def test_tokens_and_receipts_cannot_be_replayed_by_another_user(self):
        tickets=AnalysisTickets();ticket=tickets.put('fixture','genie_one',subject='alice')
        with self.assertRaises(ValueError): tickets.take(ticket,subject='bob')
        self.assertEqual(tickets.take(ticket,subject='alice'),('fixture','genie_one'))
        with self.assertRaises(ValueError): tickets.take(ticket,subject='alice')
        marker=_context.set(self.users['alice'])
        try: receipt=sign_test('SELECT true AS should_trigger','day')
        finally: _context.reset(marker)
        review={'window':'day','test':{'receipt':receipt}}
        for user,expected in [('alice',True),('bob',False)]:
            marker=_context.set(self.users[user])
            try: self.assertEqual(verified_test('SELECT true AS should_trigger',review),expected)
            finally: _context.reset(marker)

    def test_oauth_requires_real_session_ignores_forwarded_identity_and_checks_origin(self):
        original=self.app.state.auth
        manager=AuthManager();manager.mode='oauth';manager.origin='https://hub.example'
        self.app.state.auth=manager
        try:
            self.assertEqual(self.client.get('/api/tasks',headers={'x-forwarded-access-token':'spoof','x-forwarded-email':'alice@example.com'}).status_code,401)
            import time
            manager.sessions['fixture']=(time.monotonic()+60,self.users['alice'])
            self.client.cookies.set('__Host-hub-session','fixture')
            self.assertEqual(self.client.post('/api/tasks/create',json={},headers={'Origin':'https://evil.example'}).status_code,403)
            self.assertEqual(self.client.post('/api/tasks/create',json={}).status_code,403)
        finally:
            self.client.cookies.clear();self.app.state.auth=original

    def test_background_resolver_cannot_run_as_someone_else_and_false_skips_genie(self):
        from creator_execution import CreatorRunner
        client=SimpleNamespace(config=SimpleNamespace(host='https://company.example',client_id=None),
            current_user=SimpleNamespace(me=Mock(return_value=SimpleNamespace(id='alice',active=True,user_name='alice@example.com'))))
        alice=Identity('alice','https://company.example','alice@example.com','oauth_user',client)
        task={'task_id':'fixture','_source':'registry','trigger':{'type':'sql','sql':'SELECT false AS should_trigger'},
              'report':{'security':alice.security,'agent':'genie_one','prompt':'report'}}
        for identity in (None,self.users['bob']):
            with self.assertRaises(PermissionError): CreatorRunner(lambda *_:identity,'warehouse')(task,'.',{'business_date':'2025-06-24'})
        with patch('creator_execution.execute_readonly',return_value={'should_trigger':False,'reason':'below threshold'}) as sql, \
             patch('genie_one_mcp.wait_for_answer') as genie:
            result=CreatorRunner(lambda *_:alice,'warehouse')(task,'.',{'business_date':'2025-06-24'})
            self.assertEqual(result,(False,'below threshold',''))
            sql.assert_called_once();genie.assert_not_called()
        self.assertIsNone(_context.get())

    def test_genie_sql_uses_structured_executed_query_not_rendered_text(self):
        sql,item=extract_trigger_sql([{'item_id':'explore','sql':'SELECT count(*) FROM t'},
            {'item_id':'condition','sql':'SELECT true AS should_trigger'}])
        self.assertEqual(item,'condition');self.assertIn('should_trigger',sql)
        for items in [[],[{'item_id':'x','sql':'DELETE FROM t'}],
                      [{'item_id':'a','sql':'SELECT true AS should_trigger'},{'item_id':'b','sql':'SELECT false AS should_trigger'}]]:
            with self.assertRaises(ValueError): extract_trigger_sql(items)

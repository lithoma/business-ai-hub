"""Native Jobs boundary checks, entirely offline; no model or email calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from creator_job_client import JobConfig,JobClient,job_settings,run_job
from hub_auth import Identity


def config():
    return JobConfig('https://company.example','https://hub.databricksapps.com','app-fixture',
        'alice','alice@example.com','/Workspace/Shared/hub','/Workspace/Shared/hub/config/alice.json',
        '/Workspace/Shared/hub/scripts/creator-job-notebook','cluster-fixture')


class JobClientTests(unittest.TestCase):
    def test_approved_recurring_task_outlives_review_receipt_freshness(self):
        from datetime import datetime,timezone
        from hub_auth import _context
        from condition_sql import sign_test
        from task_execution import readiness
        from run_routes import install_engine_path
        install_engine_path(Path(__file__).resolve().parents[2]/'business-task-manager-v1/business-task-manager-v1')
        user=Identity('alice','https://company.example','Alice','apps_obo',object())
        marker=_context.set(user)
        try:
            sql='SELECT true AS should_trigger'
            with patch('condition_sql.datetime') as old_clock:
                old_clock.now.return_value=datetime(2020,1,1,tzinfo=timezone.utc)
                receipt=sign_test(sql,'day')
            task={'enabled':True,'lifecycle_status':'active',
                'schedule':{'frequency':'daily','time':'09:00','timezone':'Asia/Shanghai'},
                'trigger':{'type':'sql','sql':sql},'report':{'agent':'genie_one','prompt':'report',
                    'security':user.security,'condition_sql':{'window':'day','test':{'receipt':receipt}}}}
            self.assertTrue(readiness(task)['errors']) # Cannot activate using a stale test.
            self.assertFalse(readiness(task,for_execution=True)['errors']) # Already approved unchanged task continues.
            task['trigger']['sql']='SELECT false AS should_trigger'
            self.assertTrue(readiness(task,for_execution=True)['errors'])
            task['trigger']['sql']=sql;task['lifecycle_status']='draft'
            self.assertTrue(readiness(task,for_execution=True)['errors'])
        finally:_context.reset(marker)

    def test_template_paused_and_offline_execution_has_no_browser_dependency(self):
        cfg=config();payload=job_settings(cfg)
        self.assertEqual(payload['run_as'],{'user_name':'alice@example.com'})
        self.assertEqual(payload['max_concurrent_runs'],1)
        self.assertEqual(payload['schedule']['pause_status'],'PAUSED')
        self.assertEqual(payload['tasks'][0]['max_retries'],0)
        self.assertNotIn('new_cluster',payload['tasks'][0])
        runtime=SimpleNamespace(config=SimpleNamespace(host=cfg.workspace_host,auth_type='runtime'),
            current_user=SimpleNamespace(me=Mock(return_value=SimpleNamespace(id='alice',user_name='alice@example.com',active=True))))
        keys=['a'*64,'b'*64,'c'*64]
        transport=Mock()
        transport.request.side_effect=[
            {'creator_subject':'alice','runs':[{'run_id':keys[0]},{'run_id':keys[1]},
                {'task_id':'third','status':'DEFERRED','claimed':False}]},
            {'run_id':keys[0],'status':'NOT_TRIGGERED'}, {'run_id':keys[1],'status':'REPORT_CREATED'},
            {'creator_subject':'alice','runs':[{'run_id':key} for key in keys]},
            {'run_id':keys[2],'status':'REPORT_CREATED'}]
        clock=[0]
        result=run_job(cfg,runtime,lambda:'unused',73,901,transport=transport,
            clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual([item['status'] for item in result['runs']],['NOT_TRIGGERED','REPORT_CREATED','REPORT_CREATED'])
        self.assertFalse(result['browser_session_required']);self.assertFalse(result['automatic_email'])
        self.assertEqual(result['state'],'completed')
        runtime.current_user.me.return_value.id='bob'
        transport.reset_mock()
        with self.assertRaises(PermissionError):run_job(cfg,runtime,lambda:'unused',73,902,transport=transport)
        transport.request.assert_not_called()

    def test_audience_token_refresh_redirect_rejection_and_no_replayed_post(self):
        cfg=config();session=Mock();clock=[0]
        session.post.return_value=SimpleNamespace(status_code=200,json=lambda:{'access_token':'fixture-access','expires_in':60})
        session.request.return_value=SimpleNamespace(status_code=200,json=lambda:{'ok':True})
        client=JobClient(cfg,lambda:'fixture-notebook',session=session,clock=lambda:clock[0])
        client.request('POST','/api/creator-jobs/dispatch',{})
        client.request('GET','/api/creator-jobs/runs/'+'a'*64)
        self.assertEqual(session.post.call_count,1)
        self.assertEqual(session.post.call_args.kwargs['data']['audience'],cfg.app_client_id)
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
        self.assertNotIn('fixture-access',repr(client))
        clock[0]=31;client.request('GET','/api/creator-jobs/runs/'+'a'*64)
        self.assertEqual(session.post.call_count,2)
        session.request.return_value=SimpleNamespace(status_code=302)
        with self.assertRaises(RuntimeError):client.request('POST','/api/creator-jobs/dispatch',{})
        session.request.reset_mock();session.request.side_effect=TimeoutError('fixture unknown outcome')
        with self.assertRaises(TimeoutError):client.request('POST','/api/creator-jobs/dispatch',{})
        session.request.assert_called_once()


class AppJobTests(unittest.TestCase):
    def setUp(self):
        # Reuse the canonical SQL-ledger fixture without collecting its test suite.
        sys.path.insert(0,str(Path(__file__).parent))
        import test_run_service as fixtures
        self.fixture=fixtures.RunTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        binding=Path(folder.name)/'bindings.json'
        binding.write_text(json.dumps({'version':1,'bindings':[{
            'workspace':'https://fixture.example','creator_subject':'fixture','job_id':73,'enabled':True}]}))
        self.env=patch.dict(os.environ,{'HUB_CREATOR_JOBS_ENABLED':'true',
            'HUB_CREATOR_JOB_BINDINGS_FILE':str(binding),'HUB_MANUAL_RUNS_ENABLED':'false','TASK_EXECUTION_ENABLED':'true'})
        self.env.start();self.addCleanup(self.env.stop)
        self.fixture.identity=Identity('fixture','https://fixture.example','Alice','apps_obo',object())
        self.fixture.task['report']['security']=self.fixture.identity.security
        self.fixture.task['schedule']['time']='00:00'
        self.fixture.app.state.auth.mode='databricks_app_obo'
        self.fixture.app.state.auth.origin='https://hub.example'
        from task_authorization import resolve_for_task
        self.fixture.service.creator_resolver=lambda task:resolve_for_task(self.fixture.app.state.auth,task)
        self.patch=patch('task_authorization.verified_identity',return_value=self.fixture.identity)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.patch2=patch('run_service.verified_identity',return_value=self.fixture.identity)
        self.patch2.start();self.addCleanup(self.patch2.stop)
        self.headers={'origin':'https://hub.example'}
        self.body={'creator_subject':'fixture','job_id':73,'job_run_id':901}

    def test_native_tick_is_creator_bound_idempotent_and_reuses_existing_pipeline(self):
        web=self.fixture.client
        response=web.post('/api/creator-jobs/dispatch',headers=self.headers,json=self.body)
        self.assertEqual(response.status_code,200,response.text)
        key=response.json()['runs'][0]['run_id']
        status=web.get('/api/creator-jobs/runs/'+key).json()
        self.assertEqual(status['status'],'REPORT_CREATED');self.assertNotIn('report_text',status)
        replay=web.post('/api/creator-jobs/dispatch',headers=self.headers,json={**self.body,'job_run_id':902})
        self.assertTrue(replay.json()['runs'][0]['reused'])
        self.fixture.condition.assert_called_once();self.fixture.report.assert_called_once();self.fixture.send.assert_not_called()
        from run_service import runtime
        audit=runtime(self.fixture.repo.get(key))['execution']
        self.assertEqual(audit['job_id'],73)
        self.assertTrue(audit['identity_verified'])
        self.assertFalse(audit['job_run_id_verified_by_server'])
        # Enabling the Job worker must not silently enable the manual UI operation.
        self.assertEqual(self.fixture.start().status_code,403) # missing Origin rejected first
        self.assertEqual(web.post('/api/tasks/fixture/trial',headers=self.headers,json={
            'business_date':'2026-10-21','expected_updated_at':'v1',
            'request_key':'00000000-0000-0000-0000-000000000001','confirm':True}).status_code,503)

    def test_disabled_wrong_creator_job_and_shared_authority_do_no_business_work(self):
        web=self.fixture.client
        for body in ({**self.body,'creator_subject':'bob'},{**self.body,'job_id':74}):
            self.assertEqual(web.post('/api/creator-jobs/dispatch',headers=self.headers,json=body).status_code,403)
        with patch.dict(os.environ,{'HUB_CREATOR_JOBS_ENABLED':'false'}):
            self.assertEqual(web.post('/api/creator-jobs/dispatch',headers=self.headers,json=self.body).status_code,503)
        with patch.dict(os.environ,{'TASK_EXECUTION_ENABLED':'false'}):
            self.assertEqual(web.post('/api/creator-jobs/dispatch',headers=self.headers,json=self.body).status_code,503)
        self.fixture.identity=replace(self.fixture.identity,mode='databricks_app')
        self.assertEqual(web.post('/api/creator-jobs/dispatch',headers=self.headers,json=self.body).status_code,403)
        self.fixture.condition.assert_not_called();self.fixture.report.assert_not_called();self.fixture.send.assert_not_called()


if __name__=='__main__':unittest.main()

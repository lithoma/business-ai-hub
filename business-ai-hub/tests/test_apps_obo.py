"""OBO/creator boundaries only. Fixture SDK responses; no cloud/model/SQL calls."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from hub_auth import Identity, current_identity, install_auth
from task_authorization import CreatorAuthorizations, verify_creator
import apps_auth


class AppsOBOTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{
            'HUB_AUTH_MODE':'databricks_app_obo','HUB_PUBLIC_ORIGIN':'https://hub.example',
            'DATABRICKS_APP_NAME':'fixture','DATABRICKS_HOST':'https://company.example',
            'DATABRICKS_CLIENT_ID':'app-fixture','DATABRICKS_CLIENT_SECRET':'offline-fixture-only'})
        self.env.start();self.addCleanup(self.env.stop)
        self.created=[]
        def client_factory(**kwargs):
            # Exercise the real explicit credential strategy; it must only return
            # the provided user credential even with App M2M variables present.
            headers=kwargs['credentials_strategy'](SimpleNamespace(host=kwargs['host'],group_id=None))()
            token=headers['Authorization'].removeprefix('Bearer ')
            if token=='invalid':raise RuntimeError('fixture rejected')
            subject={'alice-token':'alice','bob-token':'bob'}[token]
            client=SimpleNamespace(config=SimpleNamespace(host=kwargs['host'],client_id=None),
                current_user=SimpleNamespace(me=Mock(return_value=SimpleNamespace(
                    id=subject,active=True,user_name=subject+'@example.com'))))
            self.created.append(client)
            return client
        self.sdk=patch.object(apps_auth,'WorkspaceClient',side_effect=client_factory)
        self.sdk.start();self.addCleanup(self.sdk.stop)
        self.app=FastAPI();install_auth(self.app)
        @self.app.get('/api/who')
        def who():
            user=current_identity()
            return {'key':user.key,'mode':user.mode,'security':user.security}
        self.web=TestClient(self.app)
        self.headers={'x-forwarded-user':'idp-actor','x-forwarded-email':'forged@example.com',
            'x-forwarded-host':'hub.example','x-forwarded-access-token':'alice-token'}

    def test_users_are_token_bound_and_shared_identity_never_used(self):
        with patch.object(apps_auth,'app_workspace') as shared:
            alice=self.web.get('/api/who',headers=self.headers).json()
            bob=self.web.get('/api/who',headers={**self.headers,'x-forwarded-access-token':'bob-token'}).json()
            shared.assert_not_called()
        self.assertNotEqual(alice['key'],bob['key'])
        self.assertEqual(alice['security']['creator_subject'],'alice')
        self.assertEqual(alice['security']['execution_authority'],'creator')
        self.assertEqual(alice['mode'],'apps_obo')
        session=self.web.get('/api/session',headers=self.headers).json()
        self.assertEqual(session['data_authority'],'current_user')
        self.assertEqual(session['task_authority'],'creator')
        self.assertTrue(session['task_authorization']['authorized'])
        self.assertFalse(session['background_delegation_ready'])
        self.assertNotIn('alice-token',str(session))
        self.assertNotIn('offline-fixture-only',str(session))

    def test_missing_invalid_token_wrong_ingress_and_origin_are_rejected(self):
        with patch.object(apps_auth,'app_workspace') as shared:
            for token in ('','invalid'):
                self.assertEqual(self.web.get('/api/who',headers={**self.headers,'x-forwarded-access-token':token}).status_code,401)
            self.assertEqual(self.web.get('/api/who',headers={**self.headers,'x-forwarded-host':'other.example'}).status_code,403)
            self.assertEqual(self.web.post('/api/session/logout',headers=self.headers).status_code,403)
            shared.assert_not_called()
        self.assertEqual(self.web.post('/api/session/logout',headers={**self.headers,'origin':'https://hub.example'}).status_code,200)
        # No extra per-task authorization endpoint/workflow is introduced.
        self.assertEqual(self.web.post('/api/session/task-authorization',headers={**self.headers,'origin':'https://hub.example'}).status_code,405)

    def test_creator_credentials_expire_and_actual_sdk_identity_is_checked(self):
        self.web.get('/api/who',headers=self.headers)
        identity=Identity('alice','https://company.example','Alice','apps_obo',self.created[-1])
        task={'report':{'security':identity.security}}
        clock=[0];store=CreatorAuthorizations(ttl=10,clock=lambda:clock[0])
        store.remember(identity)
        self.assertEqual(store.resolve(task).key,identity.key)
        bob=Identity('bob',identity.workspace,'Bob','apps_obo',identity.client)
        with self.assertRaises(PermissionError):verify_creator(task,bob)
        clock[0]=11
        with self.assertRaises(PermissionError):store.resolve(task)
        store.remember(identity)
        identity.client.current_user.me.return_value.id='bob'
        with self.assertRaises(PermissionError):store.resolve(task)
        self.assertFalse(store.status(identity)['authorized'])
        store.remember(identity);store.revoke(identity)
        with self.assertRaises(PermissionError):store.resolve(task)

    def test_sdk_obo_strategy_cannot_use_environment_app_credentials(self):
        # Construct the actual SDK client without issuing an HTTP request.
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.core import Config
        with patch.object(apps_auth,'WorkspaceClient',wraps=WorkspaceClient), \
             patch.object(Config,'_resolve_host_metadata'), \
             patch.object(apps_auth,'verified_identity') as verify:
            verify.side_effect=lambda client,mode:Identity('alice','https://company.example','Alice',mode,client)
            result=self.web.get('/api/who',headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            client=verify.call_args.args[0]
            self.assertEqual(client.config.auth_type,'apps-obo')
            self.assertEqual(client.config.authenticate()['Authorization'],'Bearer alice-token')


if __name__=='__main__':unittest.main()

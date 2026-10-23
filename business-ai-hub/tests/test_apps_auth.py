"""Focused Apps preparation checks; no real SDK auth, model or SQL calls."""
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import apps_auth
from hub_auth import current_identity,install_auth
from environment_config import read_profile,missing_settings

ROOT=Path(__file__).resolve().parents[2]


class AppsPreparationTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'HUB_AUTH_MODE':'databricks_app','HUB_PUBLIC_ORIGIN':'https://hub.example',
            'DATABRICKS_APP_NAME':'fixture-app','DATABRICKS_HOST':'https://company.example',
            'DATABRICKS_CLIENT_ID':'fixture-app-id','DATABRICKS_CLIENT_SECRET':'offline-fixture-only'})
        self.env.start();self.addCleanup(self.env.stop)
        self.client=SimpleNamespace(config=SimpleNamespace(host='https://company.example',
            client_id='fixture-app-id',auth_type='oauth-m2m'),current_user=Mock())
        self.cache=patch.object(apps_auth,'_client',self.client);self.cache.start();self.addCleanup(self.cache.stop)
        app=FastAPI();install_auth(app)
        @app.get('/api/who')
        def who():
            identity=current_identity()
            return {'key':identity.key,'label':identity.label,'security':identity.security}
        @app.post('/api/change')
        def change():return {'ok':True}
        self.web=TestClient(app)
        self.headers={'x-forwarded-user':'alice','x-forwarded-email':'alice@example.com','x-forwarded-host':'hub.example'}

    def test_platform_login_required_and_shared_sp_authority_is_explicit(self):
        self.assertEqual(self.web.get('/api/who').status_code,401)
        first=self.web.get('/api/who',headers=self.headers).json()
        second=self.web.get('/api/who',headers={**self.headers,'x-forwarded-user':'bob','x-forwarded-email':'bob@example.com'}).json()
        self.assertEqual(first['key'],second['key'])
        self.assertNotEqual(first['label'],second['label'])
        self.assertEqual(first['security']['execution_authority'],'app_service_principal')
        self.assertEqual(first['security']['created_by_platform_user'],'alice')
        self.client.current_user.me.assert_not_called()
        self.assertEqual(self.web.post('/api/change',headers=self.headers).status_code,403)
        self.assertEqual(self.web.post('/api/change',headers={**self.headers,'origin':'https://hub.example'}).status_code,200)
        self.assertEqual(self.web.get('/api/who',headers={**self.headers,'x-forwarded-host':'wrong.example'}).status_code,403)

    def test_no_app_runtime_or_wrong_sdk_identity_never_falls_back_to_user(self):
        with patch.dict(os.environ,{'DATABRICKS_APP_NAME':''}):
            self.assertEqual(self.web.get('/api/who',headers=self.headers).status_code,503)
        self.client.config.client_id='another-app'
        self.assertEqual(self.web.get('/api/who',headers=self.headers).status_code,503)

    def test_apps_profile_does_not_request_custom_oauth_or_local_storage_profile(self):
        config=read_profile(ROOT/'config/company-apps.yaml')
        missing=missing_settings(config)
        self.assertIn('TASK_REGISTRY_TABLE',missing)
        self.assertNotIn('HUB_OAUTH_CLIENT_ID',missing)
        self.assertNotIn('HUB_STORAGE_PROFILE',missing)
        self.assertFalse(config['env']['TASK_EXECUTION_ENABLED'])
        self.assertFalse(config['env']['HUB_INCLUDE_YAML_TASKS'])

    def test_delta_insert_supplies_required_attempt_id(self):
        from run_service import RunRepository
        query=Mock(return_value=[])
        repo=RunRepository(query,'catalog.schema.runs')
        repo.get=Mock(return_value={})
        repo.insert({'run_id':'fixture'})
        statement,params=query.call_args.args
        self.assertIn('attempt_id',statement)
        self.assertTrue(params['attempt_id'])


if __name__=='__main__':unittest.main()

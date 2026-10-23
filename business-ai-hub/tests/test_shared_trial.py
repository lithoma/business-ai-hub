"""Only the new temporary-sharing boundary; no model, SQL or mail calls."""
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hub_auth import Identity, IdentityMiddleware, current_identity

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('hub_share_tests', ROOT/'scripts/share-local.py')
share = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share)


class SharedTrialTests(unittest.TestCase):
    def test_company_profile_cannot_disable_identity_or_touch_storage(self):
        with patch.object(share,'WorkspaceClient') as sdk:
            with self.assertRaisesRegex(ValueError,'公司 OAuth'):
                share.build_shared_app(ROOT/'config/company.yaml','https://trial.example')
            sdk.assert_not_called()

    def test_visitors_share_explicit_test_identity_but_origin_and_host_still_checked(self):
        auth=share.SharedTrialAuth('https://trial.example','unused','https://dev.example')
        auth.local=Identity('test-owner','https://dev.example','共享测试账户','shared_trial',SimpleNamespace())
        app=FastAPI();app.state.auth=auth;app.add_middleware(IdentityMiddleware)
        @app.api_route('/api/fixture',methods=['GET','POST'])
        def fixture():return {'subject':current_identity().subject,'mode':current_identity().mode}
        with TestClient(app,base_url='https://trial.example',client=('127.0.0.1',12000)) as client:
            self.assertEqual(client.get('/api/fixture').json()['subject'],'test-owner')
            self.assertEqual(client.get('/api/fixture',headers={'x-forwarded-email':'other@example.com'}).json()['subject'],'test-owner')
            self.assertEqual(client.post('/api/fixture').status_code,403)
            self.assertEqual(client.post('/api/fixture',headers={'Origin':'https://other.example'}).status_code,403)
            self.assertEqual(client.post('/api/fixture',headers={'Origin':'https://trial.example'}).status_code,200)
            self.assertEqual(client.get('/api/fixture',headers={'Host':'other.example'}).status_code,403)

    def test_trial_proxy_reuses_existing_worker_and_rejects_cross_origin_before_forwarding(self):
        import yaml
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{},clear=False), patch.dict(sys.modules):
            sys.modules.pop('app',None)
            config=yaml.safe_load((ROOT/'config/local.yaml').read_text())
            config['local']['database']=str(Path(folder)/'trial.sqlite3')
            path=Path(folder)/'local.yaml';path.write_text(yaml.safe_dump(config))
            app=share.build_shared_app(path,'https://trial.example')
            app.state.auth.local=Identity('test-owner',config['env']['DATABRICKS_HOST'],'共享测试账户','shared_trial',SimpleNamespace())
            self.assertFalse(hasattr(app.state,'local_trials'))
            upstream=SimpleNamespace(content=b'{"enabled":true,"mail_available":true}',status_code=200,headers={'content-type':'application/json'})
            with TestClient(app,base_url='https://trial.example',client=('127.0.0.1',12000)) as client, patch.object(share.requests,'request',return_value=upstream) as proxy:
                self.assertEqual(client.post('/api/tasks/fixture/trial',json={},headers={'Origin':'https://other.example'}).status_code,403)
                proxy.assert_not_called()
                self.assertTrue(client.get('/api/local-trials/settings').json()['enabled'])
                proxy.assert_called_once()
                self.assertEqual(proxy.call_args.args[1],'http://127.0.0.1:8765/api/local-trials/settings')


if __name__ == '__main__':unittest.main()

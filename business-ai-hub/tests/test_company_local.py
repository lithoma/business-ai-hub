"""One isolated offline check for the Python-only company desktop launch path."""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

ROOT=Path(__file__).resolve().parents[2]


class CompanyDesktopTests(unittest.TestCase):
    def test_company_profile_browser_login_and_local_crud_without_cloud_calls(self):
        code=textwrap.dedent('''
            import importlib.util,os,sys
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from types import SimpleNamespace
            from unittest.mock import Mock,patch
            from fastapi.testclient import TestClient
            sys.path.insert(0,str(Path('business-ai-hub').resolve()))
            from environment_config import read_profile,missing_settings,apply_selected_profile
            cfg=Path('config/company-local.yaml').resolve()
            profile=read_profile(cfg)
            assert not missing_settings(profile)
            os.environ['HUB_CONFIG_FILE']=str(cfg)
            apply_selected_profile()
            spec=importlib.util.spec_from_file_location('desktop_launcher','scripts/dev-local.py')
            launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
            with TemporaryDirectory() as directory:
                application=launcher.build_app(Path(directory)/'demo.sqlite3',profile='',
                    interactive_analysis=True,conversation_workflow=True,genie_one_chat=True,manual_trials=True)
                user=SimpleNamespace(config=SimpleNamespace(host=profile['env']['DATABRICKS_HOST'],client_id=None),
                    current_user=SimpleNamespace(me=Mock(return_value=SimpleNamespace(id='fixture-company-user',active=True,user_name='fixture@example.com'))))
                with patch('hub_auth.WorkspaceClient',return_value=user) as sdk:
                    with TestClient(application,base_url='http://127.0.0.1:8765',client=('127.0.0.1',50000)) as web:
                        session=web.get('/api/session')
                        assert session.status_code==200,session.text
                        assert session.json()['workspace']==profile['env']['DATABRICKS_HOST']
                        assert sdk.call_args.kwargs['auth_type']=='external-browser'
                        assert web.get('/api/tasks').json()['tasks']==[]
                        payload={'task_id':'desktop-fixture','name':'Offline fixture',
                            'schedule':{'frequency':'daily'},'trigger':{'type':'always'},
                            'report':{'agent':'genie_one','prompt':'Offline fixture'}}
                        response=web.post('/api/tasks/create',json=payload)
                        assert response.status_code==200,response.text
                        tasks=web.get('/api/tasks').json()['tasks']
                        assert len(tasks)==1 and tasks[0]['report']['security']['creator_subject']=='fixture-company-user'
                        assert web.get('/api/reports').json()['runs']==[]
                application.state.run_service.pool.shutdown(wait=True)
                application.state.local_trials.pool.shutdown(wait=True)
        ''')
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()

"""App identity gate and one-request check, fully offline."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from test_task_drafts import proposal, response

path = Path(__file__).resolve().parents[2] / 'scripts/check-app-parser.py'
spec = importlib.util.spec_from_file_location('check_app_parser', path)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class AppIdentityCheckTests(unittest.TestCase):
    def run_check(self, workspace, infer, model):
        with patch('databricks.sdk.WorkspaceClient', return_value=workspace), \
             patch('task_llm.generate_configuration', model), \
             patch.object(sys, 'argv', ['check-app-parser.py'] + (['--infer'] if infer else [])), \
             patch.dict(os.environ), redirect_stdout(io.StringIO()) as output:
            check.main()
            return output.getvalue()

    def test_personal_profile_or_wrong_app_identity_never_infers(self):
        model = Mock(side_effect=AssertionError('No inference'))
        cli = Mock(config=SimpleNamespace(client_id=None, auth_type='databricks-cli'))
        with self.assertRaises(SystemExit): self.run_check(cli, True, model)
        cli.current_user.me.assert_not_called(); model.assert_not_called()
        wrong = Mock(config=SimpleNamespace(client_id=check.APP_ID, auth_type='oauth-m2m'))
        wrong.current_user.me.return_value = SimpleNamespace(id='someone-else')
        with self.assertRaises(SystemExit): self.run_check(wrong, True, model)
        model.assert_not_called()

    def test_verified_app_identity_still_requires_explicit_infer_flag(self):
        app = Mock(config=SimpleNamespace(client_id=check.APP_ID, auth_type='oauth-m2m'))
        app.current_user.me.return_value = SimpleNamespace(id=check.APP_SCIM_ID)
        model = Mock(side_effect=AssertionError('No inference'))
        output = self.run_check(app, False, model)
        self.assertIn('"inference_requests": 0', output); model.assert_not_called()

    def test_explicit_app_check_makes_one_request_and_checks_intent(self):
        app = Mock(config=SimpleNamespace(client_id=check.APP_ID, auth_type='oauth-m2m'))
        app.current_user.me.return_value = SimpleNamespace(id=check.APP_SCIM_ID)
        data = proposal(); data['schedule']['weekday'] = 'friday'
        data['report']['agent'] = 'genie_a2'; data['trigger']['type'] = 'always'; data['conditional'] = False
        model = Mock(return_value=response(data))
        output = self.run_check(app, True, model)
        model.assert_called_once()
        self.assertIn('"inference_requests": 1', output)
        self.assertIn('"friday": true', output)
        self.assertIn('"draft_saved": false', output)


if __name__ == '__main__': unittest.main()

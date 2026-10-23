"""Necessary offline checks for the two selectable environment profiles."""
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import yaml
from environment_config import read_profile, missing_settings, apply_selected_profile, task_manager_settings

ROOT=Path(__file__).resolve().parents[2]


class EnvironmentConfigTests(unittest.TestCase):
    def test_profiles_keep_different_hosts_dates_and_disabled_company_execution(self):
        local=read_profile(ROOT/'config/local.yaml');company=read_profile(ROOT/'config/company.yaml')
        self.assertNotEqual(local['env']['DATABRICKS_HOST'],company['env']['DATABRICKS_HOST'])
        self.assertEqual(local['task_manager']['schedule']['business_date_offset_days'],487)
        self.assertEqual(company['task_manager']['schedule']['business_date_offset_days'],1)
        self.assertEqual(company['env']['SQL_WAREHOUSE_ID'],'540a674067e832d2')
        self.assertFalse(company['task_manager']['execution']['enabled'])
        self.assertIn('TASK_REGISTRY_TABLE',missing_settings(company))
        with patch.dict(os.environ,{'HUB_CONFIG_FILE':str(ROOT/'config/company.yaml'),'TASK_REGISTRY_TABLE':'old.test.table'}):
            with self.assertRaisesRegex(ValueError,'未回退'):apply_selected_profile()
            self.assertEqual(os.environ['TASK_REGISTRY_TABLE'],'old.test.table')

    def test_task_manager_overrides_are_shared_and_secrets_cannot_enter_profile(self):
        with patch.dict(os.environ,{'HUB_CONFIG_FILE':str(ROOT/'config/local.yaml')}):
            loaded=task_manager_settings({'notification':{'email':{'enabled':False}},'schedule':{'time':'00:00'}})
            self.assertEqual(loaded['schedule']['time'],'09:00')
            self.assertEqual(loaded['notification']['email']['enabled'],False)
        company=read_profile(ROOT/'config/company.yaml')
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'profile.yaml'
            company['env']['GMAIL_REFRESH_TOKEN']='fixture-not-a-real-token'
            path.write_text(yaml.safe_dump(company))
            with self.assertRaises(ValueError):read_profile(path)

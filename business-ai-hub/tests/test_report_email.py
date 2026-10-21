"""Offline MIME/CSV/provider tests. No actual OAuth, mail, model or Databricks calls."""
import base64
import copy
import csv
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

ENGINE = Path(__file__).resolve().parents[2] / 'business-task-manager-v1' / 'business-task-manager-v1'
sys.path.insert(0, str(ENGINE))
from report_email import (build_message, export_csv, deliver_emails, GmailProvider,
                         EmailOutcomeUnknown, EmailRejected, EmailConfigurationError, ExportLimitError)
from test_execution import MemoryLedger, NOW
from task_engine import TaskManager
import yaml


class Frame:
    columns = ['品牌', 'ID', '数值', '说明']
    rows = [('善存', '000123', Decimal('-12.50'), '含逗号,引号"和\n换行'), ('其他', '=1+1', 42, None)]
    def limit(self, count):
        result = Frame(); result.columns = self.columns; result.rows = self.rows[:count]; return result
    def toLocalIterator(self): return iter(self.rows)


class MailLedger(MemoryLedger):
    def pending_email_reports(self, day, limit):
        return [row for row in self.rows.values() if row['status'] == 'REPORT_CREATED' and row['notification_status'] == 'NOT_QUEUED'
                and json.loads(row['config_json']).get('report', {}).get('delivery', {}).get('enabled')][:limit]
    def claim_email(self, key):
        if self.rows[key]['notification_status'] != 'NOT_QUEUED': return False
        self.rows[key]['notification_status'] = 'EMAIL_SENDING'; return True
    def email_result(self, key, status, message_id='', error='', artifacts=None):
        self.rows[key].update(notification_status=status, provider_message_id=message_id,
                              notification_error_code=error, artifacts_json=json.dumps(artifacts or []))


class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'report.html.j2').write_text((ENGINE / 'templates/email/default.html.j2').read_text())
        (self.root / 'data.sql').write_text('SELECT * FROM approved_fixture WHERE day=:business_date')
        self.config = {'enabled': True, 'from_address': 'sender@example.com', 'allowed_recipients': ['recipient@example.com'],
                       'templates': {'default': {'file': 'report.html.j2'}},
                       'exports': {'wide': {'sql_file': 'data.sql', 'filename': '业务宽表.csv', 'max_rows': 100}},
                       'max_messages_per_dispatch': 20}
        self.task = {'task_id': 'mail-fixture', 'name': '测试日报', '_source': 'registry',
                     'schedule': {'frequency': 'daily'}, 'trigger': {'type': 'always'},
                     'report': {'agent': 'genie_a1', 'prompt': 'Report', 'delivery': {'enabled': True, 'to': ['recipient@example.com'], 'csv_exports': ['wide']}},
                     '_execution_context': {'business_date': '2025-06-19', 'dispatch_date': '2026-10-19'}}
        self.ledger = MailLedger()
        self.key = self.ledger.claim(self.task, 'slot', NOW.date(), json.dumps(self.task))
        self.ledger.finish(self.key, 'REPORT_CREATED', reason='Fixture only', report='# 测试报告\n<script>unsafe</script>')
        self.run = self.ledger.rows[self.key]
        self.spark = Mock(sql=Mock(return_value=Frame()))

    def test_complete_mime_csv_has_all_columns_chinese_and_exact_decimals(self):
        raw, metadata = build_message(self.spark, self.root, self.config, self.run)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(message['To'], 'recipient@example.com')
        self.assertIn('测试日报', str(message['Subject']))
        html = message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('<h1>测试报告</h1>', html)
        self.assertNotIn('<script>', html)
        attachment = list(message.iter_attachments())[0]
        self.assertEqual(attachment.get_filename(), '业务宽表.csv')
        content = attachment.get_payload(decode=True)
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        self.assertEqual(rows[0], Frame.columns)
        self.assertEqual(rows[1], ['善存', '000123', '-12.50', '含逗号,引号"和\n换行'])
        self.assertEqual(rows[2][1], "'=1+1")
        self.assertEqual(metadata[0]['rows'], 2)
        self.assertEqual(self.spark.sql.call_args.kwargs['args'], {'business_date': '2025-06-19'})
        # CSV data is exported directly from the query adapter, never a model.

    def test_limits_wide_tables_and_recipient_gate_no_silent_partial_export(self):
        export = dict(self.config['exports']['wide'], max_rows=1)
        with self.assertRaises(ExportLimitError): export_csv(self.spark, self.root, export, self.task['_execution_context'])
        frame = Frame(); frame.columns = [f'col{i}' for i in range(500)]; frame.rows = [tuple(range(500))]
        self.spark.sql.return_value = frame
        content, meta = export_csv(self.spark, self.root, self.config['exports']['wide'], self.task['_execution_context'])
        self.assertEqual(meta['columns'], 500)
        self.assertEqual(len(next(csv.reader(io.StringIO(content.decode('utf-8-sig'))))), 500)
        with self.assertRaises(ExportLimitError): build_message(self.spark, self.root, dict(self.config, max_message_bytes=100), self.run)
        with self.assertRaises(EmailConfigurationError): build_message(self.spark, self.root, dict(self.config, allowed_recipients=[]), self.run)

    def test_gmail_is_single_post_and_ambiguous_outcomes_are_not_retried(self):
        credentials = Mock(valid=True, token='fixture-only')
        adapter = GmailProvider(credentials)
        result = Mock(status_code=200, json=Mock(return_value={'id': 'fixture-message'}))
        with patch('report_email.requests.post', return_value=result) as post:
            self.assertEqual(adapter.send(b'fixture MIME'), 'fixture-message')
            post.assert_called_once()
            self.assertEqual(base64.urlsafe_b64decode(post.call_args.kwargs['json']['raw']), b'fixture MIME')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        for failure in (requests.Timeout('fixture token'), requests.ConnectionError('fixture token')):
            with patch('report_email.requests.post', side_effect=failure) as post:
                with self.assertRaises(EmailOutcomeUnknown): adapter.send(b'fixture')
                post.assert_called_once()
        with patch.dict(os.environ, {'GMAIL_CLIENT_ID': '', 'GMAIL_CLIENT_SECRET': '', 'GMAIL_REFRESH_TOKEN': ''}):
            with self.assertRaises(EmailConfigurationError): GmailProvider.from_environment()

    def test_mail_states_prevent_duplicate_send_and_keep_message_id(self):
        provider = Mock(send=Mock(return_value='fixture-message'))
        result = deliver_emails(self.spark, self.root, self.config, self.ledger, NOW.date(), provider)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(self.run['notification_status'], 'EMAIL_ACCEPTED')
        self.assertEqual(self.run['provider_message_id'], 'fixture-message')
        deliver_emails(self.spark, self.root, self.config, self.ledger, NOW.date(), provider)
        provider.send.assert_called_once()
        self.run['notification_status'] = 'NOT_QUEUED'
        provider.send.side_effect = EmailOutcomeUnknown('fixture')
        deliver_emails(self.spark, self.root, self.config, self.ledger, NOW.date(), provider)
        self.assertEqual(self.run['notification_status'], 'EMAIL_UNKNOWN')
        before = provider.send.call_count
        deliver_emails(self.spark, self.root, self.config, self.ledger, NOW.date(), provider)
        self.assertEqual(provider.send.call_count, before)

    def test_disabled_mail_does_not_access_data_auth_or_provider(self):
        provider = Mock()
        with patch('report_email.GmailProvider.from_environment', side_effect=AssertionError('No auth')):
            result = deliver_emails(self.spark, self.root, {'enabled': False}, self.ledger, NOW.date(), provider)
        self.assertTrue(result['disabled']); self.spark.sql.assert_not_called(); provider.send.assert_not_called()

    def test_existing_executor_uses_mail_without_calling_outbox_or_agent_for_csv(self):
        task_dir = self.root / 'tasks' / 'mail'; task_dir.mkdir(parents=True)
        task = copy.deepcopy(self.task); task['enabled'] = True; task['report']['prompt'] = 'prompt.md'
        task.pop('_execution_context'); task.pop('_source')
        (task_dir / 'config.yaml').write_text(yaml.safe_dump(task))
        (task_dir / 'prompt.md').write_text('Analyse fixture data, preserve the business date')
        settings = {'execution': {'enabled': True, 'runs_table': 'workspace.douyin.business_task_runs_v1'},
                    'schedule': {'time': '09:00', 'timezone': 'Asia/Shanghai', 'business_date_offset_days': 487},
                    'agents': {}, 'notification': {'provider': 'gmail', 'legacy_alert_disabled': True, 'email': self.config}}
        path = self.root / 'settings.yaml'; path.write_text(yaml.safe_dump(settings))
        ledger = MailLedger(); provider = Mock(send=Mock(return_value='fixture-message'))
        agents = Mock(ask=Mock(return_value='Offline fixture report'))
        outbox = Mock(side_effect=AssertionError('Mail must not depend on SQL Alert'))
        manager = TaskManager(self.spark, path)
        manager.run(now=NOW, agents=agents, store=ledger, publisher=outbox, mail_provider=provider)
        agents.ask.assert_called_once(); provider.send.assert_called_once(); outbox.assert_not_called()
        manager.run(now=NOW, agents=agents, store=ledger, publisher=outbox, mail_provider=provider)
        agents.ask.assert_called_once(); provider.send.assert_called_once()


if __name__ == '__main__': unittest.main()

"""Offline contracts. All model responses are test fixtures, never product fallbacks."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

import app as backend
from task_drafts import TaskProposal, extract_proposal, has_condition, proposal_schema, review_proposal

REQUIREMENTS = '每周一检查善存上一周GMV，如果比前一周下降超过15%，分析原因并生成销售风险报告。'


def proposal():
    return {
        'name': '善存销售风险报告', 'description': REQUIREMENTS,
        'schedule': {'frequency': 'weekly', 'time': '09:00', 'timezone': 'Asia/Shanghai',
                     'weekday': 'monday', 'interval_days': 0, 'start_date': '', 'cron': ''},
        'trigger': {'type': 'agent', 'agent': 'genie_a1', 'context_agents': ['genie_a1'], 'sql': '',
                    'prompt': '比较善存上一周与前一周的 GMV，仅当下降超过15%时返回 trigger=true；数据不足应报错。'},
        'report': {'agent': 'genie_a1', 'prompt': '分析善存 GMV 下降原因并生成销售风险报告。', 'template': ''},
        'conditional': True, 'assumptions': ['未提供时刻，建议 09:00。', '未提供时区，建议 Asia/Shanghai。'],
        'warnings': [], 'missing_information': ['需确认 GMV 口径及可用数据周期。'],
    }


def response(data=None, finish='stop', blocks=False):
    text = json.dumps(data if data is not None else proposal(), ensure_ascii=False)
    return {'choices': [{'finish_reason': finish, 'message': {
        'content': [{'type': 'text', 'text': text}] if blocks else text,
    }}]}


class ParserTests(unittest.TestCase):
    def test_schema_is_databricks_compatible(self):
        schema = proposal_schema()
        for key in ('$ref', '$defs', 'pattern', 'anyOf', 'oneOf'):
            self.assertNotIn(f'"{key}"', json.dumps(schema))
        self.assertEqual(set(schema['properties']), set(schema['required']))

    def test_preserves_conditional_threshold_and_period(self):
        parsed = extract_proposal(response(blocks=True))
        review = review_proposal(parsed, REQUIREMENTS)
        self.assertTrue(review.conditional)
        self.assertIn('15%', parsed.trigger.prompt)
        self.assertIn('前一周', parsed.trigger.prompt)
        self.assertTrue(review.missing_information)

    def test_conditional_is_never_downgraded_even_if_model_lies(self):
        data = proposal(); data['trigger']['type'] = 'always'; data['conditional'] = False
        with self.assertRaises(ValueError):
            review_proposal(TaskProposal.model_validate(data), REQUIREMENTS)

    def test_report_analysis_does_not_automatically_imply_trigger(self):
        self.assertFalse(has_condition('每周生成销售增长趋势报告'))

    def test_unconfigured_and_unsupported_schedule_remain_explicit(self):
        data = proposal(); data['trigger']['type'] = 'unconfigured'
        review = review_proposal(TaskProposal.model_validate(data), '每月检查风险')
        self.assertTrue(any('尚未配置' in text for text in review.missing_information))
        self.assertTrue(any('占位' in text for text in review.missing_information))

    def test_invented_sql_is_rejected(self):
        data = proposal(); data['trigger'].update(type='sql', sql='SELECT * FROM made_up_table')
        with self.assertRaises(ValueError):
            review_proposal(TaskProposal.model_validate(data), REQUIREMENTS)

    def test_truncated_refused_malformed_missing_and_extra_fields_rejected(self):
        cases = [response(finish='length'), response(finish='content_filter'),
                 {'choices': [{'finish_reason': 'stop', 'message': {'content': '```json {} ```'}}]}]
        for mutate in ('missing', 'extra', 'boolean', 'weekday', 'time', 'timezone', 'interval'):
            data = proposal()
            if mutate == 'missing': del data['schedule']['time']
            if mutate == 'extra': data['enabled'] = True
            if mutate == 'boolean': data['conditional'] = 'true'
            if mutate == 'weekday': data['schedule']['weekday'] = 'someday'
            if mutate == 'time': data['schedule']['time'] = '29:00'
            if mutate == 'timezone': data['schedule']['timezone'] = 'Invented/Zone'
            if mutate == 'interval': data['schedule'].update(frequency='interval', interval_days=0)
            cases.append(response(data))
        for case in cases:
            with self.subTest(case=case), self.assertRaises((ValueError, KeyError)):
                extract_proposal(case)


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)
        self.env = patch.dict(os.environ, {'TASK_PARSER_ENDPOINT': 'existing-parser'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.workspace = patch.object(backend, '_workspace', return_value=SimpleNamespace(config=SimpleNamespace(
            host='https://example.invalid', authenticate=lambda: {'Authorization': 'Bearer fixture-only'},
        ))).start()
        self.sql = patch.object(backend, '_exec_sql', side_effect=AssertionError('No SQL during generation')).start()
        self.genie = patch.object(backend, '_ask_genie', side_effect=AssertionError('No Genie during generation')).start()
        self.transport = patch('task_llm.requests.post', return_value=Mock(status_code=200, json=lambda: response())).start()
        self.addCleanup(patch.stopall)

    def generate(self, text=REQUIREMENTS):
        return self.client.post('/api/tasks/generate', json={'requirements': text})

    def test_exactly_one_inference_no_database_or_genie(self):
        result = self.generate()
        self.assertEqual(result.status_code, 200, result.text)
        self.transport.assert_called_once()
        self.sql.assert_not_called(); self.genie.assert_not_called()
        kwargs = self.transport.call_args.kwargs
        self.assertEqual(kwargs['timeout'], (5, 60))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['response_format']['type'], 'json_schema')
        config = result.json()['configuration']
        self.assertFalse(config['enabled'])
        self.assertEqual(config['report']['draft_review']['original_request'], REQUIREMENTS)

    def test_missing_endpoint_never_uses_fake_model_or_workspace(self):
        with patch.dict(os.environ, {'TASK_PARSER_ENDPOINT': ''}):
            result = self.generate()
        self.assertEqual(result.status_code, 503)
        self.assertIn('Model not configured', result.json()['detail'])
        self.transport.assert_not_called(); self.workspace.assert_not_called()
        self.assertEqual(self.client.get('/api/health').status_code, 200)

    def test_unity_gateway_uses_service_name_and_one_request(self):
        with patch.dict(os.environ, {'TASK_PARSER_ENDPOINT': 'system.ai.gpt-oss-20b'}):
            result = self.generate()
        self.assertEqual(result.status_code, 200, result.text)
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args[0], 'https://example.invalid/ai-gateway/mlflow/v1/chat/completions')
        self.assertEqual(self.transport.call_args.kwargs['json']['model'], 'system.ai.gpt-oss-20b')
        self.sql.assert_not_called(); self.genie.assert_not_called()

    def test_legacy_route_is_preserved_and_invalid_names_make_no_request(self):
        self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(self.transport.call_args.args[0], 'https://example.invalid/serving-endpoints/existing-parser/invocations')
        self.assertNotIn('model', self.transport.call_args.kwargs['json'])
        self.transport.reset_mock(); self.workspace.reset_mock()
        for invalid in ('https://other.invalid/model', 'system.ai.gpt/redirect', 'system.ai', 'a.b.c.d'):
            with patch.dict(os.environ, {'TASK_PARSER_ENDPOINT': invalid}):
                self.assertEqual(self.generate().status_code, 503)
        self.transport.assert_not_called(); self.workspace.assert_not_called()

    def test_permission_failure_reports_only_provider_named_privilege_once(self):
        for named in ('USE CATALOG', 'USE_SCHEMA', 'EXECUTE'):
            self.transport.reset_mock()
            self.transport.return_value.status_code = 403
            self.transport.return_value.json = lambda: {'error_code': 'PERMISSION_DENIED',
                'message': f'User does not have {named} on the model service. Bearer fixture-secret must not be exposed.'}
            result = self.generate()
            self.assertEqual(result.status_code, 403, result.text)
            self.assertIn(named.replace('_', ' '), result.json()['detail'])
            self.assertNotIn('fixture-secret', result.text)
            self.transport.assert_called_once(); self.sql.assert_not_called(); self.genie.assert_not_called()

    def test_generic_access_failure_does_not_invent_missing_privileges(self):
        self.transport.return_value.status_code = 403
        self.transport.return_value.json = lambda: {'error_code': 'PERMISSION_DENIED', 'message': 'Access denied: fixture-secret'}
        result = self.generate()
        self.assertEqual(result.status_code, 403)
        self.assertIn('服务未明确报告', result.json()['detail'])
        self.assertNotIn('缺失权限：', result.text)
        self.assertNotIn('fixture-secret', result.text)
        self.transport.assert_called_once()

    def test_structured_privilege_evidence_and_non_json_auth_failure(self):
        from task_llm import LLMAccessError
        error = LLMAccessError(403, 'system.ai.gpt-oss-20b', {'missing_privileges': ['EXECUTE', 'USE_CATALOG', 'fixture-secret']})
        self.assertEqual(error.missing_privileges, ['EXECUTE', 'USE CATALOG'])
        self.transport.return_value.status_code = 401
        self.transport.return_value.json = Mock(side_effect=ValueError('not json'))
        result = self.generate()
        self.assertEqual(result.status_code, 401)
        self.transport.assert_called_once()
        self.assertNotIn('not json', result.text)

    def test_input_limit_and_empty_input_do_not_call_model(self):
        for text in ('   ', 'a' * 4001):
            self.assertEqual(self.generate(text).status_code, 422)
        self.transport.assert_not_called()

    def test_upstream_errors_are_bounded_and_sanitized(self):
        for error, code in [(requests.Timeout('fixture secret'), 504), (requests.HTTPError('fixture secret'), 502)]:
            self.transport.reset_mock(); self.transport.side_effect = error
            result = self.generate()
            self.assertEqual(result.status_code, code)
            self.assertNotIn('fixture secret', result.text)
            self.transport.assert_called_once(); self.sql.assert_not_called()

    def test_invalid_model_response_does_not_retry_or_save(self):
        self.transport.return_value.json = lambda: response(finish='length')
        self.assertEqual(self.generate().status_code, 502)
        self.transport.assert_called_once(); self.sql.assert_not_called()

    def test_user_review_save_persists_schedule_and_provenance_as_disabled_draft(self):
        config = self.generate().json()['configuration']
        config['name'] = "用户修改 O'Brien"
        config['enabled'] = True  # Backend must keep drafts disabled regardless of caller.
        self.sql.side_effect = lambda sql, parameters=None: [] if sql.startswith('SELECT') else None
        result = self.client.post('/api/tasks/create', json=config)
        self.assertEqual(result.status_code, 200, result.text)
        statement = self.sql.call_args.args[0]
        self.assertEqual(self.sql.call_args.args[1]["name"], "用户修改 O'Brien")
        self.assertIn("false,", statement)
        self.assertIn("'draft'", statement)
        params = self.sql.call_args.args[1]
        self.assertIn('Asia/Shanghai', params['schedule'])
        self.assertIn('original_request', params['report'])
        self.assertIn('15%', params['report'])

    def test_conditional_provenance_cannot_be_saved_as_always(self):
        config = self.generate().json()['configuration']
        config['trigger']['type'] = 'always'
        self.assertEqual(self.client.post('/api/tasks/create', json=config).status_code, 422)
        self.sql.assert_not_called()

    def test_manual_creation_still_works_without_model(self):
        self.sql.side_effect = lambda sql, parameters=None: [] if sql.startswith('SELECT') else None
        with patch.dict(os.environ, {'TASK_PARSER_ENDPOINT': ''}):
            result = self.client.post('/api/tasks/create', json={
                'task_id': 'manual', 'name': '手动日报', 'schedule': {'frequency': 'daily'},
                'trigger': {'type': 'always'},
            })
        self.assertEqual(result.status_code, 200, result.text)
        self.transport.assert_not_called()

    def test_string_boolean_and_native_boolean_are_consistent(self):
        for value, expected in [('false', False), ('true', True), (False, False), (True, True)]:
            self.sql.side_effect = None
            self.sql.return_value = [{'task_id': 'fixture', 'enabled': value}]
            self.assertIs(backend._load_registry_tasks()[0]['enabled'], expected)


if __name__ == '__main__':
    unittest.main()

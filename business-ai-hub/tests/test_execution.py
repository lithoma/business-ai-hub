"""Offline 2B -> Registry -> 2C -> history integration, with explicit fake adapters."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone, date
from unittest.mock import Mock, patch
import yaml
from fastapi.testclient import TestClient
import app as backend
from task_execution import readiness, validate_read_sql
from test_task_drafts import proposal, response, REQUIREMENTS

ENGINE = Path(__file__).resolve().parents[2] / 'business-task-manager-v1' / 'business-task-manager-v1'
sys.path.insert(0, str(ENGINE))
from task_engine import TaskManager
from registry_tasks import due_slot, registry_task, validate_read_sql as runtime_read_sql
from run_store import run_id, RunStore

NOW = datetime(2026, 10, 19, 1, 0, 21, tzinfo=timezone.utc)  # Monday 09:00:21 Shanghai
DISPATCH = {'time': '09:00', 'timezone': 'Asia/Shanghai', 'business_date_offset_days': 487}


class Row(dict):
    def asDict(self, recursive=False): return dict(self)


class Rows:
    def __init__(self, rows): self.rows = [Row(r) for r in rows]
    def collect(self): return self.rows
    def limit(self, limit): return Rows(self.rows[:limit])


class MemoryLedger:
    def __init__(self): self.rows = {}
    def claim(self, task, slot, day, config):
        key = run_id(task['_source'], task['task_id'], slot)
        if key in self.rows: return None
        self.rows[key] = dict(run_id=key, task_id=task['task_id'], task_name=task['name'], source=task['_source'],
                              scheduled_slot=slot, dispatch_date=day.isoformat(), status='RUNNING',
                              report_text='', trigger_reason='', error_code='', config_json=config,
                              notification_status='NOT_QUEUED', started_at=NOW.isoformat(), finished_at='')
        return key
    def finish(self, key, status, reason='', report='', error=''):
        self.rows[key].update(status=status, trigger_reason=reason, report_text=report, error_code=error, finished_at=NOW.isoformat())
    def pending_reports(self, day):
        return [r for r in self.rows.values() if r['dispatch_date'] <= day.isoformat() and r['status'] == 'REPORT_CREATED' and r['notification_status'] == 'NOT_QUEUED']
    def mark_queued(self, keys):
        for key in keys: self.rows[key]['notification_status'] = 'QUEUED'


class MemoryRegistry:
    def __init__(self, ledger): self.tasks, self.calls, self.ledger = {}, [], ledger
    def tasks_view(self): return list(copy.deepcopy(self.tasks).values())
    def query(self, sql, params=None):
        params = params or {}
        self.calls.append((sql, params))
        if sql.startswith('SELECT task_id'):
            return [{'task_id': params['task_id']}] if params['task_id'] in self.tasks else []
        if sql.startswith('INSERT'):
            self.tasks[params['task_id']] = dict(task_id=params['task_id'], name=params['name'], description=params['description'],
                schedule=json.loads(params['schedule']), trigger=json.loads(params['trigger']), report=json.loads(params['report']),
                owner=params['owner'], enabled=False, lifecycle_status='draft', updated_at='version1', _source='registry')
            return [{'num_affected_rows': '1'}]
        if sql.startswith('UPDATE'):
            task = self.tasks[params['task_id']]
            if task['updated_at'] != params['version']: return [{'num_affected_rows': '0'}]
            if 'status' in params:
                task.update(lifecycle_status=params['status'], enabled=params['status'] == 'active')
            else:
                task.update(name=params['name'], description=params['description'], owner=params['owner'])
                for key in ('schedule', 'trigger', 'report'): task[key] = json.loads(params[key])
            task['updated_at'] += 'x'
            return [{'num_affected_rows': '1'}]
        if 'FROM workspace.douyin.business_task_runs_v1' in sql:
            values = list(self.ledger.rows.values())
            if params.get('task_id'): values = [r for r in values if r['task_id'] == params['task_id']]
            if params.get('status'): values = [r for r in values if r['status'] == params['status']]
            return values
        raise AssertionError('Unexpected offline SQL: ' + sql)

    def spark(self):
        def sql(query, args=None):
            if query == 'SELECT * FROM workspace.douyin.business_task_registry_v1':
                return Rows([{**t, **{key + '_config': json.dumps(t[key]) for key in ('schedule', 'trigger', 'report')}} for t in self.tasks.values()])
            if query.strip().startswith('SELECT'):
                return Rows([{'should_trigger': True, 'reason': 'explicit test SQL evidence'}])
            raise AssertionError('No mutations expected through fake Spark')
        return Mock(sql=Mock(side_effect=sql))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)
        self.ledger = MemoryLedger(); self.db = MemoryRegistry(self.ledger)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {'schedule': DISPATCH, 'agents': {'genie_a1': 'fixture-a1', 'genie_a2': 'fixture-a2'},
                         'execution': {'enabled': True, 'runs_table': 'workspace.douyin.business_task_runs_v1'},
                         'registry': {'enabled': True, 'table': 'workspace.douyin.business_task_registry_v1'},
                         'notification': {'outbox_table': 'workspace.douyin.business_task_outbox_v1', 'recipient_group': 'default', 'max_report_chars': 12000}}
        self.path = self.root / 'settings.yaml'; self.path.write_text(yaml.safe_dump(self.settings))
        self.agents = Mock(ask=Mock(side_effect=['{"trigger":true,"reason":"fixture evidence: decline 16%"}', 'Explicit offline fixture report']))
        self.publisher = Mock()
        self.spark = self.db.spark()
        self.env = patch.dict(os.environ, {'TASK_EXECUTION_ENABLED': 'true', 'TASK_PARSER_ENDPOINT': 'fixture-parser'})
        self.env.start()
        patch.object(backend, '_exec_sql', side_effect=self.db.query).start()
        patch.object(backend, '_load_registry_tasks', side_effect=self.db.tasks_view).start()
        patch.object(backend, '_load_task_configs', return_value=[]).start()
        # Runtime has no auth/network fallback. Any unexpected Workspace construction fails.
        patch('task_engine.AgentClient', return_value=self.agents).start()
        patch.object(backend, '_workspace', side_effect=AssertionError('Remote access forbidden')).start()
        self.addCleanup(patch.stopall)

    def create(self, data=None):
        data = data or proposal()
        # Replace ONLY the provider boundary; real extract/review/Pydantic/API flow runs.
        with patch.object(backend, 'generate_configuration', return_value=response(data)) as model:
            result = self.client.post('/api/tasks/generate', json={'requirements': REQUIREMENTS})
            model.assert_called_once()
        self.assertEqual(result.status_code, 200, result.text)
        config = result.json()['configuration']
        config['task_id'] = 'fixture-task'
        saved = self.client.post('/api/tasks/create', json=config)
        self.assertEqual(saved.status_code, 200, saved.text)
        return config

    def activate(self, status=200):
        result = self.client.post('/api/tasks/fixture-task/lifecycle', json={
            'action': 'activate', 'confirm': True, 'reviewed': True,
            'expected_updated_at': self.db.tasks['fixture-task']['updated_at'],
        })
        self.assertEqual(result.status_code, status, result.text)
        return result

    def run_engine(self, now=NOW):
        return TaskManager(self.spark, self.path).run(now=now, agents=self.agents, store=self.ledger, publisher=self.publisher)

    def test_full_natural_language_draft_activation_execution_history_and_queue(self):
        config = self.create()
        self.run_engine()  # Draft is not executed, even though it has a configured prompt.
        self.agents.ask.assert_not_called(); self.assertFalse(self.ledger.rows)
        ready = self.client.get('/api/tasks/fixture-task/readiness').json()
        self.assertTrue(ready['configuration_ready'])
        self.assertIn('待补充', ''.join(ready['warnings']))
        self.activate()
        result = self.run_engine()
        self.assertEqual(result['reports'], 1)
        self.assertEqual(self.agents.ask.call_count, 2)
        first_prompt = self.agents.ask.call_args_list[0].args
        self.assertEqual(first_prompt[0], 'genie_a1'); self.assertIn('15%', first_prompt[1])
        self.assertIn('business_date', first_prompt[1])
        rows = self.client.get('/api/reports?task_id=fixture-task&status=REPORT_CREATED').json()['runs']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['report_text'], 'Explicit offline fixture report')
        self.assertEqual(rows[0]['notification_status'], 'QUEUED')
        self.assertIn('explicit', self.publisher.call_args.args[4].lower())
        self.run_engine()
        self.assertEqual(self.agents.ask.call_count, 2)  # No replay / second inference.
        self.assertEqual(self.publisher.call_args.args[5], 0)  # No stale daily alert.

    def test_false_trigger_does_not_generate_report_or_email(self):
        self.create(); self.activate()
        self.agents.ask.side_effect = ['{"trigger":false,"reason":"fixture no decline"}']
        self.run_engine()
        self.agents.ask.assert_called_once()
        self.assertEqual(next(iter(self.ledger.rows.values()))['status'], 'NOT_TRIGGERED')
        self.assertEqual(self.publisher.call_args.args[5], 0)

    def test_failure_is_durable_not_retried_and_does_not_notify(self):
        self.create(); self.activate(); self.agents.ask.side_effect = RuntimeError('fixture secret')
        with self.assertRaises(RuntimeError): self.run_engine()
        row = next(iter(self.ledger.rows.values()))
        self.assertEqual(row['status'], 'FAILED'); self.assertEqual(row['error_code'], 'RuntimeError')
        self.assertNotIn('fixture secret', json.dumps(row)); self.publisher.assert_not_called()
        self.run_engine(); self.agents.ask.assert_called_once()

    def test_pause_return_draft_edit_reactivate_and_conflicts(self):
        self.create(); self.activate()
        version = self.db.tasks['fixture-task']['updated_at']
        stale = self.client.post('/api/tasks/fixture-task/lifecycle', json={'action': 'pause', 'confirm': True, 'expected_updated_at': 'stale'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.put('/api/tasks/fixture-task', json={'name': 'bad'}).status_code, 409)
        for action in ('pause', 'edit'):
            version = self.db.tasks['fixture-task']['updated_at']
            self.assertEqual(self.client.post('/api/tasks/fixture-task/lifecycle', json={'action': action, 'confirm': True, 'expected_updated_at': version}).status_code, 200)
        edited = self.client.put('/api/tasks/fixture-task', json={'name': "edited O'Brien\\test", 'description': ''})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.db.tasks['fixture-task']['name'], "edited O'Brien\\test")
        self.assertNotIn("O'Brien", self.db.calls[-1][0])  # Bound parameters.
        self.activate()

    def test_activation_gate_review_confirmation_and_schedule(self):
        self.create()
        with patch.dict(os.environ, {'TASK_EXECUTION_ENABLED': ''}): self.activate(503)
        for field in ('confirm', 'reviewed'):
            payload = {'action': 'activate', 'confirm': True, 'reviewed': True, 'expected_updated_at': 'version1'}
            payload[field] = False
            self.assertEqual(self.client.post('/api/tasks/fixture-task/lifecycle', json=payload).status_code, 400)
        task = self.db.tasks['fixture-task']
        for mutate in ('time', 'missing_report', 'incomplete', 'interval'):
            candidate = copy.deepcopy(task)
            if mutate == 'time': candidate['schedule']['time'] = '10:30'
            if mutate == 'missing_report': candidate['report']['prompt'] = ''
            if mutate == 'incomplete': candidate['trigger']['type'] = 'unconfigured'
            if mutate == 'interval': candidate['schedule'].update(frequency='interval', interval_days=2, start_date='')
            self.assertFalse(readiness(candidate)['configuration_ready'])
        self.agents.ask.assert_not_called()

    def test_partial_edit_cannot_remove_original_condition(self):
        self.create()
        result = self.client.put('/api/tasks/fixture-task', json={'trigger': {'type': 'always'}})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.db.tasks['fixture-task']['trigger']['type'], 'agent')

    def test_stale_edit_and_provenance_removal_are_rejected(self):
        self.create()
        stale = self.client.put('/api/tasks/fixture-task', json={'name': 'stale', 'expected_updated_at': 'old'})
        self.assertEqual(stale.status_code, 409)
        downgrade = self.client.put('/api/tasks/fixture-task', json={
            'report': {'agent': 'genie_a1', 'prompt': 'report without review'}, 'trigger': {'type': 'always'},
        })
        self.assertEqual(downgrade.status_code, 422)

    def test_broken_sql_agent_decisions_and_size_limits_fail_visibly(self):
        self.create(); self.activate()
        for answer in ('{"trigger":"false"}', '{"error":"no data"}', 'ambiguous'):
            self.ledger.rows.clear(); self.agents.ask.side_effect = [answer]
            with self.assertRaises(RuntimeError): self.run_engine()
            self.assertEqual(next(iter(self.ledger.rows.values()))['status'], 'FAILED')
        self.ledger.rows.clear()
        self.agents.ask.side_effect = ['{"trigger":true,"reason":"fixture"}', 'x' * 12001]
        with self.assertRaises(RuntimeError): self.run_engine()
        self.assertEqual(next(iter(self.ledger.rows.values()))['status'], 'FAILED')
        self.publisher.assert_not_called()

    def test_invalid_active_configuration_is_persisted_without_agent_calls(self):
        self.create(); self.activate()
        self.db.tasks['fixture-task']['trigger']['type'] = 'unconfigured'
        with self.assertRaises(RuntimeError): self.run_engine()
        row = next(iter(self.ledger.rows.values()))
        self.assertEqual(row['error_code'], 'InvalidConfiguration')
        self.agents.ask.assert_not_called()

    def test_history_errors_are_explicit_and_query_values_bound(self):
        self.create()
        self.client.get('/api/reports', params={'task_id': "x'; DROP TABLE t;--", 'status': 'FAILED'})
        sql, params = self.db.calls[-1]
        self.assertNotIn('DROP', sql); self.assertIn('DROP', params['task_id'])
        with patch.object(backend, '_exec_sql', side_effect=RuntimeError('secret')):
            result = self.client.get('/api/reports')
            self.assertEqual(result.status_code, 503); self.assertNotIn('secret', result.text)
        self.assertEqual(self.client.get('/api/reports?limit=10000').status_code, 422)

    def test_engine_disabled_is_no_op_before_adapters(self):
        self.settings['execution']['enabled'] = False
        self.path.write_text(yaml.safe_dump(self.settings))
        with self.assertRaises(RuntimeError): self.run_engine()
        self.spark.sql.assert_not_called(); self.agents.ask.assert_not_called(); self.publisher.assert_not_called()

    def test_registry_sql_and_agent_a2_use_configured_adapters(self):
        config = proposal(); config['trigger'].update(type='sql', sql='SELECT true AS should_trigger, :business_date AS reason')
        config['report']['agent'] = 'genie_a2'
        # SQL supplied by the user; generation guard still rejects invented SQL.
        self.create()
        task = self.db.tasks['fixture-task']; task['trigger'] = config['trigger']; task['report']['agent'] = 'genie_a2'
        from condition_sql import sign_test
        task['report']['condition_sql'] = {'window': 'day', 'test': {'receipt': sign_test(task['trigger']['sql'], 'day')}}
        self.activate()
        self.agents.ask.side_effect = ['Explicit offline A2 report']
        self.run_engine()
        self.assertEqual(self.agents.ask.call_args.args[0], 'genie_a2')
        sql_call = self.spark.sql.call_args
        self.assertEqual(sql_call.kwargs['args']['business_date'], (NOW.date() - __import__('datetime').timedelta(days=487)).isoformat())

    def test_outbox_failure_keeps_saved_reports_pending_without_regenerating(self):
        self.create(); self.activate(); self.publisher.side_effect = RuntimeError('offline publish failure')
        with self.assertRaises(RuntimeError): self.run_engine()
        self.assertEqual(next(iter(self.ledger.rows.values()))['notification_status'], 'NOT_QUEUED')
        self.publisher.side_effect = None
        # Recover the following day even though this weekly task is not due.
        self.run_engine(now=NOW.replace(day=20)); self.assertEqual(self.agents.ask.call_count, 2)
        self.assertEqual(self.publisher.call_args.args[5], 1)
        self.assertIn('执行日 2026-10-19', self.publisher.call_args.args[4])
        self.assertEqual(next(iter(self.ledger.rows.values()))['notification_status'], 'QUEUED')

    def test_original_yaml_report_templates_still_work(self):
        directory = self.root / 'tasks' / 'legacy'; directory.mkdir(parents=True)
        (directory / 'config.yaml').write_text(yaml.safe_dump({'task_id': 'legacy', 'name': 'legacy', 'enabled': True,
            'schedule': {'frequency': 'daily'}, 'trigger': {'type': 'always'},
            'report': {'agent': 'genie_a1', 'prompt': 'report.md', 'template': 'template.md'}}))
        (directory / 'report.md').write_text('Report {{business_date}}')
        (directory / 'template.md').write_text('# Legacy\n{{report_body}}')
        self.agents.ask.side_effect = ['Offline legacy report']
        self.run_engine()
        row = next(iter(self.ledger.rows.values()))
        self.assertEqual(row['source'], 'yaml'); self.assertEqual(row['report_text'], '# Legacy\nOffline legacy report')


class CalendarTests(unittest.TestCase):
    def slot(self, schedule, when=NOW): return due_slot({'schedule': schedule}, DISPATCH, when)
    def test_weekday_time_interval_anchor_and_timezone(self):
        self.assertIsNotNone(self.slot({'frequency': 'weekly', 'weekday': 'monday'}))
        self.assertIsNone(self.slot({'frequency': 'weekly', 'weekday': 'tuesday'}))
        self.assertIsNone(self.slot({'frequency': 'daily'}, NOW.replace(hour=0)))
        self.assertIsNotNone(self.slot({'frequency': 'interval', 'interval_days': 2, 'start_date': '2026-10-17'}))
        self.assertIsNone(self.slot({'frequency': 'interval', 'interval_days': 2, 'start_date': '2026-10-18'}))
        self.assertIsNone(self.slot({'frequency': 'daily', 'start_date': '2026-10-20'}))
        with self.assertRaises(ValueError): self.slot({'frequency': 'daily', 'timezone': 'UTC'})
        with self.assertRaises(ValueError): self.slot({'frequency': 'interval', 'interval_days': 2})
    def test_sql_contract_matches_and_rejects_write_multiple_commands(self):
        for validator in (validate_read_sql, runtime_read_sql):
            for sql in ('SELECT true AS should_trigger', 'WITH x AS (SELECT true AS ok) SELECT ok AS should_trigger FROM x'):
                validator(sql)
            for sql in ('', 'DELETE FROM t', 'SELECT 1; DROP TABLE t', 'COPY INTO t FROM \'x\'', 'CREATE TABLE t AS SELECT 1', 'SELECT * INTO t FROM x', 'SELECT * FROM ('):
                with self.subTest(sql=sql), self.assertRaises(ValueError): validator(sql)
    def test_running_claim_is_not_automatically_replayed(self):
        ledger = MemoryLedger()
        task = {'task_id': 'x', 'name': 'x', '_source': 'registry'}
        key = ledger.claim(task, 'slot', date.today(), '{}')
        self.assertEqual(ledger.rows[key]['status'], 'RUNNING')
        self.assertIsNone(ledger.claim(task, 'slot', date.today(), '{}'))
        self.assertNotEqual(run_id('yaml', 'x', 'slot'), run_id('registry', 'x', 'slot'))



class DatabaseBoundaryTests(unittest.TestCase):
    def test_statement_execution_binds_values_and_reads_all_chunks(self):
        from types import SimpleNamespace as NS
        first = NS(status=NS(state='SUCCEEDED'), statement_id='fixture',
                   manifest=NS(truncated=False, schema=NS(columns=[NS(name='run_id')])),
                   result=NS(data_array=[['first']], next_chunk_index=1))
        api = Mock()
        api.execute_statement.return_value = first
        api.get_statement_result_chunk_n.return_value = NS(data_array=[['second']], next_chunk_index=None)
        with patch.object(backend, '_workspace', return_value=NS(statement_execution=api)):
            rows = backend._exec_sql('SELECT :value', {'value': "O'Brien\\test"})
            self.assertEqual(rows, [{'run_id': 'first'}, {'run_id': 'second'}])
            params = api.execute_statement.call_args.kwargs['parameters']
            self.assertEqual(params[0].value, "O'Brien\\test")
            api.get_statement_result_chunk_n.assert_called_once_with('fixture', 1)
            first.manifest.truncated = True
            with self.assertRaises(RuntimeError): backend._exec_sql('SELECT 1')

    def test_run_store_statements_parse_and_keep_claims_immutable(self):
        import sqlglot
        records, calls = {}, []
        def sql(statement, args=None):
            # Parse generated SQL in the same dialect without connecting to Spark.
            sqlglot.parse_one(statement, read='databricks')
            calls.append((statement, args))
            if statement.startswith('MERGE'):
                records.setdefault(args['run_id'], args['attempt'])
                return Rows([])
            if statement.startswith('SELECT attempt_id'):
                return Rows([{'attempt_id': records[args['key']]}])
            return Rows([])
        store = RunStore(Mock(sql=sql), 'workspace.douyin.business_task_runs_v1')
        task = {'task_id': "x';DROP", 'name': 'name', '_source': 'registry'}
        key = store.claim(task, 'slot', date(2026, 10, 19), '{}')
        self.assertIsNotNone(key)
        self.assertIsNone(store.claim(task, 'slot', date(2026, 10, 19), '{}'))
        self.assertNotIn("x';DROP", calls[0][0]); self.assertEqual(calls[0][1]['task_id'], "x';DROP")
        store.finish(key, 'NOT_TRIGGERED', reason='fixture')
        self.assertIn("status='RUNNING'", calls[-1][0])
        store.pending_reports(date(2026, 10, 19))
        store.mark_queued([key, 'another-key'])
        self.assertEqual(len(calls[-1][1]), 2)

if __name__ == '__main__': unittest.main()

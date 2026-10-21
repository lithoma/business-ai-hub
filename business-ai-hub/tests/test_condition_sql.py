"""Focused offline checks for SQL safety, one query, review activation and date reuse."""
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import os
import unittest
from fastapi.testclient import TestClient
import app as backend
from condition_sql import validate_sql, parameters, generate_with_genie, execute_readonly, sign_test, verified_test
from task_execution import readiness
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'business-task-manager-v1'/'business-task-manager-v1'))
from registry_tasks import validate_read_sql, condition_parameters

QUERY='SELECT SUM(gmv) < 100 AS should_trigger, \'fixture\' AS reason FROM workspace.douyin.approved WHERE day >= :period_start'


class ConditionSQLTests(unittest.TestCase):
    def test_readonly_gate_blocks_writes_multi_statement_and_hidden_function_calls(self):
        for validate in (validate_sql,validate_read_sql):
            validate(QUERY)
            validate('SELECT MEASURE(gmv)>0 AS should_trigger FROM workspace.douyin.metric_view')
            for query in ('DELETE FROM x','SELECT true AS should_trigger; DROP TABLE x',
                          "SELECT ai_query('x','y') AS should_trigger",'SELECT c.s.side_effect() AS should_trigger',
                          "SELECT true AS should_trigger FROM read_files('/x')",'SELECT * INTO x FROM y'):
                with self.subTest(query=query),self.assertRaises(ValueError): validate(query)

    def test_one_genie_call_extracts_sql_and_parameterizes_dates_without_warehouse_call(self):
        query="SELECT SUM(gmv)<100 AS should_trigger FROM workspace.douyin.approved WHERE day >= '2025-06-09'"
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.return_value=NS(attachments=[NS(query=NS(query=query))],conversation_id='fixture-c',message_id='fixture-m')
        values=parameters('2025-06-20','week')
        generated,source=generate_with_genie(workspace,'fixture-space','GMV低于100',values)
        self.assertIn(':period_start',generated)
        self.assertTrue(source['upstream_read_query'])
        workspace.genie.start_conversation_and_wait.assert_called_once()
        workspace.statement_execution.execute_statement.assert_not_called()

    def test_genie_safe_division_and_same_day_parameter_roles_are_preserved(self):
        # Regression for the completed user query rejected as an unknown function.
        query="""WITH params AS (SELECT CAST('2025-06-21' AS DATE) AS business_date,
            CAST('2025-06-21' AS DATE) AS period_start, CAST('2025-06-21' AS DATE) AS period_end,
            CAST('2025-06-20' AS DATE) AS previous_period_start, CAST('2025-06-20' AS DATE) AS previous_period_end)
            SELECT try_divide(SUM(gmv), 100) < -0.1 AS should_trigger
            FROM approved WHERE date BETWEEN CAST('2025-06-21' AS DATE) AND CAST('2025-06-21' AS DATE)
            OR date BETWEEN CAST('2025-06-20' AS DATE) AND CAST('2025-06-20' AS DATE)"""
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.return_value=NS(attachments=[NS(query=NS(query=query))],conversation_id='fixture',message_id='fixture')
        generated,_=generate_with_genie(workspace,'fixture','GMV下降超过10%',parameters('2025-06-21','day'))
        for validate in (validate_sql,validate_read_sql):
            tree=validate(generated)
            from sqlglot import exp
            self.assertEqual({n.name for n in tree.find_all(exp.Placeholder)},set(parameters('2025-06-21','day')))
            for bad in ('SELECT c.s.try_divide(1,2) AS should_trigger',
                        'SELECT try_divide(1,2,3) AS should_trigger'):
                with self.assertRaises(ValueError): validate(bad)
        self.assertIn('BETWEEN CAST(:period_start AS DATE) AND CAST(:period_end AS DATE)',generated)
        self.assertIn('BETWEEN CAST(:previous_period_start AS DATE) AND CAST(:previous_period_end AS DATE)',generated)
        workspace.genie.start_conversation_and_wait.assert_called_once()
        workspace.statement_execution.execute_statement.assert_not_called()

    def test_genie_clarification_is_shown_without_fabricated_sql_or_retry(self):
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.return_value=NS(attachments=[NS(query=None,text=NS(content='请明确异常规则或阈值。'))])
        with self.assertRaisesRegex(ValueError,'请明确异常规则'):
            generate_with_genie(workspace,'fixture','销售异常',parameters('2025-06-21','day'))
        workspace.genie.start_conversation_and_wait.assert_called_once()
        workspace.statement_execution.execute_statement.assert_not_called()

    def test_condition_query_has_one_bounded_statement_and_strict_boolean_result(self):
        workspace=Mock()
        response=NS(status=NS(state='SUCCEEDED'),manifest=NS(truncated=False,schema=NS(columns=[NS(name='should_trigger',type_name='BOOLEAN'),NS(name='reason',type_name='STRING')])),result=NS(data_array=[['false','no decline']],next_chunk_index=None),statement_id='fixture')
        workspace.statement_execution.execute_statement.return_value=response
        result=execute_readonly(workspace,'warehouse',QUERY,parameters('2025-06-20','week'))
        self.assertIs(result['should_trigger'],False)
        call=workspace.statement_execution.execute_statement
        call.assert_called_once(); self.assertEqual(call.call_args.kwargs['row_limit'],2)
        self.assertEqual(call.call_args.kwargs['wait_timeout'],'50s')
        response.result.data_array=[[None,'前一日没有数据']]
        with self.assertRaisesRegex(ValueError,'条件无法验证：前一日没有数据'):
            execute_readonly(workspace,'warehouse',QUERY,parameters('2025-06-20','week'))
        response.manifest.schema.columns[0].type_name='STRING'
        with self.assertRaises(ValueError): execute_readonly(workspace,'warehouse',QUERY,parameters('2025-06-20','week'))

    def test_write_is_rejected_before_client_access_and_test_never_saves_or_calls_genie(self):
        client=TestClient(backend.app)
        with patch.dict(os.environ,{'TASK_CONDITION_SQL_ENABLED':'true'}), \
             patch.object(backend,'_ws_read_yaml',return_value={'schedule':{'business_date_offset_days':487}}), \
             patch.object(backend,'_condition_workspace',side_effect=AssertionError('No remote client')) as remote:
            result=client.post('/api/conditions/sql/test',json={'sql':'DELETE FROM x','confirm':True})
            self.assertEqual(result.status_code,422); remote.assert_not_called()
        with patch.dict(os.environ,{'TASK_CONDITION_SQL_ENABLED':'true'}), \
             patch.object(backend,'_ws_read_yaml',return_value={}), \
             patch.object(backend,'_condition_workspace',return_value=Mock()), \
             patch('condition_sql.execute_readonly',return_value={'should_trigger':True,'reason':'fixture','statement_id':'fixture'}) as execute, \
             patch.object(backend,'_exec_sql',side_effect=AssertionError('No writes')):
            result=client.post('/api/conditions/sql/test',json={'sql':QUERY,'window':'week','confirm':True,'business_date':'2025-06-20'})
            self.assertEqual(result.status_code,200,result.text); execute.assert_called_once()
            self.assertTrue(verified_test(QUERY,result.json()))

    def test_sql_activation_requires_reviewed_successful_test_and_rejects_changed_sql(self):
        task={'schedule':{'frequency':'weekly','weekday':'friday'},'trigger':{'type':'sql','sql':QUERY},
              'report':{'agent':'genie_a1','prompt':'Report'}}
        with patch.dict(os.environ,{'TASK_EXECUTION_ENABLED':'true'}):
            self.assertFalse(readiness(task)['can_activate'])
            task['report']['condition_sql']={'window':'week','test':{'receipt':sign_test(QUERY,'week')}}
            self.assertTrue(readiness(task)['can_activate'])
            task['trigger']['sql']=QUERY.replace('< 100','< 200')
            self.assertFalse(readiness(task)['can_activate'])

    def test_current_and_previous_periods_are_recomputed_consistently_for_execution(self):
        for window in ('day','week','rolling7'):
            self.assertEqual(parameters('2025-06-20',window),condition_parameters('2025-06-20',window))
        self.assertEqual(parameters('2025-06-20','week')['period_start'],'2025-06-09')
        self.assertEqual(parameters('2025-06-20','week')['period_end'],'2025-06-15')
        self.assertNotEqual(parameters('2025-06-20','week'),parameters('2025-06-27','week'))


if __name__=='__main__': unittest.main()

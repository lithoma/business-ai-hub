"""Offline cross-domain evidence handoff and mixed-period regression checks."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import os
import sys
from pathlib import Path
import unittest
from fastapi.testclient import TestClient
import app as backend
from condition_sql import parameters
from condition_decision import gather_context
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'business-task-manager-v1/business-task-manager-v1'))
from registry_tasks import condition_parameters, validate_read_sql


class ConditionContextTests(unittest.TestCase):
    def test_original_business_context_reaches_genie_and_other_domain_is_consulted_once(self):
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.side_effect=[
            NS(status='COMPLETED',attachments=[NS(text=NS(content='fixture metric definition'),query=NS(query='SELECT SUM(value) FROM approved_metric_view'))],conversation_id='context-c',message_id='context-m'),
            NS(attachments=[NS(query=NS(query='SELECT false AS should_trigger'))],conversation_id='primary-c',message_id='primary-m')]
        client=TestClient(backend.app)
        with patch.dict(os.environ,{'TASK_CONDITION_SQL_ENABLED':'true'}), \
             patch.object(backend,'_ws_read_yaml',return_value={}), \
             patch.object(backend,'_condition_workspace',return_value=workspace), \
             patch.object(backend,'_exec_sql',side_effect=AssertionError('No Registry writes')):
            response=client.post('/api/conditions/sql/generate',json={'condition':'昨日高于上周平均值',
                'original_request':'fixture品牌的GMV；销售与竞品联合比较','agent':'genie_a1',
                'conversation':[{'role':'user','content':'fixture历史业务口径：指定品牌的净GMV'}],
                'context_agents':['genie_a1','genie_a2'],'window':'day_vs_week','business_date':'2025-06-20'})
            self.assertEqual(response.status_code,200,response.text)
            calls=workspace.genie.start_conversation_and_wait.call_args_list
            self.assertEqual(len(calls),2)
            self.assertEqual([c.kwargs['space_id'] for c in calls],[backend.GENIE_SPACES['genie_a2'],backend.GENIE_SPACES['genie_a1']])
            self.assertIn('fixture品牌的GMV',calls[1].kwargs['content'])
            self.assertIn('fixture历史业务口径',calls[1].kwargs['content'])
            self.assertIn('fixture metric definition',calls[1].kwargs['content'])
            self.assertIn('approved_metric_view',calls[1].kwargs['content'])
            self.assertEqual(response.json()['review']['context_sources'][0]['conversation_id'],'context-c')
            workspace.statement_execution.execute_statement.assert_not_called()

    def test_unavailable_other_domain_stops_without_silent_fallback_or_retry(self):
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.return_value=NS(status='FAILED')
        with self.assertRaises(ValueError):
            gather_context(workspace,{'genie_a1':'a1','genie_a2':'a2'},['genie_a1','genie_a2'],
                'genie_a1','joint condition',parameters('2025-06-20','day_vs_week'),validate_read_sql)
        workspace.genie.start_conversation_and_wait.assert_called_once()

    def test_yesterday_vs_previous_calendar_week_is_not_previous_day_and_runtime_matches(self):
        for day in ('2025-06-20','2025-06-22','2025-06-23'):
            values=parameters(day,'day_vs_week')
            self.assertEqual(values,condition_parameters(day,'day_vs_week'))
            self.assertEqual(values['period_start'],day)
            self.assertEqual(values['period_end'],day)
        values=parameters('2025-06-20','day_vs_week')
        self.assertEqual(values['previous_period_start'],'2025-06-09')
        self.assertEqual(values['previous_period_end'],'2025-06-15')


if __name__=='__main__': unittest.main()

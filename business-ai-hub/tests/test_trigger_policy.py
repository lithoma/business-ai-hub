"""Focused offline checks for trigger routing and the AI condition review gate."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import app as backend
from condition_decision import parse_boolean_decision
from condition_sql import verified_test
from task_drafts import TaskProposal, review_proposal
from task_execution import readiness
from test_task_drafts import proposal, REQUIREMENTS


class TriggerPolicyTests(unittest.TestCase):
    def test_vague_anomaly_has_reviewable_ai_prompt_but_numeric_rules_stay_sql(self):
        task=TaskProposal.model_validate(proposal())
        task.trigger.type='sql'
        task.trigger.prompt='检查善存品牌每日 GMV 或销量是否异常'
        review=review_proposal(task,'每天善存销售异常时生成报告')
        self.assertEqual(task.trigger.type,'agent')
        self.assertEqual(task.trigger.sql,'')
        self.assertIn('不得自行设定阈值',task.trigger.prompt)
        self.assertIn('{"error":',task.trigger.prompt)
        self.assertTrue(any('异常判定规则' in item for item in review.missing_information))
        task=TaskProposal.model_validate(proposal())
        task.trigger.prompt='异常定义为 GMV 相比前一天下降超过10%'
        review_proposal(task,'每天GMV下降超过10%时生成报告')
        self.assertEqual(task.trigger.type,'sql')
        task=TaskProposal.model_validate(proposal())
        task.trigger.type='unconfigured'; task.trigger.prompt='出现异常时报告，尚不清楚监控对象'
        review_proposal(task,'出现异常就报告')
        self.assertEqual(task.trigger.type,'unconfigured')

    def test_deterministic_conditions_use_sql_but_qualitative_and_unconditional_stay_distinct(self):
        for condition in ('善存上一周 GMV 环比下降超过15%，分析风险原因',
                          '检查前一日指标是否高于上周平均值',
                          'GMV drops by more than 15% compared with previous week',
                          '存在竞品新品记录'):
            task=TaskProposal.model_validate(proposal())
            task.trigger.prompt=condition
            review=review_proposal(task, REQUIREMENTS)
            self.assertEqual(task.trigger.type,'sql')
            self.assertEqual(task.trigger.sql,'')
            self.assertEqual(task.trigger.prompt,condition)
            self.assertTrue(any('SQL' in warning for warning in review.warnings))
        task=TaskProposal.model_validate(proposal())
        task.trigger.prompt='结合公告判断竞品策略是否出现实质变化，说明依据。'
        review_proposal(task, '如果竞品策略出现实质变化，生成报告')
        self.assertEqual(task.trigger.type,'agent')
        task.trigger.type='always'; task.conditional=False; task.trigger.prompt=''
        review_proposal(task, '每周五生成销售风险分析报告')
        self.assertEqual(task.trigger.type,'always')

    def test_ai_unknown_is_an_error_and_test_matches_executor_contract(self):
        self.assertEqual(parse_boolean_decision('{"trigger":false,"reason":"本期无策略变化"}'),(False,'本期无策略变化'))
        for answer in ('{"trigger":"false","reason":"x"}', '{"error":"数据不足"}',
                       '{"trigger":false,"reason":""}', '{"trigger":false,"reason":"x","error":"missing data"}'):
            with self.subTest(answer=answer),self.assertRaises(ValueError): parse_boolean_decision(answer)
        root=Path(__file__).resolve().parents[2]
        self.assertEqual((root/'business-ai-hub/condition_decision.py').read_bytes(),
                         (root/'business-task-manager-v1/business-task-manager-v1/condition_decision.py').read_bytes())
        sys.path.insert(0,str(root/'business-task-manager-v1/business-task-manager-v1'))
        from task_engine import TaskManager
        engine=TaskManager.__new__(TaskManager)
        engine.agents=Mock()
        engine.agents.ask.return_value='{"trigger":false,"reason":"本期无策略变化"}'
        result=engine._trigger({'task_id':'fixture','_source':'registry',
            'trigger':{'type':'agent','agent':'genie_a2','prompt':'策略是否有实质变化'}},root,
            {'business_date':'2025-06-20','period_start':'2025-06-09','period_end':'2025-06-15'})
        self.assertEqual(result,(False,'本期无策略变化'))
        engine.agents.ask.assert_called_once()

    def test_explicit_single_ai_test_does_not_save_and_receipt_binds_agent_prompt_and_window(self):
        workspace=Mock()
        workspace.genie.start_conversation_and_wait.return_value=NS(status='COMPLETED',
            attachments=[NS(text=NS(content='{"trigger":false,"reason":"fixture: no strategy change"}'))],
            conversation_id='fixture-conversation',message_id='fixture-message')
        task={'schedule':{'frequency':'weekly','weekday':'friday'},
              'trigger':{'type':'agent','agent':'genie_a2','prompt':'竞品策略是否有实质变化'},
              'report':{'agent':'genie_a2','prompt':'报告'}}
        client=TestClient(backend.app)
        with patch.dict(os.environ,{'TASK_CONDITION_SQL_ENABLED':'true','TASK_EXECUTION_ENABLED':'true'}), \
             patch.object(backend,'_ws_read_yaml',return_value={}), \
             patch.object(backend,'_condition_workspace',return_value=workspace), \
             patch.object(backend,'_exec_sql',side_effect=AssertionError('No Registry writes')):
            self.assertFalse(readiness(task)['can_activate'])
            request={'condition':task['trigger']['prompt'],'agent':'genie_a2','window':'week','business_date':'2025-06-20'}
            self.assertEqual(client.post('/api/conditions/agent/test',json=request).status_code,400)
            workspace.genie.start_conversation_and_wait.assert_not_called()
            result=client.post('/api/conditions/agent/test',json=dict(request,confirm=True))
            self.assertEqual(result.status_code,200,result.text)
            workspace.genie.start_conversation_and_wait.assert_called_once()
            workspace.statement_execution.execute_statement.assert_not_called()
            self.assertFalse(result.json()['test']['should_trigger'])
            self.assertIn('2025-06-09',workspace.genie.start_conversation_and_wait.call_args.kwargs['content'])
            task['report']['condition_agent']=result.json()
            self.assertTrue(readiness(task)['can_activate'])
            for field,value in (('agent','genie_a1'),('prompt','更改判断规则'),('context_agents',['genie_a1','genie_a2'])):
                changed={**task,'trigger':{**task['trigger'],field:value}}
                self.assertFalse(readiness(changed)['can_activate'])
            self.assertFalse(verified_test('SELECT true AS should_trigger',result.json()))
            task['report']['condition_agent']['window']='day'
            self.assertFalse(readiness(task)['can_activate'])


if __name__=='__main__': unittest.main()

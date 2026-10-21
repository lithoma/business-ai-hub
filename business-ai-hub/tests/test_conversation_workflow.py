"""Focused offline workflow checks. Model/Genie output is fixtures only."""
import json
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as backend
from conversation_workflow import AnalysisTickets
from genie_one_mcp import wait_for_answer
from test_task_drafts import proposal, response, REQUIREMENTS


def plan(intent='mixed', dependent=False):
    return {'intent':intent,'execution_order':'analysis_first' if dependent else 'independent',
            'analysis_question':'分析善存上一周与前一周的GMV。' if intent in ('analysis','mixed') else '',
            'analysis_agent':'genie_a1', 'task_requirements':REQUIREMENTS if intent in ('task','mixed') else '',
            'task_proposals':[proposal()] if intent in ('task','mixed') and not dependent else [],
            'clarification':''}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)
        self.workspace = NS(config=NS(host='https://fixture.invalid',authenticate=lambda: {}))
        self.env = patch.dict(os.environ, {'HUB_LOCAL_WORKFLOW':'true','TASK_PARSER_ENDPOINT':'fixture-model'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.ws = patch.object(backend,'_workspace',return_value=self.workspace).start()
        self.sql = patch.object(backend,'_exec_sql',side_effect=AssertionError('No persistence during routing')).start()
        self.genie = patch.object(backend,'_ask_genie',side_effect=AssertionError('No implicit Genie calls')).start()
        patch.object(backend,'_analysis_tickets',AnalysisTickets()).start()
        self.transport = patch('task_llm.requests.post').start()
        self.addCleanup(patch.stopall)

    def route(self, value, request=REQUIREMENTS):
        self.transport.return_value = Mock(status_code=200,json=lambda:response(value))
        return self.client.post('/api/conversation/route',json={'requirements':request,'source_agent':'genie_a1'})

    def test_one_call_routes_extracts_and_hands_off_only_analysis_once(self):
        for intent, dependent in (('analysis',False),('task',False),('mixed',False),('mixed',True)):
            with self.subTest(intent=intent,dependent=dependent):
                self.transport.reset_mock()
                result = self.route(plan(intent,dependent),'分析善存GMV' if intent=='analysis' else REQUIREMENTS)
                self.assertEqual(result.status_code,200,result.text)
                body = result.json()
                self.transport.assert_called_once()
                self.sql.assert_not_called(); self.genie.assert_not_called()
                if body['configuration']:
                    self.assertFalse(body['configuration']['enabled'])
                    self.assertEqual(body['configuration']['trigger']['type'],'sql')
                self.assertEqual(bool(body['configuration']),intent in ('task','mixed') and not dependent)
                if body['analysis_ticket']:
                    with patch.object(backend,'_ask_workflow_analysis',return_value={'answer':'fixture only'}) as analysis:
                        payload={'ticket':body['analysis_ticket']}
                        self.assertEqual(self.client.post('/api/conversation/analysis',json=payload).status_code,200)
                        self.assertEqual(self.client.post('/api/conversation/analysis',json=payload).status_code,409)
                        analysis.assert_called_once_with('分析善存上一周与前一周的GMV。','genie_a1',None)
                self.assertEqual(self.transport.call_count,1)

    def test_unsafe_or_incomplete_routing_never_reaches_genie(self):
        misroute = self.route(plan('analysis'),'创建每周任务发邮件给我')
        self.assertEqual(misroute.status_code,200)
        self.assertEqual(misroute.json()['intent'],'clarify')
        unsafe = plan(); unsafe['analysis_question']='创建一个每周任务并发送邮件'
        self.assertEqual(self.route(unsafe).status_code,502)
        missing = plan(); del missing['task_proposals'][0]['schedule']['time']
        self.assertEqual(self.route(missing).status_code,502)
        self.assertEqual(self.client.post('/api/chat',json={'question':'创建定时任务','agent':'genie_a1'}).status_code,409)
        self.genie.assert_not_called(); self.sql.assert_not_called()

    def test_missing_model_and_genie_one_gate_fail_without_fallback(self):
        with patch.dict(os.environ,{'TASK_PARSER_ENDPOINT':'','CONVERSATION_ROUTER_ENDPOINT':''}):
            result = self.client.post('/api/conversation/route',json={'requirements':'分析销售'})
            self.assertEqual(result.status_code,503)
            self.assertIn('Model not configured',result.text)
            self.transport.assert_not_called()
        with patch.dict(os.environ,{'CHAT_ANALYSIS_PROVIDER':'genie_one','GENIE_ONE_CHAT_ENABLED':'false'}):
            with self.assertRaises(HTTPException) as caught:
                backend._ask_workflow_analysis('分析销售','genie_a1',None)
            self.assertEqual(caught.exception.status_code,503)
        self.genie.assert_not_called()

    def test_genie_one_polls_without_a_second_ask(self):
        adapter = Mock(caller=object())
        adapter.ask.return_value={'structured_content':{'conversation_id':'fixture-c','response_id':'fixture-r'}}
        adapter.poll.side_effect=[{'structured_content':{'status':'in_progress'}},
                                  {'structured_content':{'status':'completed','final_answer':'fixture only'}}]
        with patch('genie_one_mcp.time.sleep'):
            result=wait_for_answer(adapter,'分析销售')
        self.assertEqual(result['answer'],'fixture only')
        adapter.ask.assert_called_once()
        self.assertEqual(adapter.poll.call_count,2)


if __name__ == '__main__':
    unittest.main()

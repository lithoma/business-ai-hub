"""Two offline checks for real recording paths, not live paid-provider benchmarks."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi.testclient import TestClient
import app as backend
from genie_one_mcp import Caller, GenieOneMCP, wait_for_answer
from test_conversation_workflow import plan
from test_task_drafts import response


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='hub-measurements-')
        self.addCleanup(self.directory.cleanup)
        self.database=str(Path(self.directory.name)/'metrics.sqlite3')
        self.env=patch.dict(os.environ,{'HUB_TEST_METRICS_DB':self.database,'HUB_LOCAL_WORKFLOW':'true',
                                        'TASK_PARSER_ENDPOINT':'fixture-model'})
        self.env.start();self.addCleanup(self.env.stop)
        self.client=TestClient(backend.app)
        self.workspace=NS(config=NS(host='https://fixture.invalid',authenticate=lambda:{'Authorization':'NEVER_RECORD_THIS_TOKEN'}))

    def test_model_and_mcp_calls_persist_with_one_trace_and_partial_usage(self):
        raw=response(plan('analysis'));raw['usage']={'prompt_tokens':17,'completion_tokens':9,'total_tokens':26}
        transport=Mock()
        transport.list_tools.return_value=[{'name':name,'inputSchema':{'type':'object'}} for name in ('genie_ask','genie_poll_response')]
        transport.call_tool.side_effect=[{'structuredContent':{'conversation_id':'fixture-c','response_id':'fixture-r'}},
                                         {'structuredContent':{'status':'completed','final_answer':'DO_NOT_RECORD_ANSWER'}}]
        adapter=GenieOneMCP(self.workspace,Caller('fixture-local','local_user'),transport=transport)
        trace=str(uuid.uuid4());headers={'X-Hub-Trace-Id':trace}
        def analysis(question,agent,conversation):
            return wait_for_answer(adapter,question)
        with patch.object(backend,'_workspace',return_value=self.workspace), \
             patch('task_llm.requests.post',return_value=Mock(status_code=200,json=lambda:raw)) as model, \
             patch.object(backend,'_ask_workflow_analysis',side_effect=analysis):
            first=self.client.post('/api/conversation/route',headers=headers,json={'requirements':'DO_NOT_RECORD_QUESTION'})
            self.assertEqual(first.status_code,200,first.text)
            second=self.client.post('/api/conversation/analysis',headers=headers,json={'ticket':first.json()['analysis_ticket']})
            self.assertEqual(second.status_code,200,second.text)
            model.assert_called_once();self.assertEqual(transport.call_tool.call_count,2)
        export=self.client.get('/api/test-metrics/export').json()
        self.assertEqual({r['trace_id'] for r in export['requests']},{trace})
        self.assertEqual(export['summary']['requests'],2)
        self.assertEqual(export['summary']['reported_inference_tokens']['total_tokens'],26)
        self.assertEqual(export['summary']['observed_application_calls']['mcp_tool:genie_ask'],1)
        self.assertEqual(export['summary']['observed_application_calls']['mcp_tool:genie_poll_response'],1)
        self.assertIsNone(export['summary']['genie_internal_tokens'])
        self.assertIsNone(export['summary']['actual_cost'])
        self.assertTrue(all(r['duration_ms']>=0 for r in export['requests']))
        serialized=json.dumps(export)
        for private in ('DO_NOT_RECORD_QUESTION','DO_NOT_RECORD_ANSWER','NEVER_RECORD_THIS_TOKEN'):
            self.assertNotIn(private,serialized)
        # A fresh export reads the same persisted rows; export itself adds no test request.
        self.assertEqual(self.client.get('/api/test-metrics/export').json()['summary']['requests'],2)

    def test_failed_call_is_recorded_without_retry_or_invented_zero_usage(self):
        with patch.object(backend,'_workspace',return_value=self.workspace), \
             patch('task_llm.requests.post',return_value=Mock(status_code=403,json=lambda:{'error_code':'PERMISSION_DENIED'})) as model:
            result=self.client.post('/api/conversation/route',json={'requirements':'fixture'})
            self.assertEqual(result.status_code,403)
            model.assert_called_once()
        export=self.client.get('/api/test-metrics/export').json()
        self.assertEqual(export['summary']['failed_requests'],1)
        self.assertIsNone(export['summary']['reported_inference_tokens']['total_tokens'])
        inference=next(row for row in export['spans'] if row['kind']=='inference')
        self.assertEqual(inference['outcome'],'error')
        self.assertEqual(inference['details']['http_status'],403)
        self.assertEqual(inference['error_type'],'LLMAccessError')


if __name__=='__main__': unittest.main()

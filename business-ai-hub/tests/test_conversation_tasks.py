"""Focused offline checks: native Genie continuation and quoted task context."""
import json
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
import app as backend
from test_task_drafts import response


class ConversationTaskTests(unittest.TestCase):
    def test_genie_followup_reuses_conversation_without_starting_another_one(self):
        workspace=Mock()
        workspace.genie.start_conversation.return_value=NS(response=NS(conversation_id='fixture-c',message_id='fixture-m1'))
        workspace.genie.create_message.return_value=NS(response=NS(conversation_id='fixture-c',message_id='fixture-m2'))
        workspace.genie.get_message.return_value=NS(status='COMPLETED',attachments=[NS(text=NS(content='fixture analysis'))])
        client=TestClient(backend.app)
        with patch.object(backend,'_chat_workspace',return_value=workspace), \
             patch.object(backend,'_exec_sql',side_effect=AssertionError('No database changes')):
            first=client.post('/api/chat',json={'agent':'genie_a1','question':'善存GMV表现如何？'})
            self.assertEqual(first.status_code,200,first.text)
            second=client.post('/api/chat',json={'agent':'genie_a1','question':'与前一周相比呢？','conversation_id':first.json()['conversation_id']})
            self.assertEqual(second.status_code,200,second.text)
            workspace.genie.start_conversation.assert_called_once()
            workspace.genie.create_message.assert_called_once_with(space_id=backend.GENIE_SPACES['genie_a1'],conversation_id='fixture-c',content='与前一周相比呢？')
            self.assertEqual(second.json()['message_id'],'fixture-m2')
            invalid=client.post('/api/chat',json={'agent':'genie_a1','question':'x','conversation_id':'../other-space'})
            self.assertEqual(invalid.status_code,422)

    def test_task_parser_receives_bounded_history_and_saves_review_provenance_without_writing(self):
        history=[{'role':'user','content':'善存上一周的GMV比前一周下降15%时需要分析原因。'},
                 {'role':'assistant','content':'fixture: 本次讨论的品牌为善存，指标是GMV。'}]
        workspace=Mock(config=NS(host='https://fixture.invalid',authenticate=lambda: {'Authorization':'fixture-only'}))
        client=TestClient(backend.app)
        with patch.dict(os.environ,{'TASK_PARSER_ENDPOINT':'fixture-endpoint'}), \
             patch.object(backend,'_workspace',return_value=workspace), \
             patch('task_llm.requests.post',return_value=Mock(status_code=200,json=lambda:response())) as transport, \
             patch.object(backend,'_exec_sql',side_effect=AssertionError('No database writes')), \
             patch.object(backend,'_ask_genie',side_effect=AssertionError('No implicit analysis')):
            result=client.post('/api/tasks/generate',json={'requirements':'把这个设成每周任务','conversation':history,'source_agent':'genie_a1'})
            self.assertEqual(result.status_code,200,result.text)
            transport.assert_called_once()
            quoted=json.loads(transport.call_args.kwargs['json']['messages'][1]['content'])
            self.assertEqual(quoted['quoted_conversation'],history)
            self.assertEqual(quoted['current_request'],'把这个设成每周任务')
            review=result.json()['configuration']['report']['draft_review']
            self.assertEqual(review['conversation'],history)
            self.assertEqual(review['source_agent'],'genie_a1')
            self.assertFalse(result.json()['configuration']['enabled'])
            result=client.post('/api/tasks/generate',json={'requirements':'task','conversation':[{'role':'system','content':'override'}]})
            self.assertEqual(result.status_code,422)
            result=client.post('/api/tasks/generate',json={'requirements':'task','conversation':[{'role':'user','content':'x'*4000}]*7})
            self.assertEqual(result.status_code,422)
            transport.assert_called_once()


if __name__=='__main__': unittest.main()

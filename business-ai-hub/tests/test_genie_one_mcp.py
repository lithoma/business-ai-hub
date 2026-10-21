"""Only offline MCP fixtures; never runtime mocks or live inference."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest
from genie_one_mcp import Caller, GenieOneMCP, GenieOneError


class GenieOneMCPTests(unittest.TestCase):
    def adapter(self):
        transport=Mock()
        transport.list_tools.return_value=[{'name':name,'inputSchema':{
            'type':'object','properties':{'fixture_input':{'type':'string'}},
            'required':['fixture_input'],'additionalProperties':False}}
            for name in ('genie_ask','genie_poll_response','genie_cancel_response','genie_get_query_result')]
        caller=Caller('fixture-user-a','obo_user')
        return GenieOneMCP(NS(config=NS(host='https://fixture.invalid')),caller,transport=transport),transport,caller

    def test_discovery_never_calls_a_tool_and_only_uses_workspace_gateway(self):
        adapter,transport,caller=self.adapter()
        self.assertIn('genie_ask',adapter.discover())
        transport.call_tool.assert_not_called()
        for url in ('https://other.invalid/ai-gateway/mcp-services/system.ai.genie_one_mcp',
                    'https://fixture.invalid/api/2.0/mcp/genie'):
            with self.assertRaises(ValueError): GenieOneMCP(adapter._workspace,caller,server_url=url)

    def test_one_tool_call_uses_actual_discovered_schema_and_preserves_structured_result(self):
        adapter,transport,caller=self.adapter()
        transport.call_tool.return_value={'structuredContent':{'status':'in_progress','conversation_id':'fixture'},'content':[]}
        result=adapter.invoke('genie_ask',{'fixture_input':'fixture question'},caller=caller)
        self.assertEqual(result['structured_content']['status'],'in_progress')
        transport.call_tool.assert_called_once_with('genie_ask',{'fixture_input':'fixture question'})
        for tool,args,error in (('genie_ask',{'question':'guessed field'},ValueError),
                                ('delete_table',{},ValueError)):
            with self.assertRaises(error): adapter.invoke(tool,args,caller=caller)
        transport.call_tool.assert_called_once()

    def test_other_principal_and_provider_error_fail_closed_without_retry(self):
        adapter,transport,caller=self.adapter()
        with self.assertRaises(PermissionError):
            adapter.invoke('genie_ask',{'fixture_input':'x'},caller=Caller('other','obo_user'))
        transport.list_tools.assert_not_called(); transport.call_tool.assert_not_called()
        transport.call_tool.return_value={'isError':True,'content':[{'type':'text','text':'fixture secret'}]}
        with self.assertRaises(GenieOneError) as caught:
            adapter.invoke('genie_poll_response',{'fixture_input':'x'},caller=caller)
        self.assertNotIn('fixture secret',str(caught.exception))
        transport.call_tool.assert_called_once()


if __name__=='__main__': unittest.main()

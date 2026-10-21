"""One business provider. No per-Space IDs, shared-identity fallback or extra router."""
from condition_decision import parse_boolean_decision


class AgentClient:
    def __init__(self, config, *, workspace=None):
        self.config = config
        self._workspace = workspace

    @property
    def workspace(self):
        if self._workspace is None:
            from hub_auth import business_workspace
            return business_workspace()
        return self._workspace

    def ask(self, agent_name, instruction):
        if agent_name != 'genie_one':
            raise ValueError('Only Genie One is supported; migrate the task before execution')
        from genie_one_mcp import adapter_for_user, wait_for_answer
        # The Hub verifies request/worker identity; a generic Job identity is not accepted.
        prompt = ('仅执行一次分析或条件判断；不创建任务、Alert、Job、调度或发送邮件。\n'
                  + instruction)
        return wait_for_answer(adapter_for_user(self.workspace), prompt)['answer']

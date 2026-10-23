"""Replaceable creator credential resolver for scheduled execution.

Resolve a verified Identity from an approved company credential broker. There is no
implicit CLI/App-SP fallback. The default Job entrypoint supplies no resolver and
therefore cannot run user-owned tasks. No browser token is persisted by this module.
"""
from pathlib import Path
from hub_auth import _context, owns, verified_identity
from condition_sql import execute_readonly, parameters, test_with_genie


class CreatorRunner:
    def __init__(self, resolve_identity, warehouse_id):
        self.resolve_identity = resolve_identity
        self.warehouse_id = warehouse_id

    def __call__(self, task, directory, variables):
        security = task.get('report',{}).get('security',{})
        subject, workspace = security.get('creator_subject'), security.get('workspace')
        if not subject or not workspace or security.get('execution_authority')!='creator':
            raise PermissionError('Task lacks a trusted creator binding; explicit migration is required')
        from task_authorization import verify_creator
        user = self.resolve_identity(workspace, subject)
        if user is None:raise PermissionError('Creator authorization unavailable')
        user = verify_creator(task,user)
        marker = _context.set(user)
        try:
            trigger=task['trigger'];mode=trigger['type']
            window=task['report'].get('condition_agent' if mode=='agent' else 'condition_sql',{}).get('window','day')
            values=dict(variables,**parameters(variables['business_date'],window))
            if mode=='sql':
                query=trigger.get('sql','')
                if not query and task.get('_source')!='registry':
                    query=(Path(directory)/trigger.get('file','trigger.sql')).read_text()
                result=execute_readonly(user.client,self.warehouse_id,query,values)
            elif mode=='agent':
                prompt=trigger.get('prompt','')
                if task.get('_source')!='registry': prompt=(Path(directory)/prompt).read_text()
                result=test_with_genie(user.client,'genie_one',prompt,values)
            elif mode=='always': result={'should_trigger':True,'reason':'Scheduled unconditional report'}
            else: raise ValueError('Trigger is incomplete')
            fired,reason=result['should_trigger'],result['reason']
            if type(fired) is not bool: raise ValueError('Trigger must be a strict BOOLEAN')
            if not fired: return False,reason,''
            if task['report'].get('data_file'):
                raise PermissionError('Legacy Spark report evidence requires a creator-scoped SQL export adapter')
            from agent_client import AgentClient
            from task_engine import TaskManager
            manager=TaskManager.__new__(TaskManager)
            manager.agents=AgentClient({'genie_one':{}},workspace=user.client)
            return True,reason,manager._report(task,Path(directory),values,reason)
        finally: _context.reset(marker)

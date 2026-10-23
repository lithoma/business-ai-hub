"""Formal run APIs registered independently of the development launcher."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading

from fastapi import HTTPException
from local_trials import TrialRequest, SendRequest


def install_engine_path(root):
    root=Path(root).resolve()
    # A remote Workspace config path cannot supply local Python modules.
    if not (root/'schedule_core.py').is_file():
        root=Path(__file__).resolve().parents[1]/'business-task-manager-v1/business-task-manager-v1'
    if str(root) not in sys.path:sys.path.insert(0,str(root))
    return root


def create_service(query,load_tasks,settings,root,warehouse_id,*,auth_manager=None):
    root=install_engine_path(root)
    from run_service import RunRepository,RunService
    from condition_sql import execute_readonly,parameters,test_with_genie
    from hub_auth import business_workspace
    from agent_client import AgentClient
    from task_engine import TaskManager
    from report_email import build_message,GmailProvider
    from zoneinfo import ZoneInfo
    table=os.environ.get('TASK_RUNS_TABLE','workspace.douyin.business_task_runs_v1')
    def condition(task,day):
        trigger=task['trigger'];review=task['report'].get('condition_agent' if trigger['type']=='agent' else 'condition_sql',{})
        values=parameters(day,review.get('window','day'))
        values['dispatch_date']=datetime.now(ZoneInfo(task.get('schedule',{}).get('timezone','Asia/Shanghai'))).date().isoformat()
        if review.get('local_test_only'):raise ValueError('该任务包含旧本地模拟日期适配，正式环境需要重新确认条件。')
        if trigger['type']=='sql':result=execute_readonly(business_workspace(),warehouse_id,trigger['sql'],values)
        elif trigger['type']=='agent':result=test_with_genie(business_workspace(),'genie_one',trigger['prompt'],values,[])
        elif trigger['type']=='always':result={'should_trigger':True,'reason':'本次明确请求的报告执行。'}
        else:raise ValueError('触发条件未配置。')
        return result,values
    def report(task,values,reason):
        if task['report'].get('data_file'):raise ValueError('尚未接入创建者范围的数据导出适配器。')
        manager=TaskManager.__new__(TaskManager)
        manager.agents=AgentClient(settings()['agents'],workspace=business_workspace())
        return manager._report(task,Path(root),values,reason)
    def preview(row,snapshot):
        if not snapshot['report'].get('delivery',{}).get('enabled'):raise ValueError('请先为任务配置邮件收件人与模板。')
        if snapshot['report']['delivery'].get('csv_exports'):raise ValueError('当前交互试运行只支持文本报告；CSV 导出仍待接入。')
        return build_message(None,Path(root),settings()['notification']['email'],row)[0]
    def send(raw):return GmailProvider.from_environment().send(raw)
    from task_authorization import resolve_for_task
    service=RunService(RunRepository(query,table),load_tasks,condition,report,preview,
        send if os.getenv('TASK_EMAIL_ENABLED','').lower()=='true' else None,
        creator_resolver=(lambda task:resolve_for_task(auth_manager,task)) if auth_manager else None)
    return service


def register_run_routes(app,query,load_tasks,settings,root,warehouse_id):
    root=install_engine_path(root)
    lock=threading.RLock()
    def service(*,background=False):
        with lock:
            existing=getattr(app.state,'run_service',None)
            if existing:
                existing.recover_interrupted()
                return existing
            if not background and os.getenv('HUB_MANUAL_RUNS_ENABLED','').lower()!='true':
                raise HTTPException(503,'报告试运行尚未开放；已保存的草稿保留。')
            app.state.run_service=create_service(query,load_tasks,settings,root,warehouse_id,auth_manager=app.state.auth)
            app.state.run_service.recover_interrupted()
            return app.state.run_service
    from creator_jobs import register_creator_job_routes
    register_creator_job_routes(app,lambda:service(background=True))
    @app.get('/api/runs/settings')
    def options():
        if not getattr(app.state,'run_service',None) and os.getenv('HUB_MANUAL_RUNS_ENABLED','').lower()!='true':
            return {'enabled':False,'mail_available':False,'api':'runs','scheduler_enabled':False,
                    'reason':'尚未开放正式试运行入口；可以先创建草稿。'}
        return {**service().settings(),'scheduler_enabled':scheduler_status()['enabled']}
    @app.post('/api/tasks/{task_id}/trial',status_code=202)
    def start(task_id:str,request:TrialRequest):
        from hub_auth import current_identity
        if current_identity().mode=='apps_obo' and os.getenv('HUB_MANUAL_RUNS_ENABLED')!='true':
            raise HTTPException(503,'手动试运行尚未开放。')
        return service().start(task_id,request)
    @app.get('/api/tasks/{task_id}/execution-identity')
    def execution_identity(task_id:str):
        from hub_auth import current_identity,owns
        rows=[task for task in load_tasks() if task.get('task_id')==task_id and owns(task)]
        if len(rows)!=1:raise HTTPException(404,'任务不存在或不属于当前用户。')
        security=rows[0].get('report',{}).get('security',{})
        return {'creator_subject':security.get('creator_subject'),
            'workspace':security.get('workspace'),
            'execution_authority':security.get('execution_authority'),
            'permissions':'current_databricks_permissions_at_execution',
            'credential_availability':app.state.auth.task_authorizations.status(current_identity()),
            'durable_resolver_configured':app.state.auth.creator_resolver is not None}
    @app.get('/api/runs/{run_id}')
    def status(run_id:str):return service().status(run_id)
    @app.get('/api/runs/{run_id}/email-preview')
    def preview(run_id:str):return service().preview(run_id)
    @app.post('/api/runs/{run_id}/send')
    def send(run_id:str,request:SendRequest):return service().send(run_id,request)
    @app.get('/api/scheduler/plan')
    def schedule_plan():
        result=service().schedule_plan(datetime.now(timezone.utc))
        status=scheduler_status()
        result.update(scheduler=status,scheduler_enabled=status['enabled'])
        return result
    @app.get('/api/scheduler/status')
    def scheduler_status():
        scheduler=getattr(app.state,'scheduler',None)
        return scheduler.status() if scheduler else {
            'enabled':False,'state':'disabled','interval_seconds':60,'ticks':0,
            'last_tick':None,'automatic_email':False,'single_process_only':True}
    # Deliberately no HTTP endpoint for starting the scheduler or a batch dispatch.
    # An approved external timer may call RunService.dispatch_once with explicit identity.

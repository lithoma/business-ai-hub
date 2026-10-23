"""Authenticated per-creator Job ticks into the existing, single App dispatcher.

The Job supplies fresh platform user authorization on each invocation. Job IDs
come from an administrator-approved public binding file, never from task owners
posted by the browser. Importing this module creates no resources or schedules.
"""
import json
import os
from pathlib import Path
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from hub_auth import current_identity


class JobTick(BaseModel):
    model_config=ConfigDict(extra='forbid')
    job_id: int=Field(gt=0)
    job_run_id: int=Field(gt=0)
    creator_subject: str=Field(min_length=1,max_length=256)


def approved_binding(user):
    if os.getenv('HUB_CREATOR_JOBS_ENABLED')!='true':
        raise HTTPException(503,'创建者后台 Job 接入尚未启用。')
    if user.mode!='apps_obo':
        raise HTTPException(403,'后台 Job 入口只接受 Apps 验证的用户身份。')
    try:
        raw=json.loads(Path(os.environ['HUB_CREATOR_JOB_BINDINGS_FILE']).read_text())
        if raw.get('version')!=1 or not isinstance(raw.get('bindings'),list):raise ValueError()
        matches=[item for item in raw['bindings'] if item.get('workspace')==user.workspace
            and item.get('creator_subject')==user.subject and item.get('enabled') is True]
        if len(matches)!=1:raise ValueError()
        binding=matches[0]
        if type(binding.get('job_id')) is not int or binding['job_id']<=0:raise ValueError()
        return binding
    except (OSError,KeyError,TypeError,ValueError):
        raise HTTPException(403,'该创建者没有唯一的已批准 Job 绑定。') from None


def register_creator_job_routes(app,get_service):
    @app.post('/api/creator-jobs/dispatch')
    def dispatch(request:JobTick):
        from datetime import datetime,timezone
        from task_authorization import verify_creator
        user=current_identity();binding=approved_binding(user)
        if os.getenv('TASK_EXECUTION_ENABLED')!='true':
            raise HTTPException(503,'任务执行总开关尚未开启。')
        if request.creator_subject!=user.subject or request.job_id!=binding['job_id']:
            raise HTTPException(403,'Job 与已验证的任务创建者不匹配。')
        # Current forwarded credentials came from THIS machine request, not from
        # browser session storage. All task rows still pass creator ownership checks.
        result=get_service().dispatch_once(datetime.now(timezone.utc),
            lambda task:verify_creator(task,user),enabled=True,
            execution_context={'provider':'databricks_job','job_id':request.job_id,
                'job_run_id':request.job_run_id,'creator_subject':user.subject,
                'identity_verified':True,'job_run_id_verified_by_server':False})
        return {**result,'creator_subject':user.subject,'execution_provider':'databricks_job'}

    @app.get('/api/creator-jobs/runs/{run_id}')
    def status(run_id:str):
        approved_binding(current_identity())
        if not re.fullmatch(r'[a-f0-9]{64}',run_id):raise HTTPException(404,'运行记录不存在。')
        result=get_service().status(run_id)
        # Never copy report content or business evidence into the notebook logs.
        return {key:result[key] for key in ('run_id','status','phase','error','timings')}

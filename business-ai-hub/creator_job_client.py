"""Notebook Job client. No browser sessions, PAT files, profiles or SP fallback.

Notebook token -> short-lived App-audience OAuth token -> protected App APIs.
Only explicit run_job() performs HTTP. Config/template utilities are offline.
"""
from dataclasses import dataclass,fields
import json
import re
import time
from urllib.parse import urlsplit

import requests

APP_SCOPES=('iam.current-user:read','iam.access-control:read','ai-gateway','sql:restricted-query','model-serving')


def origin(value,*,app=False):
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.port):
        raise ValueError('Expected an HTTPS origin without path or credentials')
    if app and not parsed.hostname.endswith('.databricksapps.com'):
        raise ValueError('Use the platform-assigned Databricks Apps HTTPS origin')
    return value.rstrip('/')


@dataclass(frozen=True)
class JobConfig:
    workspace_host: str
    app_origin: str
    app_client_id: str
    creator_subject: str
    run_as_user_name: str
    repo_root: str
    config_path: str
    notebook_path: str
    existing_cluster_id: str

    @classmethod
    def load(cls,path):
        with open(path,encoding='utf-8') as stream:raw=json.load(stream)
        if not isinstance(raw,dict) or set(raw)!={field.name for field in fields(cls)}:
            raise ValueError('Job config must contain only the documented public fields; no secrets')
        result=cls(**raw);result.validate();return result

    def validate(self):
        if any(not isinstance(getattr(self,field.name),str) or not getattr(self,field.name).strip() for field in fields(self)):
            raise ValueError('Complete every public Job config field first')
        origin(self.workspace_host);origin(self.app_origin,app=True)
        for value in (self.app_client_id,self.creator_subject,self.existing_cluster_id):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',value):raise ValueError('Invalid public resource ID')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+',self.run_as_user_name):raise ValueError('Expected the creator Databricks username')
        if any(not path.startswith('/Workspace/') or '..' in path.split('/') for path in (self.repo_root,self.config_path,self.notebook_path)):
            raise ValueError('Job source and config must use approved /Workspace/ paths')


def job_settings(config):
    config.validate()
    return {'name':'business-ai-hub-creator-'+config.creator_subject,
        'run_as':{'user_name':config.run_as_user_name},'max_concurrent_runs':1,
        'queue':{'enabled':False},'timeout_seconds':1800,
        'schedule':{'quartz_cron_expression':'0 0 * * * ?',
            'timezone_id':'Asia/Shanghai','pause_status':'PAUSED'},
        'tasks':[{'task_key':'creator_tick','existing_cluster_id':config.existing_cluster_id,
            'max_retries':0,'timeout_seconds':1800,
            'notebook_task':{'notebook_path':config.notebook_path,'source':'WORKSPACE',
                'base_parameters':{'repo_root':config.repo_root,'config_path':config.config_path,
                    'job_id':'{{job.id}}','job_run_id':'{{job.run_id}}'}}}]}


class JobClient:
    def __init__(self,config,get_notebook_token,*,session=None,clock=time.monotonic):
        config.validate()
        self.config,self.get_notebook_token=config,get_notebook_token
        self.session,self.clock=session or requests.Session(),clock
        self._access_token=None;self._expires=0

    def __repr__(self):return '<JobClient credentials=redacted>'

    def _headers(self):
        if self._access_token is None or self.clock()>=self._expires:
            notebook_token=self.get_notebook_token()
            if not isinstance(notebook_token,str) or not notebook_token:raise RuntimeError('Notebook runtime token unavailable')
            try:
                response=self.session.post(self.config.workspace_host.rstrip('/')+'/oidc/v1/token',
                    data={'grant_type':'urn:ietf:params:oauth:grant-type:token-exchange',
                        'subject_token':notebook_token,
                        'subject_token_type':'urn:databricks:params:oauth:token-type:personal-access-token',
                        'requested_token_type':'urn:ietf:params:oauth:token-type:access_token',
                        'scope':' '.join(APP_SCOPES),'audience':self.config.app_client_id},
                    timeout=(5,60),allow_redirects=False)
            except requests.RequestException:raise RuntimeError('Notebook token exchange unavailable; no retry') from None
            if response.status_code!=200:raise RuntimeError('Notebook token exchange failed: HTTP '+str(response.status_code))
            try:
                raw=response.json();token=raw['access_token'];ttl=int(raw['expires_in'])
                if not isinstance(token,str) or not token or any(c.isspace() for c in token) or ttl<=30:
                    raise ValueError()
                if str(raw.get('token_type','bearer')).lower()!='bearer':raise ValueError()
            except (KeyError,TypeError,ValueError):raise RuntimeError('Invalid App token exchange response') from None
            self._access_token=token;self._expires=self.clock()+ttl-30
        return {'Authorization':'Bearer '+self._access_token,'Origin':self.config.app_origin.rstrip('/')}

    def request(self,method,path,body=None):
        if not (path=='/api/creator-jobs/dispatch' or re.fullmatch(r'/api/creator-jobs/runs/[a-f0-9]{64}',path)):
            raise ValueError('Unexpected Job API route')
        headers=self._headers()
        try:
            response=self.session.request(method,self.config.app_origin.rstrip('/')+path,
                headers=headers,**({'json':body} if body is not None else {}),
                timeout=(5,120),allow_redirects=False)
        except requests.RequestException:raise RuntimeError('App Job request outcome unknown; not dispatched again') from None
        if not 200<=response.status_code<300:
            raise RuntimeError('App Job request failed: HTTP '+str(response.status_code)+'; no automatic retry')
        result=response.json()
        if not isinstance(result,dict):raise RuntimeError('Invalid App Job response')
        return result


def run_job(config,runtime_client,get_notebook_token,job_id,job_run_id,*,transport=None,
            clock=time.monotonic,sleep=time.sleep,wait_seconds=1500,poll_seconds=15):
    """One tick + bounded status polling; no repeated dispatch after uncertainty."""
    config.validate()
    if type(job_id) is not int or type(job_run_id) is not int or min(job_id,job_run_id)<=0:
        raise ValueError('A real Jobs run context is required')
    if runtime_client.config.auth_type!='runtime' or origin(runtime_client.config.host)!=origin(config.workspace_host):
        raise PermissionError('Explicit matching notebook runtime identity required')
    user=runtime_client.current_user.me()
    if (str(user.id)!=config.creator_subject or user.user_name!=config.run_as_user_name or user.active is False):
        raise PermissionError('Job Run as identity does not match task creator')
    client=transport or JobClient(config,get_notebook_token,clock=clock)
    started=clock()
    completed=[];seen=set()
    deadline=started+wait_seconds
    for batch in range(100):
        if clock()>=deadline:raise TimeoutError('App runs may still be active; not dispatched again')
        result=client.request('POST','/api/creator-jobs/dispatch',{
            'job_id':job_id,'job_run_id':job_run_id,'creator_subject':config.creator_subject})
        if result.get('creator_subject')!=config.creator_subject:raise PermissionError('App returned another creator')
        outcomes=result.get('runs')
        if not isinstance(outcomes,list):raise RuntimeError('Invalid dispatch response')
        pending={};deferred=False
        for item in outcomes:
            key=item.get('run_id')
            if key:
                if not re.fullmatch(r'[a-f0-9]{64}',key):raise RuntimeError('Invalid run reference')
                if key not in seen:pending[key]=item
            elif item.get('status')=='DEFERRED' and item.get('claimed') is False:deferred=True
            elif item.get('status')=='FAILED':
                completed.append({name:item.get(name) for name in ('task_id','status','error') if name in item})
            else:raise RuntimeError('Unrecognized dispatch outcome')
        while pending:
            if clock()>=deadline:raise TimeoutError('App runs may still be active; not dispatched again')
            for key in list(pending):
                state=client.request('GET','/api/creator-jobs/runs/'+key)
                if state.get('run_id')!=key:raise RuntimeError('Unexpected run status reference')
                if state.get('status') in ('REPORT_CREATED','NOT_TRIGGERED','FAILED'):
                    completed.append({'run_id':key,'status':state['status'],'timings':state.get('timings',{})})
                    seen.add(key);del pending[key]
            if pending:sleep(min(poll_seconds,max(0,deadline-clock())))
        if not deferred:break
        # Only explicit NOT-CLAIMED capacity deferrals allow another batch.
        # Ambiguous HTTP errors never reach this path and are never replayed.
        sleep(min(poll_seconds,max(0,deadline-clock())))
    else:raise TimeoutError('Dispatch batch limit reached; no automatic rerun')
    failed=any(item.get('status')=='FAILED' for item in completed)
    return {'state':'attention_required' if failed else 'completed','creator_subject':config.creator_subject,
        'job_id':job_id,'job_run_id':job_run_id,'duration_seconds':round(clock()-started,3),
        'runs':completed,'automatic_email':False,'browser_session_required':False}

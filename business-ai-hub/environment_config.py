"""Explicit public environment profiles; credentials remain in SDK/secret stores."""
from copy import deepcopy
import os
from pathlib import Path
import re
import yaml

ENV_KEYS = frozenset('DATABRICKS_HOST SQL_WAREHOUSE_ID HUB_AUTH_MODE HUB_PUBLIC_ORIGIN HUB_OAUTH_CLIENT_ID HUB_OAUTH_SCOPES HUB_STORAGE_PROFILE TASK_REGISTRY_TABLE TASK_RUNS_TABLE TASK_PARSER_ENDPOINT CONVERSATION_ROUTER_ENDPOINT HUB_WORKFLOW_ENABLED GENIE_ONE_ENABLED TASK_CONDITION_SQL_ENABLED TASK_EXECUTION_ENABLED TASK_EMAIL_ENABLED TASK_DISPATCH_TIME TASK_DISPATCH_TIMEZONE HUB_MANUAL_RUNS_ENABLED TASK_SCHEDULER_MODE'.split())
ENV_KEYS=ENV_KEYS|{'HUB_INCLUDE_YAML_TASKS','HUB_CREATOR_JOBS_ENABLED','HUB_CREATOR_JOB_BINDINGS_FILE','HUB_LOCAL_AUTH_TYPE'}


def read_profile(path):
    path=Path(path).expanduser().resolve()
    data=yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('version')!=1 or data.get('environment') not in ('local','company'):
        raise ValueError('配置文件需要 version: 1 和 environment: local/company。')
    if set(data)-{'version','environment','env','task_manager','local'}:
        raise ValueError('配置文件含未知设置区域。')
    if not isinstance(data.get('env'),dict) or set(data['env'])-ENV_KEYS:
        raise ValueError('配置文件只能包含允许的公开环境变量，不能存放密码、Token 或密钥。')
    def check(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if re.search(r'password|access_token|refresh_token|client_secret|private_key|review_secret',str(key),re.I):
                    raise ValueError('凭据必须放在外部 Secret/SDK 配置中，不能写入环境配置文件。')
                check(item)
        elif isinstance(value,list):
            for item in value:check(item)
    check(data)
    if not isinstance(data.get('task_manager',{}),dict): raise ValueError('task_manager 必须为设置映射。')
    if data['env'].get('DATABRICKS_HOST')!=data.get('task_manager',{}).get('workspace_host'):
        raise ValueError('Hub 与 Task Manager 必须属于同一个 Workspace。')
    if data['environment']=='company':
        if data['env'].get('HUB_AUTH_MODE') not in ('oauth','databricks_app','databricks_app_obo'): raise ValueError('公司配置需要个人 OAuth 或显式 Databricks Apps 平台身份。')
        if data.get('local'): raise ValueError('公司配置不能包含本地 SQLite/CLI 测试设置。')
    return data


def merge(base,override):
    result=deepcopy(base)
    for key,value in override.items():
        result[key]=merge(result.get(key,{}),value) if isinstance(value,dict) else deepcopy(value)
    return result


def selected_profile():
    path=os.getenv('HUB_CONFIG_FILE')
    return read_profile(path) if path else None


def missing_settings(profile):
    if profile['environment']!='company':return []
    env,tm=profile['env'],profile.get('task_manager',{})
    required=['HUB_PUBLIC_ORIGIN','TASK_REGISTRY_TABLE','TASK_RUNS_TABLE']
    if env.get('HUB_AUTH_MODE') not in ('databricks_app','databricks_app_obo'):required+=['HUB_OAUTH_CLIENT_ID','HUB_STORAGE_PROFILE']
    missing=[key for key in required if not env.get(key)]
    for key in ('catalog','schema'):
        if not tm.get(key):missing.append('task_manager.'+key)
    for section,key,env_key in [('registry','table','TASK_REGISTRY_TABLE'),('execution','runs_table','TASK_RUNS_TABLE')]:
        if not tm.get(section,{}).get(key):missing.append('task_manager.'+section+'.'+key)
        elif tm[section][key]!=env.get(env_key):raise ValueError('Hub 与 Task Manager 的存储表配置不一致。')
    return missing


def apply_selected_profile():
    profile=selected_profile()
    if profile is None:return None
    missing=missing_settings(profile)
    if missing:raise ValueError('公司配置尚未完成：'+', '.join(missing)+'。未回退到开发环境。')
    if profile['env'].get('HUB_AUTH_MODE') in ('databricks_app','databricks_app_obo'):
        runtime_host=os.getenv('DATABRICKS_HOST','').rstrip('/')
        if not os.getenv('DATABRICKS_APP_NAME') or runtime_host!=profile['env']['DATABRICKS_HOST'].rstrip('/'):
            raise ValueError('Apps 配置只能在匹配的 Databricks Apps Workspace 中启动；未使用个人登录配置。')
    for key,value in profile['env'].items():
        if not isinstance(value,(str,bool,int)):
            raise ValueError('公开环境变量只能是字符串、布尔值或整数。')
        os.environ[key]=str(value).lower() if isinstance(value,bool) else str(value)
    return profile


def task_manager_settings(base):
    profile=selected_profile()
    return merge(base,profile.get('task_manager',{})) if profile else base

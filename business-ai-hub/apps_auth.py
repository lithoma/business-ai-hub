"""App metadata identity and explicit shared / per-user business authority.

No personal profile, PAT or forwarded user token is used as a fallback. This mode
must only be hosted behind the Databricks Apps ingress, never a public raw server.
"""
from dataclasses import replace
import os
import threading
from urllib.parse import urlsplit
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import credentials_strategy
from fastapi import HTTPException
from hub_auth import Identity, workspace_host, verified_identity
from telemetry import observed_workspace

_lock=threading.Lock()
_client=None


def runtime_settings():
    required=('DATABRICKS_APP_NAME','DATABRICKS_HOST','DATABRICKS_CLIENT_ID','DATABRICKS_CLIENT_SECRET')
    if any(not os.getenv(key) for key in required):
        raise HTTPException(503,'Databricks Apps 平台身份未就绪；不会回退到个人配置。')
    return workspace_host(os.environ['DATABRICKS_HOST']),os.environ['DATABRICKS_CLIENT_ID']


def app_workspace():
    global _client
    host,client_id=runtime_settings()
    with _lock:
        if _client is None:
            _client=observed_workspace(WorkspaceClient(host=host,client_id=client_id,auth_type='oauth-m2m'))
        if _client.config.host.rstrip('/')!=host or _client.config.client_id!=client_id or _client.config.auth_type!='oauth-m2m':
            raise HTTPException(503,'App 身份与当前 Workspace 不一致。')
        return _client


def verified_app_identity(client):
    host,client_id=runtime_settings()
    if client.config.host.rstrip('/')!=host or client.config.client_id!=client_id or client.config.auth_type!='oauth-m2m':
        raise HTTPException(403,'执行身份不是当前 App 服务主体。')
    return Identity('app:'+client_id,host,'App 共享业务身份','databricks_app',client)


def resolve_app_identity(request,manager):
    runtime_settings()
    try:origin=workspace_host(manager.origin)
    except ValueError:raise HTTPException(503,'请配置平台分配的 App HTTPS 地址 HUB_PUBLIC_ORIGIN。') from None
    # These headers are supplied by the platform proxy; do not deploy this mode
    # outside Apps or disable its platform authentication/network boundary.
    actor=request.headers.get('x-forwarded-user','').strip()
    if not actor or len(actor)>256:raise HTTPException(401,'缺少 Databricks Apps 登录身份。')
    if request.headers.get('x-forwarded-host','').lower()!=urlsplit(origin).netloc.lower():
        raise HTTPException(403,'App 代理地址与配置不一致。')
    if manager.mode=='databricks_app_obo':
        token=request.headers.get('x-forwarded-access-token','').strip()
        if not token or len(token)>32768 or any(c.isspace() for c in token):
            raise HTTPException(401,'缺少有效的 Apps 用户授权；请启用用户授权并重新同意，不会回退到 App 身份。')
        host,_=runtime_settings()
        # An explicit SDK credential strategy prevents environment M2M/profile
        # discovery from supplying business credentials. Never log this closure.
        @credentials_strategy('apps-obo',[])
        def user_credentials(config):
            if workspace_host(config.host)!=host:
                raise ValueError('OBO Workspace mismatch')
            return lambda:{'Authorization':'Bearer '+token}
        try:
            client=WorkspaceClient(host=host,credentials_strategy=user_credentials,
                auth_type='apps-obo',debug_headers=False,debug_truncate_bytes=0)
            # The token's verified Databricks ID is authoritative. IdP forwarded
            # IDs/emails may use another namespace and must not set task ownership.
            user=verified_identity(client,'apps_obo')
        except Exception:
            raise HTTPException(401,'Apps 用户授权验证失败；请检查用户授权 scope 或重新授权，未使用 App 身份继续。') from None
        return replace(user,actor_subject=actor)
    if manager.mode!='databricks_app':
        raise HTTPException(503,'未知 Apps 授权模式。')
    email=request.headers.get('x-forwarded-email','').strip()
    label=email if email and len(email)<=254 and '\r' not in email and '\n' not in email else actor
    return replace(verified_app_identity(app_workspace()),label=label,actor_subject=actor)

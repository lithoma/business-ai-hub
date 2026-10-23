"""User-bound business access. OAuth secrets/tokens never leave server memory.

Internal-server deployment uses a registered Databricks OAuth application (PKCE).
Local CLI mode is explicit and loopback-only; forwarded identity headers are ignored.
Metadata storage credentials are separate and must never execute business queries.
"""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from databricks.sdk import WorkspaceClient
from databricks.sdk.oauth import OAuthClient, OidcEndpoints
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

SESSION_COOKIE = '__Host-hub-session'
LOGIN_COOKIE = '__Host-hub-login'
_context = ContextVar('hub_identity', default=None)


@dataclass(frozen=True)
class Identity:
    subject: str
    workspace: str
    label: str
    mode: str
    client: object = field(repr=False, compare=False)
    actor_subject: str = ''

    @property
    def key(self):
        return self.workspace + ':' + self.subject

    @property
    def security(self):
        result={'version': 1, 'creator_subject': self.subject, 'workspace': self.workspace,
                'execution_authority': 'app_service_principal' if self.mode=='databricks_app' else 'creator'}
        if self.actor_subject:result['created_by_platform_user']=self.actor_subject
        return result


def current_identity():
    value = _context.get()
    if value is None:
        raise HTTPException(401, '请先使用自己的 Databricks 账户登录。')
    return value


def business_workspace():
    return current_identity().client


def owns(task, identity=None):
    identity = identity or current_identity()
    security = task.get('report', {}).get('security', {})
    return (security.get('creator_subject') == identity.subject
            and security.get('workspace') == identity.workspace)


def ownership_sql(column='report_config'):
    # Column name is internal code only; all user identity values are bound.
    prefix = '$.report.security' if column == 'config_json' else '$.security'
    return (f"get_json_object({column}, '{prefix}.creator_subject')=:auth_subject AND "
            f"get_json_object({column}, '{prefix}.workspace')=:auth_workspace")


def ownership_params():
    user = current_identity()
    return {'auth_subject': user.subject, 'auth_workspace': user.workspace}


def workspace_host(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('DATABRICKS_HOST must be an HTTPS Workspace origin')
    return value.rstrip('/')


def verified_identity(client, mode):
    user = client.current_user.me()
    if not user.id or user.active is False or not user.user_name:
        raise HTTPException(403, '无法确认有效的 Databricks 用户。')
    if client.config.client_id and mode == 'local_cli':
        raise HTTPException(403, '本地模式需要个人用户登录，不能使用共享 Service Principal。')
    from telemetry import observed_workspace
    client = observed_workspace(client)
    return Identity(str(user.id), workspace_host(client.config.host), str(user.user_name), mode, client)


class AuthManager:
    """Single-process, bounded, expiring OAuth sessions; restart requires login again.

    SDK SessionCredentials refresh only in memory. Durable unattended delegation is
    deliberately separate; these browser sessions are not a background job vault.
    """
    def __init__(self):
        self.mode = os.getenv('HUB_AUTH_MODE', 'oauth')
        self.origin = os.getenv('HUB_PUBLIC_ORIGIN', '').rstrip('/')
        self.sessions, self.pending = {}, {}
        self.lock = threading.RLock()
        self.local = None
        from task_authorization import CreatorAuthorizations
        self.task_authorizations = CreatorAuthorizations()
        self.creator_resolver = None  # Company broker injection: server code only.

    def oauth_client(self):
        try:
            host = workspace_host(os.environ['DATABRICKS_HOST'])
            origin = workspace_host(self.origin)
            client_id = os.environ['HUB_OAUTH_CLIENT_ID']
            if not client_id: raise ValueError('Missing client ID')
        except (KeyError, ValueError):
            raise HTTPException(503, '登录未配置：需要公司 Workspace、站点 HTTPS 地址和 OAuth Client ID。') from None
        return OAuthClient(OidcEndpoints(host+'/oidc/v1/authorize',host+'/oidc/v1/token'),
            redirect_url=origin+'/auth/callback', client_id=client_id,
            client_secret=os.getenv('HUB_OAUTH_CLIENT_SECRET') or None,
            scopes=os.getenv('HUB_OAUTH_SCOPES', 'iam.current-user:read ai-gateway sql:restricted-query model-serving offline_access').split())

    def prune(self):
        now = time.monotonic()
        self.sessions = {k:v for k,v in self.sessions.items() if v[0]>now}
        self.pending = {k:v for k,v in self.pending.items() if v[0]>now}

    def resolve(self, request):
        if self.mode in ('databricks_app','databricks_app_obo'):
            from apps_auth import resolve_app_identity
            return resolve_app_identity(request,self)
        if self.mode == 'local_cli':
            if not request.client or request.client.host not in ('127.0.0.1', '::1', 'localhost'):
                raise HTTPException(403, '本地 CLI 模式只允许本机访问。')
            if request.url.hostname not in ('127.0.0.1','localhost','::1'):
                raise HTTPException(403, '本地模式拒绝外部 Host。')
            with self.lock:
                if self.local is None:
                    if os.getenv('HUB_LOCAL_AUTH_TYPE')=='external-browser':
                        self.local=verified_identity(WorkspaceClient(
                            host=workspace_host(os.environ['DATABRICKS_HOST']),auth_type='external-browser',
                            scopes=['iam.current-user:read','ai-gateway','sql:restricted-query','model-serving']), 'local_cli')
                    elif not os.getenv('HUB_LOCAL_AUTH_TYPE'):
                        self.local = verified_identity(WorkspaceClient(profile=os.getenv('HUB_LOCAL_PROFILE','business-ai-hub')), 'local_cli')
                    else:raise HTTPException(503,'未知本地登录方式；未回退其他身份。')
                return self.local
        if self.mode != 'oauth':
            raise HTTPException(503, '未知身份认证模式；未回退到共享身份。')
        with self.lock:
            self.prune()
            item = self.sessions.get(request.cookies.get(SESSION_COOKIE, ''))
        if not item:
            raise HTTPException(401, '登录已过期，请使用自己的 Databricks 账户重新登录。')
        # Credentials are refreshable inside this user-specific client, never shared.
        return item[1]

    def start_login(self):
        if self.mode in ('databricks_app','databricks_app_obo'):raise HTTPException(409,'请通过 Databricks Apps 平台登录并同意用户授权。')
        if self.mode != 'oauth': raise HTTPException(409, '本地使用 CLI 用户身份，无需网页登录。')
        consent = self.oauth_client().initiate_consent()
        nonce = secrets.token_urlsafe(32)
        with self.lock:
            self.prune()
            if len(self.pending)>=200: raise HTTPException(429, '登录请求过多，请稍后再试。')
            self.pending[nonce] = (time.monotonic()+600, consent)
        response = RedirectResponse(consent.authorization_url, status_code=303)
        response.set_cookie(LOGIN_COOKIE,nonce,secure=True,httponly=True,samesite='lax',max_age=600,path='/')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    def callback(self, request):
        nonce = request.cookies.get(LOGIN_COOKIE, '')
        with self.lock:
            self.prune()
            item = self.pending.pop(nonce, None)
        if not item: raise HTTPException(400, '登录会话无效或过期，请重新登录。')
        try:
            credentials = item[1].exchange_callback_parameters(dict(request.query_params))
            client = WorkspaceClient(host=workspace_host(os.environ['DATABRICKS_HOST']),credentials_strategy=credentials)
            identity = verified_identity(client, 'oauth_user')
        except Exception:
            raise HTTPException(401, 'Databricks 登录未完成；请重新登录或联系管理员检查 OAuth 配置。') from None
        key = secrets.token_urlsafe(48)
        with self.lock:
            self.prune()
            if len(self.sessions)>=500: raise HTTPException(429, '在线会话已满，请稍后重试。')
            # Rotate session to prevent fixation; never put OAuth tokens in cookies.
            self.sessions.pop(request.cookies.get(SESSION_COOKIE, ''), None)
            self.sessions[key] = (time.monotonic()+8*3600, identity)
        response = RedirectResponse(self.origin+'/', status_code=303)
        response.set_cookie(SESSION_COOKIE,key,secure=True,httponly=True,samesite='lax',max_age=8*3600,path='/')
        response.delete_cookie(LOGIN_COOKIE,path='/',secure=True,httponly=True,samesite='lax')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response


class IdentityMiddleware:
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope,receive,send)
        path = scope.get('path','')
        if not path.startswith('/api/') or path == '/api/health':
            return await self.app(scope,receive,send)
        request = Request(scope)
        manager = scope['app'].state.auth
        try:
            identity = await run_in_threadpool(manager.resolve,request)
            origin = request.headers.get('origin')
            expected = manager.origin if manager.mode in ('oauth','shared_trial','databricks_app','databricks_app_obo') else str(request.base_url).rstrip('/')
            if request.method not in ('GET','HEAD','OPTIONS'):
                if (origin and origin != expected) or (manager.mode in ('oauth','shared_trial','databricks_app','databricks_app_obo') and not origin):
                    raise HTTPException(403, '请求来源不匹配，请从本站重新操作。')
                if request.headers.get('sec-fetch-site') == 'cross-site':
                    raise HTTPException(403, '禁止跨站修改请求。')
            # Request identity automatically supplies the creator credential; no
            # separate per-task consent/button and no shared identity substitution.
            manager.task_authorizations.remember(identity)
            marker = _context.set(identity)
            try:
                async def no_cache(message):
                    if message['type']=='http.response.start':
                        message.setdefault('headers',[]).extend([(b'cache-control',b'no-store'),(b'vary',b'Cookie')])
                    await send(message)
                await self.app(scope,receive,no_cache)
            finally: _context.reset(marker)
        except HTTPException as exc:
            await JSONResponse({'detail':exc.detail},status_code=exc.status_code)(scope,receive,send)
        except Exception:
            await JSONResponse({'detail':'身份验证暂不可用；未使用共享身份继续。'},status_code=503)(scope,receive,send)


def install_auth(app):
    app.state.auth = AuthManager()
    app.add_middleware(IdentityMiddleware)

    @app.get('/auth/login')
    def login(): return app.state.auth.start_login()

    @app.get('/auth/callback')
    def callback(request: Request): return app.state.auth.callback(request)

    @app.get('/api/session')
    def session():
        user = current_identity()
        return {'authenticated':True,'user':user.label,'subject':user.subject,'workspace':user.workspace,
                'mode':user.mode,'data_authority':'app_service_principal' if user.mode=='databricks_app' else 'current_user',
                'task_authority':'shared_app' if user.mode=='databricks_app' else 'creator',
                'background_delegation_ready':False,
                'task_authorization':app.state.auth.task_authorizations.status(user)}

    @app.get('/api/session/task-authorization')
    def task_authorization():
        return app.state.auth.task_authorizations.status(current_identity())

    @app.post('/api/session/logout')
    def logout(request: Request):
        app.state.auth.task_authorizations.revoke(current_identity())
        with app.state.auth.lock:
            app.state.auth.sessions.pop(request.cookies.get(SESSION_COOKIE,''), None)
        response = JSONResponse({'status':'signed_out'})
        response.delete_cookie(SESSION_COOKIE,path='/',secure=True,httponly=True,samesite='lax')
        return response

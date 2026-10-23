#!/usr/bin/env python3
"""Explicit temporary shared trial of the LOCAL profile; never a company auth mode."""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import re
import queue
import threading
import shutil
import subprocess
import sys
import time
import asyncio
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'business-ai-hub'))
from environment_config import read_profile, apply_selected_profile
from hub_auth import AuthManager, verified_identity, workspace_host
from databricks.sdk import WorkspaceClient
from fastapi import HTTPException
from fastapi.responses import Response


class SharedTrialAuth(AuthManager):
    """All visitors use the one explicitly selected development user's authority."""
    def __init__(self, public_origin, profile, expected_workspace):
        super().__init__()
        self.mode = 'shared_trial'
        self.origin = workspace_host(public_origin)
        self.profile = profile
        self.expected_workspace = workspace_host(expected_workspace)
        self.public_host = self.origin.split('://', 1)[1]

    def resolve(self, request):
        # Uvicorn runs with proxy_headers=False. No forwarded identity is trusted.
        if not request.client or request.client.host not in ('127.0.0.1','::1'):
            raise HTTPException(403, '共享试用仅通过本机连接器提供。')
        if request.url.hostname not in (self.public_host, '127.0.0.1', 'localhost', '::1'):
            raise HTTPException(403, '共享试用地址不匹配。')
        if request.url.hostname == self.public_host and request.url.netloc != self.public_host:
            raise HTTPException(403, '共享试用地址不匹配。')
        with self.lock:
            if self.local is None:
                identity = verified_identity(WorkspaceClient(profile=self.profile), 'local_cli')
                if identity.workspace != self.expected_workspace:
                    raise HTTPException(403, 'SDK 身份与指定本地开发 Workspace 不一致。')
                self.local = replace(identity, mode='shared_trial', label='共享测试账户')
            return self.local


def build_shared_app(config, public_origin, *, local_origin='http://127.0.0.1:8765'):
    profile = read_profile(config)
    if profile['environment'] != 'local' or profile['env'].get('HUB_AUTH_MODE', 'local_cli') != 'local_cli':
        raise ValueError('共享试用只能显式选择本地配置；公司 OAuth 配置不能关闭身份隔离。')
    if profile['env']['DATABRICKS_HOST'] != 'https://dbc-f19e9824-546f.cloud.databricks.com':
        raise ValueError('共享试用仅限已授权的模拟数据开发 Workspace，不能用于公司 Workspace。')
    os.environ['HUB_CONFIG_FILE'] = str(Path(config).resolve())
    apply_selected_profile()
    spec = importlib.util.spec_from_file_location('hub_shared_local', ROOT / 'scripts/dev-local.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    local = profile['local']
    application = launcher.build_app(Path(local['database']).expanduser(),
        endpoint=profile['env'].get('TASK_PARSER_ENDPOINT',''), profile=local['databricks_profile'],
        model_limit=None if local.get('interactive_model') else 0,
        interactive_analysis=local.get('interactive_analysis',False),
        conversation_workflow=local.get('conversation_workflow',False), analysis_provider='genie_one',
        genie_one_chat=local.get('genie_one_chat',False), manual_trials=False)
    application.state.auth = SharedTrialAuth(public_origin, local['databricks_profile'], profile['env']['DATABRICKS_HOST'])
    # The original loopback instance owns its manual-trial workers. Sharing its DB
    # must not start a second recovery worker or reset in-flight report/mail state.
    @application.middleware('http')
    async def shared_scope(request, call_next):
        if request.url.path == '/robots.txt':
            return Response('User-agent: *\nDisallow: /\n', media_type='text/plain')
        if request.url.path.startswith('/auth/'):
            return Response('共享试用已按项目负责人要求暂停访客登录；正式 OAuth 代码仍保留。', media_type='text/plain', status_code=409)
        trial_path = (re.fullmatch(r'/api/(?:local-trials|runs)/(?:settings|[A-Za-z0-9_-]+(?:/email-preview|/send)?)',request.url.path)
                      or re.fullmatch(r'/api/tasks/[A-Za-z0-9_-]+/trial',request.url.path)
                      or request.url.path in ('/api/scheduler/plan','/api/scheduler/status'))
        if trial_path:
            # Reuse the original process's existing workers, previews and one-send
            # claims instead of starting a second LocalTrials recovery process.
            try:
                await asyncio.to_thread(application.state.auth.resolve, request)
                if request.method not in ('GET','HEAD','OPTIONS'):
                    if request.headers.get('origin') != public_origin or request.headers.get('sec-fetch-site') == 'cross-site':
                        raise HTTPException(403, '请从共享试用页面操作。')
                body=await request.body()
                if len(body)>1024*1024:
                    raise HTTPException(413, '试用请求过大。')
                headers={'Content-Type':'application/json','Origin':local_origin}
                trace=request.headers.get('x-hub-trace-id','')
                if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',trace):headers['X-Hub-Trace-Id']=trace
                upstream=await asyncio.to_thread(requests.request,request.method,
                    local_origin+request.url.path,params=dict(request.query_params),data=body,
                    headers=headers,timeout=(5,65),allow_redirects=False)
                response=Response(upstream.content,status_code=upstream.status_code,
                    headers={'content-type':upstream.headers.get('content-type','application/json')})
            except HTTPException as exc:
                from fastapi.responses import JSONResponse
                response=JSONResponse({'detail':exc.detail},status_code=exc.status_code)
            except requests.RequestException:
                from fastapi.responses import JSONResponse
                response=JSONResponse({'detail':'原本的本地试运行服务不可用；未重试，也没有确认发信。请检查 8765 服务。'},status_code=503)
        else:
            response = await call_next(request)
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response
    return application


def start_tunnel(binary, port):
    process = subprocess.Popen([binary, 'tunnel', '--no-autoupdate', '--edge-ip-version', '4',
        '--url', f'http://127.0.0.1:{port}'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1)
    addresses = queue.Queue(maxsize=1)
    def read_connector_output():
        found = False
        for line in process.stdout:
            if not found:
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if match:
                    addresses.put(match.group(0))
                    found = True
            # Continue draining: a full output pipe must not block an idle connector.
    threading.Thread(target=read_connector_output,daemon=True).start()
    deadline = time.monotonic()+60
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('临时分享连接器退出，未发布试用服务。')
            try:
                return process, addresses.get(timeout=1)
            except queue.Empty:
                continue
        raise RuntimeError('创建临时分享地址超时，未发布试用服务。')
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config/local.yaml')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--cloudflared', default=str(Path.home()/'.local/bin/cloudflared'))
    parser.add_argument('--public-origin', help='Reuse an already running, explicitly selected HTTPS tunnel')
    args = parser.parse_args()
    profile = read_profile(args.config)
    if profile['environment'] != 'local':
        parser.error('只能选择 local 环境；未创建分享连接。')
    process = None
    if args.public_origin:
        origin = workspace_host(args.public_origin)
    else:
        binary = shutil.which(args.cloudflared)
        if not binary:
            parser.error('需要已校验的 Cloudflare Tunnel 连接器。')
        process, origin = start_tunnel(binary, args.port)
    try:
        application = build_shared_app(args.config, origin)
        state = Path(profile['local']['database']).expanduser().parent/'shared-trial.json'
        state.write_text(json.dumps({'url':origin,'local_url':f'http://127.0.0.1:{args.port}',
            'mode':'shared_trial','identity_isolation':False,'credentials_in_url':False,
            'mail_mode':'existing local manual confirmation and fixed test recipient',
            'background_scheduler_enabled':False},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'shared_trial_url':origin,'identity_isolation':False,
            'same_local_database':True,'mail_mode':'manual confirmation via existing local service'},ensure_ascii=False),flush=True)
        import uvicorn
        uvicorn.run(application,host='127.0.0.1',port=args.port,access_log=False,proxy_headers=False)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()

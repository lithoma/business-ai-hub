#!/usr/bin/env python3
"""UI-only local preview: real exported YAML, no cloud clients, writes or AI replies."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'business-ai-hub'))
os.environ['TASK_PARSER_ENDPOINT'] = ''
os.environ['TASK_EXECUTION_ENABLED'] = 'false'
os.environ['TASK_EMAIL_ENABLED'] = 'false'
import yaml
import app as backend
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ENGINE = ROOT / 'business-task-manager-v1' / 'business-task-manager-v1'


def unavailable(*args, **kwargs):
    raise RuntimeError('Local UI preview has no Databricks connection')


def local_yaml_tasks():
    tasks = []
    for path in sorted((ENGINE / 'tasks').glob('*/config.yaml')):
        task = yaml.safe_load(path.read_text(encoding='utf-8'))
        task.update(_task_id=path.parent.name, _config_path=str(path))
        tasks.append(task)
    return tasks


backend._workspace = unavailable
backend._exec_sql = unavailable
backend._ask_genie = unavailable
backend._load_task_configs = local_yaml_tasks
backend._load_registry_tasks = lambda: []  # No local Registry records; banner explains scope.
backend._ws_read_yaml = lambda path: yaml.safe_load((ENGINE / 'settings.yaml').read_text(encoding='utf-8')) if path.endswith('/settings.yaml') else unavailable()


@backend.app.middleware('http')
async def preview_scope(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        text = 'Model not configured：本地界面预览不调用模型，可手动编辑并预览任务。' if request.url.path.endswith('/generate') else '本地界面预览：未连接 Databricks，不保存草稿、不运行任务、不发送邮件。'
        return JSONResponse({'detail': text}, status_code=503)
    if request.url.path == '/':
        index = (backend.FRONTEND_DIST / 'index.html').read_text(encoding='utf-8')
        banner = '<div role="status" style="position:fixed;bottom:8px;left:8px;z-index:9999;background:#fff3c4;color:#493600;padding:10px 16px;border:1px solid #e7ca65;border-radius:8px;font:13px Arial;max-width:90vw">本地 UI 预览 · 仅读取本地 YAML · 不保存、不调用 AI、不运行任务、不发邮件</div>'
        return HTMLResponse(index.replace('</body>', banner + '</body>'))
    return await call_next(request)


if __name__ == '__main__':
    uvicorn.run(backend.app, host='127.0.0.1', port=8765)

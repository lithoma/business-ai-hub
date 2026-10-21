#!/usr/bin/env python3
"""Interactive local task testing. Drafts stay in SQLite; no Jobs or email.
SQL tools use Genie and bounded read-only Databricks queries only on explicit clicks.

Real parser access is opt-in. A finite budget is the default; interactive use
without a call limit requires separate explicit user authorization.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'business-ai-hub'))
ENGINE = ROOT / 'business-task-manager-v1' / 'business-task-manager-v1'


def build_app(database, *, endpoint='', profile='business-ai-hub', model_limit=3, interactive_analysis=False,
              conversation_workflow=False, analysis_provider='genie_one', genie_one_chat=False,
              manual_trials=False, trial_recipient=''):
    os.environ.setdefault('SQL_WAREHOUSE_ID','a54c7a1cd51e6901')
    os.environ['HUB_AUTH_MODE'] = 'local_cli'
    os.environ['HUB_LOCAL_PROFILE'] = profile
    os.environ['TASK_PARSER_ENDPOINT'] = endpoint
    os.environ['TASK_EXECUTION_ENABLED'] = 'true'
    os.environ['TASK_CONDITION_SQL_ENABLED'] = 'true'
    os.environ['TASK_EMAIL_ENABLED'] = 'false'
    os.environ['HUB_LOCAL_WORKFLOW'] = 'true' if conversation_workflow else 'false'
    os.environ['CHAT_ANALYSIS_PROVIDER'] = analysis_provider
    os.environ['GENIE_ONE_ENABLED'] = 'true' if genie_one_chat else 'false'
    database = Path(database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    os.environ['HUB_TEST_METRICS_DB']=str(database.with_name(database.stem+'-metrics.sqlite3'))
    from telemetry import observed_workspace, recorder, span
    recorder()  # Initialize locally; no Databricks calls or cloud resources.
    # Keep SQL-test receipts valid across local server restarts. This is not an auth credential.
    secret_file = database.with_suffix('.sql-review.key')
    if not secret_file.exists():
        import secrets
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(secrets.token_hex(32))
    os.environ.setdefault('TASK_SQL_REVIEW_SECRET', secret_file.read_text())
    import app as backend
    import yaml
    from fastapi.responses import HTMLResponse, JSONResponse
    database = Path(database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS task_registry (
            task_id TEXT PRIMARY KEY, name TEXT, description TEXT, enabled BOOLEAN,
            schedule_config TEXT, trigger_config TEXT, report_config TEXT, owner TEXT,
            lifecycle_status TEXT, created_at TEXT, updated_at TEXT)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS task_runs (
            run_id TEXT PRIMARY KEY, task_id TEXT, task_name TEXT, source TEXT, scheduled_slot TEXT,
            dispatch_date TEXT, status TEXT, trigger_reason TEXT, report_text TEXT, error_code TEXT,
            notification_status TEXT, started_at TEXT, finished_at TEXT, provider_message_id TEXT,
            notification_error_code TEXT, artifacts_json TEXT, config_json TEXT)''')
    with sqlite3.connect(database) as connection:
        if 'config_json' not in [r[1] for r in connection.execute('PRAGMA table_info(task_runs)')]:
            connection.execute('ALTER TABLE task_runs ADD COLUMN config_json TEXT')
    def local_sql(statement, parameters=None):
        # Thin SQL dialect compatibility for the production App's CRUD, with real SQLite persistence.
        statement = statement.replace(backend.REGISTRY_TABLE, 'task_registry').replace('workspace.douyin.business_task_runs_v1', 'task_runs')
        statement = re.sub(r'CAST\((created_at|updated_at) AS STRING\)', r'\1', statement, flags=re.I)
        statement = statement.replace('current_timestamp()', 'dev_now()')
        with sqlite3.connect(database, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            def json_field(raw, path):
                try:
                    value=json.loads(raw or '{}')
                    for key in path.removeprefix('$.').split('.'): value=value[key]
                    return value if isinstance(value,(str,int,float)) else None
                except (ValueError,KeyError,TypeError): return None
            connection.create_function('get_json_object',2,json_field)
            connection.create_function('dev_now', 0, lambda: datetime.now(timezone.utc).isoformat())
            cursor = connection.execute(statement, parameters or {})
            if cursor.description:
                result = [dict(row) for row in cursor.fetchall()]
                for row in result:
                    if 'enabled' in row: row['enabled'] = bool(row['enabled'])
                return result
            return [{'num_affected_rows': cursor.rowcount}]
    def local_tasks():
        tasks = []
        for path in sorted((ENGINE / 'tasks').glob('*/config.yaml')):
            task = yaml.safe_load(path.read_text(encoding='utf-8'))
            task.update(_task_id=path.parent.name, _config_path=str(path))
            tasks.append(task)
        return tasks
    def cloud_disabled(*args, **kwargs):
        raise RuntimeError('Local development mode: cloud operation disabled')
    def measured_local_sql(statement,parameters=None):
        with span('local_database',kind='local_sqlite'):
            return local_sql(statement,parameters)
    backend._exec_sql = measured_local_sql
    backend._load_task_configs = local_tasks
    backend._ws_read_yaml = lambda path: yaml.safe_load((ENGINE / 'settings.yaml').read_text(encoding='utf-8')) if path.endswith('/settings.yaml') else cloud_disabled()
    if not interactive_analysis: backend._ask_genie = cloud_disabled
    backend._workspace = cloud_disabled
    # Condition tools only issue Genie Chat / bounded SELECT calls, never CRUD SQL.
    from databricks.sdk import WorkspaceClient
    condition_client = None
    def condition_workspace():
        from hub_auth import business_workspace
        return business_workspace()
    backend._condition_workspace = condition_workspace
    if interactive_analysis: backend._chat_workspace = condition_workspace
    # Parser inference stays opt-in; interactive analysis uses its separate explicit flag.
    os.environ['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + os.environ.get('PATH', '')
    calls = 0
    if endpoint:
        from databricks.sdk import WorkspaceClient
        from task_llm import generate_configuration
        from task_llm import LLMNotConfigured
        workspace = None
        lock = threading.Lock()
        def parser_workspace():
            from hub_auth import business_workspace
            return business_workspace()
        def counted_call(callback, requirements, context):
            nonlocal calls
            with lock:
                if model_limit is not None and calls >= model_limit:
                    raise LLMNotConfigured('本轮授权的自然语言解析次数已用完，未调用模型；现有草稿仍可使用下方 Genie SQL 工具。')
                calls += 1
            return callback(parser_workspace, requirements, **context)
        def capped_parser(unused_workspace, requirements, **context):
            return counted_call(generate_configuration, requirements, context)
        backend.generate_configuration = capped_parser
        from conversation_workflow import generate_route
        backend.generate_route = lambda unused_workspace, requirements, **context: counted_call(generate_route, requirements, context)
    if manual_trials:
        # Registered only by the loopback launcher, never by the deployed App.
        sys.path.insert(0,str(ENGINE))
        import importlib.util
        from local_trials import LocalTrials, register_routes
        from condition_sql import execute_readonly, parameters, test_with_genie, validate_sql
        from condition_decision import gather_context
        from agent_client import AgentClient
        from task_engine import TaskManager
        from report_email import build_message, email_address
        spec=importlib.util.spec_from_file_location('hub_trial_cli',ROOT/'scripts/test-local-tasks.py')
        trial_cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(trial_cli)
        settings=yaml.safe_load((ENGINE/'settings.yaml').read_text())
        state=database.parent/'manual-task-tests'
        def trial_condition(task,day):
            trigger=task['trigger'];review=task['report'].get('condition_sql' if trigger['type']=='sql' else 'condition_agent',{})
            values=parameters(day,review.get('window','day'));values['dispatch_date']=datetime.now(timezone.utc).date().isoformat()
            expected=None
            if review.get('local_test_only'):
                expected=json.loads((state/'previous-observed-expectations-20261021.json').read_text()).get(task['task_id'],{}).get(day)
                if expected is None: raise ValueError('此临时测试任务仅预备了 2025-06-24、2025-06-27 的独立校验数据；请选择其中一天，或恢复正式日期规则。')
            if trigger['type']=='sql':
                result=execute_readonly(condition_workspace(),backend.SQL_WAREHOUSE_ID,trigger['sql'],values)
                if expected:
                    if result['should_trigger'] is not expected['should_trigger']: raise ValueError('SQL 判断与独立金额计算不一致。')
                    values.update(trial_cli.observed_test_context(result['reason'],expected))
            elif trigger['type']=='agent':
                agents=list(dict.fromkeys([trigger['agent'],*trigger.get('context_agents',[])]))
                sources=[]
                result=test_with_genie(condition_workspace(),'genie_one',trigger['prompt'],values,sources)
            elif trigger['type']=='always': result={'should_trigger':True,'reason':'用户明确发起的一次手动试运行。'}
            else: raise ValueError('请先补全触发条件。')
            return result,values
        def trial_report(task,values,reason):
            manager=TaskManager.__new__(TaskManager);manager.agents=AgentClient(settings['agents'], workspace=condition_workspace())
            manager.agents._workspace=condition_workspace()
            return manager._report(task,ENGINE,values,reason)
        credentials=Path.home()/'.config/business-ai-hub/gmail-credentials.json'
        sender=json.loads(credentials.read_text()).get('authorized_email','') if credentials.is_file() else ''
        recipient=email_address(trial_recipient) if trial_recipient else ''
        def trial_preview(row,snapshot):
            snapshot['report']['delivery']={'enabled':True,'to':[recipient],'cc':[],'template_id':'default',
                'subject_template':'[手动测试] {{ task_name }} | {{ business_date }}','csv_exports':[]}
            config=dict(settings['notification']['email'],from_address=sender,allowed_recipients=[recipient])
            run=dict(row,task_name=snapshot.get('name','任务报告'),config_json=json.dumps(snapshot,ensure_ascii=False))
            return build_message(None,ENGINE,config,run)[0]
        service=LocalTrials(database,database.parent/'local-trials',backend._load_registry_tasks,
            trial_condition,trial_report,trial_preview,sender,recipient,
            (lambda raw:trial_cli.gmail(sender).send(raw)) if sender and recipient else None)
        # Make the already generated/mailed reports available without another paid call.
        for file in list(state.glob('*/*/execution-snapshot.json'))[:500]:
            result_file=file.with_name('result.json')
            result=json.loads(result_file.read_text()) if result_file.is_file() else {}
            timings={key:result[key] for key in ('condition_seconds','report_seconds','duration_seconds') if key in result}
            service.import_report(file.parent.name,json.loads(file.read_text()),timings)
        register_routes(backend.app,service)
        backend.app.state.local_trials=service
    @backend.app.middleware('http')
    async def local_scope(request, call_next):
        if request.url.path in ('/api/chat','/api/conversation/analysis') and not interactive_analysis:
            return JSONResponse({'detail': '本地任务测试未启用 Genie 分析。创建定期任务请使用“设置自动任务”。'}, status_code=503)
        if request.url.path == '/':
            index = (backend.FRONTEND_DIST / 'index.html').read_text(encoding='utf-8')
            if not endpoint:
                model = '真实解析未开启，可手动创建'
            elif model_limit is None:
                model = '真实解析已开启 · 仅在你点击生成' + ('或发送消息' if conversation_workflow else '') + '时调用 · 不设测试次数限制'
            else:
                remaining = max(0,model_limit-calls)
                model = f'真实解析已开启，剩余 {remaining} 次调用' if remaining else '本轮自然语言解析额度已用完；仍可用 Genie 生成条件 SQL'
            if interactive_analysis: model += ' · AI 分析在发送问题时调用 ' + 'Genie One' + '（CLI 身份）'
            if conversation_workflow: model += ' · 统一分流已开启，业务能力统一 Genie One；保存的 SQL 直接复用'
            banner = '<style>#local-test-banner{position:fixed;bottom:10px;left:10px;z-index:9999;background:#e5f4fc;color:#174b68;padding:10px;border:1px solid #8ec7e7;border-radius:8px;font:11px/1.8 Arial;width:232px}#local-test-banner summary{cursor:pointer;font-weight:600}@media(max-width:950px){#local-test-banner{position:static;width:auto;margin:10px}}</style><details id="local-test-banner"><summary>本地测试 · 不自动运行或发信</summary>草稿保存在本机 SQLite · ' + model + ' · 本地可审核启用 · 条件工具会读取 Databricks · 不运行 Job、不自动发邮件</details>'
            return HTMLResponse(index.replace('</body>', banner + '</body>'))
        return await call_next(request)
    return backend.app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path.home() / '.local/share/business-ai-hub/dev.sqlite3')
    parser.add_argument('--parser-endpoint', default='')
    parser.add_argument('--profile', default='business-ai-hub')
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument('--model-limit', type=int, default=3)
    budget.add_argument('--interactive-model', action='store_true',
                        help='User-authorized interactive parser use without a fixed call limit; requests only, no background calls')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--interactive-analysis', action='store_true', help='Enable user-clicked Genie analysis and conversation continuation with the CLI identity')
    parser.add_argument('--conversation-workflow', action='store_true', help='Enable the one-call semantic router and draft extraction, only when the user sends a message')
    parser.add_argument('--analysis-provider', choices=('genie_one',), default='genie_one',
                        help='All business calls use Genie One; saved SQL runs directly')
    parser.add_argument('--enable-genie-one-chat', action='store_true', help='Explicitly enable user-requested local Genie One analytics testing; no automatic model smoke test')
    parser.add_argument('--manual-trials', action='store_true', help='Enable explicit local report trials; emails require separate preview and confirmation')
    parser.add_argument('--trial-recipient', default='', help='Fixed authorized recipient for manual mail tests only')
    args = parser.parse_args()
    if not 0 <= args.model_limit <= 10: parser.error('Model limit must be 0–10')
    if args.interactive_model and not args.parser_endpoint: parser.error('--interactive-model requires --parser-endpoint')
    if args.enable_genie_one_chat and not (args.analysis_provider == 'genie_one' and args.interactive_analysis and args.conversation_workflow):
        parser.error('--enable-genie-one-chat requires --analysis-provider genie_one, --interactive-analysis and --conversation-workflow')
    application = build_app(args.database, endpoint=args.parser_endpoint, profile=args.profile,
                            model_limit=None if args.interactive_model else args.model_limit,
                            interactive_analysis=args.interactive_analysis,
                            conversation_workflow=args.conversation_workflow, analysis_provider=args.analysis_provider,
                            genie_one_chat=args.enable_genie_one_chat,manual_trials=args.manual_trials,trial_recipient=args.trial_recipient)
    import uvicorn
    uvicorn.run(application, host='127.0.0.1', port=args.port)


if __name__ == '__main__': main()

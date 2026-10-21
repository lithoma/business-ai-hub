#!/usr/bin/env python3
"""Explicit, bounded live task trials. Local history only; never calls Jobs or cloud CRUD.

Uses the existing condition API, TaskManager report method and Gmail MIME/provider.
No retry of a claimed case or an uncertain mail send. Credentials/artifacts stay outside Git.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'business-task-manager-v1/business-task-manager-v1'
sys.path[:0] = [str(ROOT / 'business-ai-hub'), str(ENGINE)]
from telemetry import record_action, span, observed_workspace


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def api(path, body=None, method=None, trace=None):
    request = urllib.request.Request('http://127.0.0.1:8765' + path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
        method=method, headers={'Content-Type': 'application/json', 'X-Hub-Trace-Id': trace or str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(request, timeout=100) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = json.load(exc).get('detail', 'Local API request failed')
        raise ValueError(f'HTTP {exc.code}: {detail}') from None


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.chmod(0o600)
    temp.replace(path)


def ledger(database, key, fields):
    with sqlite3.connect(database, timeout=10) as connection:
        connection.execute('UPDATE task_runs SET ' + ','.join(name+'=?' for name in fields) + ' WHERE run_id=?',
            [*fields.values(), key])


def gmail(sender):
    # Never print values, tokens or authentication exception contents.
    from google.oauth2.credentials import Credentials
    from report_email import GmailProvider
    values = json.loads((Path.home()/'.config/business-ai-hub/gmail-credentials.json').read_text())
    if values.get('authorized_email', '').casefold() != sender.casefold():
        raise ValueError('Project Gmail authorization does not match sender')
    return GmailProvider(Credentials(token=None, client_id=values['client_id'], client_secret=values['client_secret'],
        refresh_token=values['refresh_token'], token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/gmail.send']))


def send_once(provider, raw, folder):
    from report_email import EmailOutcomeUnknown
    claim = folder/'email-attempt.json'
    fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'status': 'EMAIL_SENDING', 'started_at': timestamp()}, stream)
    started = time.monotonic()
    try:
        with span('gmail_task_trial', kind='mail_adapter', provider='gmail') as event:
            message_id = provider.send(raw)
            event['api_accepted'] = True
        result = {'status': 'EMAIL_ACCEPTED', 'provider_message_id': message_id}
    except EmailOutcomeUnknown:
        result = {'status': 'EMAIL_UNKNOWN', 'error': 'ProviderOutcomeUnknown'}
    except Exception as exc:
        result = {'status': 'EMAIL_FAILED', 'error': type(exc).__name__}
    result['duration_seconds'] = round(time.monotonic()-started, 3)
    save(claim, result)
    return result


def observed_test_context(reason, expected):
    """Test SQL emits explicit date/metric markers; no production fallback is added."""
    match = re.search(r'比较日期=(\d{4}-\d{2}-\d{2})', reason)
    if not match or match.group(1) != expected['comparison_date']:
        raise ValueError('SQL comparison date disagrees with independent source data')
    for label, field in (('当前GMV', 'current_gmv'), ('比较GMV', 'previous_gmv')):
        value = re.search(label+r'=([-+\d.]+)', reason)
        if not value or Decimal(value.group(1)) != Decimal(expected[field]):
            raise ValueError('SQL metric value disagrees with independent source data')
    return {'comparison_basis':'previous_observed_day', 'comparison_date':match.group(1),
        'previous_period_start':match.group(1), 'previous_period_end':match.group(1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', action='append', required=True)
    parser.add_argument('--date', dest='dates', action='append', required=True)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--profile', default='business-ai-hub')
    parser.add_argument('--sender', default='ltytianyi2004@gmail.com')
    parser.add_argument('--recipient', default='tianyili397@gmail.com')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--reports', action='store_true')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--activate-passed', action='store_true')
    parser.add_argument('--expectations', type=Path, help='Independent arithmetic results keyed by task ID and date')
    parser.add_argument('--observed-day-test', action='store_true', help='Local test-only date context; production logic is unchanged')
    parser.add_argument('--restore-snapshot', type=Path, help='Return selected test tasks to their saved original drafts; no inference/mail')
    args = parser.parse_args()
    if not args.batch.replace('-', '').isalnum(): parser.error('Use an alphanumeric batch name')
    if len(args.task_id) != 2 or len(set(args.task_id)) != 2 or len(args.dates) != 2 or len(set(args.dates)) != 2:
        parser.error('This trial is bounded to exactly two tasks and two distinct dates')
    for day in args.dates: date.fromisoformat(day)
    if args.send and not args.reports: parser.error('--send requires --reports')
    if api('/api/conversation/settings').get('identity_mode') != 'single_user_local':
        parser.error('Refusing task writes outside the verified single-user local test server')
    tasks = {task['task_id']: task for task in api('/api/tasks')['tasks']}
    if args.restore_snapshot:
        snapshots=json.loads(args.restore_snapshot.read_text())
        if {item['task_id'] for item in snapshots} != set(args.task_id):
            parser.error('Snapshot IDs must exactly match the selected tasks')
        if not args.execute:
            print(json.dumps({'status':'restore_plan_only','task_ids':args.task_id})); return
        for snapshot in snapshots:
            key=snapshot['task_id']; current=tasks[key]
            for status,action in (('active','pause'),('paused','edit')):
                if current['lifecycle_status']==status:
                    api('/api/tasks/'+key+'/lifecycle',{'action':action,'confirm':True,'reviewed':True,
                        'expected_updated_at':current['updated_at']})
                    current=next(item for item in api('/api/tasks')['tasks'] if item['task_id']==key)
            body={name:snapshot[name] for name in ('name','description','schedule','trigger','report','owner')}
            api('/api/tasks/'+key,dict(body,expected_updated_at=current['updated_at']),method='PUT')
        print(json.dumps({'status':'original_drafts_restored','task_ids':args.task_id})); return
    selected = [tasks[key] for key in args.task_id]
    if any(task.get('_source') != 'registry' or task.get('lifecycle_status') != 'draft'
           or task.get('trigger', {}).get('type') != 'sql' for task in selected):
        parser.error('Only the two explicitly selected SQL drafts may be tested/activated')
    if args.observed_day_test and not args.expectations:
        parser.error('Observed-day tests require independent date/metric expectations')
    if any(task['report'].get('condition_sql',{}).get('local_test_only') for task in selected) and not args.observed_day_test:
        parser.error('These temporary tasks require --observed-day-test; restore the original snapshot for calendar-day testing')
    if not args.execute:
        print(json.dumps({'status': 'plan_only', 'tasks': [task['name'] for task in selected],
            'dates': args.dates, 'condition_tests': 4, 'max_report_calls': 4 if args.reports else 0,
            'max_emails': 4 if args.send else 0, 'cloud_writes': 0}, ensure_ascii=False))
        return
    output = Path.home()/'.local/share/business-ai-hub/manual-task-tests'/args.batch
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = Path.home()/'.local/share/business-ai-hub/dev.sqlite3'
    import yaml
    from condition_sql import parameters
    from task_engine import TaskManager
    from agent_client import AgentClient
    from report_email import build_message
    from databricks.sdk import WorkspaceClient
    settings = yaml.safe_load((ENGINE/'settings.yaml').read_text())
    manager = TaskManager.__new__(TaskManager)
    manager.agents = AgentClient(settings['agents'])
    # This manual adapter invokes _report only, not the dispatcher or its notification loop.
    client = None
    provider = gmail(args.sender) if args.send else None
    email_config = dict(settings['notification']['email'], from_address=args.sender,
        allowed_recipients=[args.recipient], enabled=True)
    summary = {'batch': args.batch, 'started_at': timestamp(), 'mode': 'manual_local_trial',
        'cases': [], 'tasks': [], 'emails_accepted': 0, 'actual_cost': None, 'genie_internal_tokens': None}
    save(output/'tasks-before.json', selected)
    save(output/'summary.json', summary)
    expected_cases = json.loads(args.expectations.read_text()) if args.expectations else None
    for task in selected:
        results = []
        last_review = None
        for day in args.dates:
            started = time.monotonic()
            key = hashlib.sha256((args.batch+'\0'+task['task_id']+'\0'+day).encode()).hexdigest()
            folder = output/key
            folder.mkdir(mode=0o700)
            trace = str(uuid.uuid4())
            result = {'run_id': key, 'task_id': task['task_id'], 'task_name': task['name'],
                'business_date': day, 'trace_id': trace, 'status': 'RUNNING'}
            with sqlite3.connect(database, timeout=10) as connection:
                connection.execute('''INSERT INTO task_runs
                    (run_id,task_id,task_name,source,scheduled_slot,dispatch_date,status,trigger_reason,
                     report_text,error_code,notification_status,started_at,finished_at,artifacts_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (key,task['task_id'],task['name'],'manual_local',f'manual:{args.batch}:{day}',
                     date.today().isoformat(),'RUNNING','','','','NOT_QUEUED',timestamp(),'','[]'))
            try:
                phase = time.monotonic()
                reviewed = api('/api/conditions/sql/test', {'sql': task['trigger']['sql'],
                    'window': task['report'].get('condition_sql', {}).get('window', 'day'),
                    'business_date': day, 'confirm': True}, trace=trace)
                result.update(condition_seconds=round(time.monotonic()-phase, 3), condition=reviewed['test'])
                if expected_cases is not None:
                    expected = expected_cases[task['task_id']][day]
                    result['independent_check'] = expected
                    if reviewed['test']['should_trigger'] is not expected['should_trigger']:
                        raise ValueError('SQL decision disagrees with independent metric arithmetic')
                    if args.observed_day_test:
                        comparison=observed_test_context(reviewed['test']['reason'],expected)
                        reviewed['test']['comparison_context']=comparison
                        reviewed['bindings'].update(comparison)
                last_review = reviewed
                fired = reviewed['test']['should_trigger']
                result['status'] = 'TRIGGER_CONFIRMED' if fired else 'NOT_TRIGGERED'
                ledger(database,key,{'trigger_reason': reviewed['test']['reason']})
                if fired and args.reports:
                    if client is None:
                        client = observed_workspace(WorkspaceClient(profile=args.profile))
                        manager.agents._workspace = client
                    variables = parameters(day, reviewed['window'])
                    variables.update(reviewed['test'].get('comparison_context',{}))
                    variables.update(dispatch_date=date.today().isoformat())
                    snapshot = deepcopy(task)
                    snapshot['_execution_context'] = variables
                    snapshot['report']['delivery'] = {'enabled': True, 'to': [args.recipient], 'cc': [],
                        'template_id': 'default', 'subject_template': '[手动测试] {{ task_name }} | {{ business_date }}', 'csv_exports': []}
                    save(folder/'execution-snapshot.json', snapshot)
                    phase = time.monotonic()
                    with span('manual_task_report', kind='report', provider=task['report']['agent'], run_id=key):
                        report = manager._report(snapshot, ENGINE, variables, reviewed['test']['reason'])
                    result.update(report_seconds=round(time.monotonic()-phase, 3), status='REPORT_CREATED')
                    (folder/'report.md').write_text(report)
                    ledger(database,key,{'status':'REPORT_CREATED','report_text':report})
                    run = {'run_id': key, 'task_name':task['name'], 'config_json':json.dumps(snapshot,ensure_ascii=False),
                        'report_text':report,'trigger_reason':reviewed['test']['reason']}
                    raw, artifacts = build_message(None, ENGINE, email_config, run)
                    (folder/'message.eml').write_bytes(raw)
                    if args.send:
                        ledger(database,key,{'notification_status':'EMAIL_SENDING'})
                        mailed = send_once(provider,raw,folder)
                        result['mail'] = mailed
                        ledger(database,key,{'notification_status':mailed['status'],
                            'provider_message_id':mailed.get('provider_message_id',''),
                            'notification_error_code':mailed.get('error',''), 'artifacts_json':json.dumps(artifacts)})
                        if mailed['status'] == 'EMAIL_ACCEPTED': summary['emails_accepted'] += 1
            except Exception as exc:
                result.update(status='FAILED', error_type=type(exc).__name__,
                    error=str(exc)[:1500] if isinstance(exc,ValueError) else type(exc).__name__)
            result['duration_seconds'] = round(time.monotonic()-started, 3)
            ledger(database,key,{'status': result['status'],
                'error_code':result.get('error_type',''),'finished_at':timestamp()})
            save(folder/'result.json',result)
            results.append(result); summary['cases'].append(result)
            save(output/'summary.json',summary)
            print(json.dumps({k:v for k,v in result.items() if k not in ('condition',)},ensure_ascii=False),flush=True)
        task_result = {'task_id':task['task_id'],'name':task['name'],'activated':False}
        report_exercised = not args.reports or any(case['status']=='REPORT_CREATED' for case in results)
        mail_ok = not args.send or all(case.get('mail',{}).get('status')=='EMAIL_ACCEPTED'
            for case in results if case['status']=='REPORT_CREATED')
        if args.activate_passed and last_review and report_exercised and mail_ok and all(case['status']!='FAILED' for case in results):
            try:
                current = next(item for item in api('/api/tasks')['tasks'] if item['task_id']==task['task_id'])
                if current['updated_at'] != task['updated_at']: raise ValueError('Task changed during trial; not activated')
                report = deepcopy(current['report'])
                report['condition_sql'] = dict(report.get('condition_sql',{}), **last_review)
                # Automatic email remains off. These sends are explicit manual trials only.
                api('/api/tasks/'+task['task_id'], {'report':report,'expected_updated_at':current['updated_at']},method='PUT')
                ready = api('/api/tasks/'+task['task_id']+'/readiness')
                if not ready['can_activate']: raise ValueError('; '.join(ready['errors']))
                api('/api/tasks/'+task['task_id']+'/lifecycle', {'action':'activate','confirm':True,
                    'reviewed':True,'expected_updated_at':ready['updated_at']})
                task_result['activated'] = True
            except Exception as exc:
                task_result['error'] = str(exc)[:1500] if isinstance(exc,ValueError) else type(exc).__name__
        summary['tasks'].append(task_result)
        save(output/'summary.json',summary)
    summary['finished_at'] = timestamp()
    save(output/'summary.json',summary)
    print(json.dumps({'summary':str(output/'summary.json'),'emails_accepted':summary['emails_accepted'],
        'tasks':summary['tasks']},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    os.environ['PATH'] = str(Path.home()/'.local/bin')+os.pathsep+os.environ.get('PATH','')
    os.environ.setdefault('HUB_TEST_METRICS_DB',str(Path.home()/'.local/share/business-ai-hub/dev-metrics.sqlite3'))
    with record_action('manual_local_task_batch'): main()

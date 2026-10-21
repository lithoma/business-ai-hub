"""Opt-in local test measurements. No prompts, SQL, credentials or response text.

One local SQLite insert/update per observed event; no polling, external collector,
model requests or billing queries. Unknown provider usage/cost remains NULL.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import functools
from enum import Enum
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


_current = ContextVar('hub_test_request',default=None)
_recorders = {}
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def usage_fields(value):
    value = value if isinstance(value,dict) else {}
    def number(*keys):
        for key in keys:
            item=value.get(key)
            if type(item) is int and item >= 0: return item
        return None
    return {'input_tokens':number('prompt_tokens','input_tokens'),
            'output_tokens':number('completion_tokens','output_tokens'),
            'total_tokens':number('total_tokens')}


class Recorder:
    def __init__(self,path):
        self.path=Path(path).expanduser().resolve()
        self.dropped=0
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.path,os.O_CREAT|os.O_WRONLY,0o600); os.close(fd)
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('''CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, trace_id TEXT, operation TEXT, method TEXT,
              started_at TEXT, duration_ms REAL, status_code INTEGER, error_type TEXT,
              response_bytes INTEGER, state TEXT NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS spans (
              span_id TEXT PRIMARY KEY, request_id TEXT, trace_id TEXT, name TEXT,
              kind TEXT, provider TEXT, started_at TEXT, duration_ms REAL,
              outcome TEXT, error_type TEXT, details_json TEXT)''')

    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.path,timeout=1)
        try:
            with connection: yield connection
        finally:
            connection.close()

    def write(self,statement,values):
        try:
            with self.connect() as connection: connection.execute(statement,values)
        except Exception:
            self.dropped+=1
            logging.getLogger(__name__).warning('Local test measurement write failed; application request was not retried')

    def start(self,request_id,trace_id,operation,method):
        self.write('INSERT INTO requests VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL,?)',
                   (request_id,trace_id,operation,method,now(),'running'))

    def finish(self,request_id,duration,status,error,size):
        self.write('UPDATE requests SET duration_ms=?,status_code=?,error_type=?,response_bytes=?,state=? WHERE request_id=?',
                   (duration,status,error,size,'error' if error or status >= 400 else 'completed',request_id))

    def export(self):
        with self.connect() as connection:
            connection.row_factory=sqlite3.Row
            requests=[dict(row) for row in connection.execute('SELECT * FROM requests ORDER BY started_at')]
            spans=[dict(row) for row in connection.execute('SELECT * FROM spans ORDER BY started_at')]
        counts={}
        reported={'input_tokens':0,'output_tokens':0,'total_tokens':0}
        known={key:0 for key in reported}
        for span in spans:
            span['details']=json.loads(span.pop('details_json'))
            key=span['kind']+':'+span['name']
            counts[key]=counts.get(key,0)+1
            # Only one-shot inference responses carry usage, not cumulative MCP polls.
            if span['kind']=='inference':
                for key in reported:
                    value=span['details'].get(key)
                    if type(value) is int: reported[key]+=value; known[key]+=1
        return {'schema_version':1,'exported_at':now(),'requests':requests,'spans':spans,
                'summary':{'requests':len(requests),'failed_requests':sum(row['state']=='error' for row in requests),
                           'in_progress_requests':sum(row['state']=='running' for row in requests),
                           'observed_application_calls':counts,
                           'reported_inference_tokens':{key:reported[key] if known[key] else None for key in reported},
                           'inference_usage_coverage':known,'dropped_measurement_writes':self.dropped,
                           'actual_cost':None,'genie_internal_tokens':None,'warehouse_dbu':None},
                'limitations':['Spans may overlap: do not sum nested times as total latency.',
                               'API duration ends after response transmission; no browser rendering or user think time.',
                               'Counts are observed application/SDK calls, not all provider-internal HTTP retries or hidden agent/model calls.',
                               'Unknown tokens, internal Genie SQL, DBUs and cost are not zero. Reported token totals may be partial.',
                               'Test logging begins only after this instrumentation was enabled; no historical backfill.']}


def recorder():
    path=os.getenv('HUB_TEST_METRICS_DB','').strip()
    if not path: return None
    with _lock:
        if path not in _recorders:
            try: _recorders[path]=Recorder(path)
            except Exception:
                logging.getLogger(__name__).warning('Local test recording unavailable')
                return None
        return _recorders[path]


@contextmanager
def record_action(operation):
    """Same recorder for explicitly invoked local CLI operations, not a scheduler."""
    store=recorder()
    if store is None:
        yield
        return
    request_id=uuid.uuid4().hex;trace_id=str(uuid.uuid4());started=time.perf_counter();error=None
    store.start(request_id,trace_id,operation,'CLI')
    token=_current.set({'recorder':store,'request_id':request_id,'trace_id':trace_id})
    try: yield
    except BaseException as exc:
        error=type(exc).__name__
        raise
    finally:
        store.finish(request_id,round((time.perf_counter()-started)*1000,3),500 if error else 200,error,0)
        _current.reset(token)


@contextmanager
def span(name,*,kind='application',provider='',**details):
    context=_current.get()
    if context is None:
        yield details
        return
    started=time.perf_counter(); timestamp=now(); error=None
    try:
        yield details
    except BaseException as exc:
        error=type(exc).__name__
        raise
    finally:
        # All details are explicitly assembled at call sites; never accept raw
        # request/response dictionaries or exception messages here.
        context['recorder'].write('INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex,context['request_id'],context['trace_id'],name,kind,provider,
             timestamp,round((time.perf_counter()-started)*1000,3),
             'error' if error else 'completed',error,json.dumps(details,ensure_ascii=False)))


class TestMetricsMiddleware:
    def __init__(self,app): self.app=app

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if (scope['type']!='http' or not path.startswith('/api/') or path in
                ('/api/health','/api/conversation/settings','/api/test-metrics/export')):
            return await self.app(scope,receive,send)
        store=recorder()
        if store is None: return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        try: trace_id=str(uuid.UUID(headers.get(b'x-hub-trace-id',b'').decode()))
        except (ValueError,UnicodeDecodeError): trace_id=str(uuid.uuid4())
        request_id=uuid.uuid4().hex
        # Route templates do not contain SQL, task IDs, question strings or email addresses.
        known={'/api/chat','/api/tasks','/api/tasks/create','/api/tasks/generate',
               '/api/conversation/route','/api/conversation/analysis','/api/conditions/sql/generate',
               '/api/conditions/sql/test','/api/conditions/agent/test','/api/reports','/api/report-options'}
        operation=path if path in known else '/api/task_or_report_operation'
        if path.startswith('/api/tasks/') and path not in known:
            operation='/api/tasks/{task_id}'+('/'+path.rsplit('/',1)[-1] if path.endswith(('/lifecycle','/readiness')) else '')
        started=time.perf_counter()
        store.start(request_id,trace_id,operation,scope['method'])
        token=_current.set({'recorder':store,'request_id':request_id,'trace_id':trace_id})
        status=500; size=0; error=None
        async def measured_send(message):
            nonlocal status,size
            if message['type']=='http.response.start':
                status=message['status']
                message={**message,'headers':list(message.get('headers',[]))+
                         [(b'x-hub-request-id',request_id.encode()),(b'x-hub-trace-id',trace_id.encode())]}
            if message['type']=='http.response.body': size+=len(message.get('body',b''))
            await send(message)
        try:
            await self.app(scope,receive,measured_send)
        except BaseException as exc:
            error=type(exc).__name__
            raise
        finally:
            store.finish(request_id,round((time.perf_counter()-started)*1000,3),status,error,size)
            _current.reset(token)


class ObservedService:
    def __init__(self,service,name): self._service,self._name=service,name

    def __getattr__(self,name):
        target=getattr(self._service,name)
        kinds={
            'genie':{'start_conversation':'genie_turn','create_message':'genie_turn',
                     'start_conversation_and_wait':'genie_turn_sdk_waiter', 'get_message':'genie_poll',
                     'get_message_attachment_query_result':'genie_result'},
            'statement_execution':{'execute_statement':'sql_submission','get_statement':'sql_poll',
                                   'get_statement_result_chunk_n':'sql_result'},
            'current_user':{'me':'identity_lookup'},
            'workspace':{'download':'workspace_read'},
        }
        kind=kinds.get(self._name,{}).get(name)
        if not kind or not callable(target): return target
        @functools.wraps(target)
        def measured(*args,**kwargs):
            details={key:kwargs[key] for key in ('space_id','warehouse_id') if isinstance(kwargs.get(key),str)}
            if kind.endswith('sdk_waiter'): details['internal_poll_count']=None
            with span(name,kind=kind,provider='databricks_sdk',**details) as event:
                result=target(*args,**kwargs)
                source=getattr(result,'response',None) or result
                for key in ('statement_id','conversation_id','message_id'):
                    value=getattr(source,key,None)
                    if isinstance(value,str): event[key]=value
                state=getattr(getattr(result,'status',None),'state',None)
                if state is not None: event['provider_status']=str(state)
                elif isinstance(getattr(result,'status',None),(str,Enum)):
                    event['provider_status']=str(result.status)
                manifest=getattr(result,'manifest',None)
                for key in ('total_row_count','total_byte_count'):
                    value=getattr(manifest,key,None)
                    if type(value) is int: event[key]=value
                return result
        return measured


class ObservedWorkspace:
    def __init__(self,client): self._client=client; self._services={}
    def __getattr__(self,name):
        target=getattr(self._client,name)
        if name not in ('genie','statement_execution','current_user','workspace'): return target
        if name not in self._services: self._services[name]=ObservedService(target,name)
        return self._services[name]


def observed_workspace(client):
    return client if isinstance(client,ObservedWorkspace) else ObservedWorkspace(client)

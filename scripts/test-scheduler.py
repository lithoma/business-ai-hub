#!/usr/bin/env python3
"""Offline scheduler acceptance: isolated SQLite + explicit non-AI fixture adapters.

Never opens the development DB, authenticates to Databricks, queries business data,
calls models or sends email. A short real timer uses an accelerated test clock.
"""
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'business-ai-hub'))
sys.path.insert(0,str(ROOT/'business-task-manager-v1/business-task-manager-v1'))
from hub_auth import Identity, _context
from run_service import RunRepository, RunService
from scheduler_loop import SchedulerLoop


class Fixture:
    def __init__(self, directory):
        self.database=directory/'isolated-runs.sqlite3'
        self.calls=Counter()
        client=SimpleNamespace(config=SimpleNamespace(host='https://scheduler-fixture.invalid',client_id=None),
            current_user=SimpleNamespace(me=lambda:SimpleNamespace(id='offline',active=True,user_name='offline@example.invalid')))
        self.identity=Identity('offline','https://scheduler-fixture.invalid','offline@example.invalid','offline_test',client)
        self.tasks=[dict(task_id=key,name='离线调度测试 '+key,updated_at='fixture-v1',_source='registry',
            enabled=key!='paused',lifecycle_status='paused' if key=='paused' else 'active',
            schedule={'frequency':'daily','time':'09:00','timezone':'Asia/Shanghai'},
            trigger={'type':'always'},report={'agent':'genie_one','prompt':'固定离线测试，不调用模型','security':self.identity.security})
            for key in ('triggered','not_triggered','paused')]
        with closing(sqlite3.connect(self.database)) as db,db:
            db.execute('''CREATE TABLE runs (run_id TEXT PRIMARY KEY,task_id TEXT,task_name TEXT,source TEXT,
                scheduled_slot TEXT,dispatch_date TEXT,status TEXT,trigger_reason TEXT,report_text TEXT,
                error_code TEXT,notification_status TEXT,started_at TEXT,finished_at TEXT,config_json TEXT,
                artifacts_json TEXT,provider_message_id TEXT,notification_error_code TEXT)''')
            db.execute('CREATE UNIQUE INDEX run_slot ON runs(source,task_id,scheduled_slot)')
        self.repo=RunRepository(self.query,'offline.scheduler.runs',sqlite=True)
        self.service=self.new_service()

    def query(self,sql,params=None):
        def field(raw,path):
            value=json.loads(raw or '{}')
            for part in path.removeprefix('$.').split('.'):value=value.get(part,{})
            return value if isinstance(value,(str,int,float)) else json.dumps(value)
        with closing(sqlite3.connect(self.database,timeout=5)) as db,db:
            db.row_factory=sqlite3.Row
            db.create_function('get_json_object',2,field)
            cursor=db.execute(sql.replace('offline.scheduler.runs','runs'),params or {})
            return [dict(row) for row in cursor.fetchall()] if cursor.description else [{'num_affected_rows':cursor.rowcount}]

    @contextmanager
    def scope(self):
        token=_context.set(self.identity)
        try:yield
        finally:_context.reset(token)

    def condition(self,task,day):
        self.calls['condition']+=1
        return {'should_trigger':task['task_id']=='triggered','reason':'离线固定条件，非真实业务查询'}, {'business_date':day}

    def report(self,task,values,reason):
        self.calls['report']+=1
        return '离线固定测试文本，不是 AI 生成报告。'

    def forbidden_delivery(self,*args):
        self.calls['mail']+=1
        raise AssertionError('No preview or mail allowed in scheduler acceptance')

    def new_service(self):
        return RunService(self.repo,lambda:self.tasks,self.condition,self.report,self.forbidden_delivery,self.forbidden_delivery)

    def settle(self):
        deadline=time.monotonic()+3
        while self.service.pending and time.monotonic()<deadline:threading.Event().wait(0.01)
        if self.service.pending:raise AssertionError('Offline worker did not complete')

    def rows(self):return self.query('SELECT * FROM offline.scheduler.runs ORDER BY task_id,scheduled_slot')


def run_validation(directory):
    fixture=Fixture(directory)
    clock=[datetime.fromisoformat('2026-10-22T08:59:00+08:00')]
    loop=SchedulerLoop(fixture.service,fixture.scope,lambda task:fixture.identity,clock=lambda:clock[0])
    checks=[]
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append({'name':name,'passed':True})
    try:
        try:loop.start()
        except RuntimeError:pass
        else:raise AssertionError('Timer started without opt-in')
        check('默认不开启定时器',loop.status()['state']=='disabled' and not fixture.rows())
        loop.tick(enabled=True)
        check('整点之前不执行',not fixture.rows())
        clock[0]=clock[0].replace(hour=9,minute=0)
        loop.tick(enabled=True);fixture.settle()
        rows=fixture.rows()
        check('到点自动领取：true 生成报告，false 不生成',
            len(rows)==2 and {row['status'] for row in rows}=={'REPORT_CREATED','NOT_TRIGGERED'} and fixture.calls['report']==1)
        loop.tick(enabled=True);fixture.settle()
        check('同一时刻重复检查不重复执行',len(fixture.rows())==2 and fixture.calls['condition']==2)
        check('暂停任务不执行',all(row['task_id']!='paused' for row in rows))
        fixture.tasks[0].update(enabled=False,lifecycle_status='paused')
        clock[0]+=timedelta(days=1)
        loop.tick(enabled=True);fixture.settle()
        check('运行后暂停，次日跳过',len(fixture.rows())==3 and fixture.calls['report']==1)
        fixture.service.pool.shutdown(wait=True)
        fixture.service=fixture.new_service()
        loop=SchedulerLoop(fixture.service,fixture.scope,lambda task:fixture.identity,clock=lambda:clock[0])
        loop.tick(enabled=True);fixture.settle()
        check('重建执行器后保留领取记录，不重放',len(fixture.rows())==3 and fixture.calls['condition']==3)

        # A finite real background loop: 1 wall second = 120 test seconds.
        # Fixture schedules remain daily at 09:00; this is not an hourly business task.
        fixture.tasks[0].update(enabled=True,lifecycle_status='active')
        base=datetime.fromisoformat('2026-10-24T08:59:00+08:00')
        start=time.monotonic()
        loop=SchedulerLoop(fixture.service,fixture.scope,lambda task:fixture.identity,interval_seconds=0.1,
            clock=lambda:base+timedelta(seconds=(time.monotonic()-start)*120))
        check('真实计时器可启动且不会重复启动',loop.start(enabled=True) and not loop.start(enabled=True))
        threading.Event().wait(1.2)
        check('真实计时器可停止',loop.stop())
        fixture.settle()
        check('跨过整点后只执行一次',len(fixture.rows())==5 and fixture.calls['condition']==5 and fixture.calls['report']==2)
        ticks=loop.status()['ticks']
        threading.Event().wait(0.15)
        check('停止后不继续检查',loop.status()['ticks']==ticks)
        check('整个流程没有发信',fixture.calls['mail']==0)
        return {'mode':'offline_fixture','business_calls':0,'model_calls':0,'emails_sent':0,
                'clock_note':'真实后台计时，使用加速的测试时钟；不是当前墙上整点的生产验收',
                'checks':checks,'adapter_calls':dict(fixture.calls),'final_timer':loop.status(),
                'ledger':[{k:r[k] for k in ('task_id','scheduled_slot','status','notification_status')} for r in fixture.rows()]}
    finally:
        loop.stop()
        fixture.service.pool.shutdown(wait=True)


def main():
    directory=Path.home()/'.local/share/business-ai-hub/scheduler-validation'/(
        datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    directory.mkdir(parents=True,mode=0o700)
    result=run_validation(directory)
    output=directory/'result.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':len(result['checks']),'result':str(output),'mode':result['mode'],
        'business_calls':0,'model_calls':0,'emails_sent':0},ensure_ascii=False))


if __name__=='__main__':main()

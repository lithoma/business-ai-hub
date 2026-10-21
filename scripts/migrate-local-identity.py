#!/usr/bin/env python3
"""Explicit local-only migration: back up, bind old single-user drafts, select Genie One.
Never contacts models, writes Databricks, starts Jobs or sends mail. Stop local server first.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'business-ai-hub'))
from hub_auth import verified_identity
from databricks.sdk import WorkspaceClient


def migrate(database, identity, apply=False):
    database=Path(database).expanduser().resolve()
    if not database.is_file(): raise ValueError('Local SQLite database not found')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        rows=list(db.execute('SELECT * FROM task_registry'))
        if not apply: return {'tasks':len(rows),'apply':False}
        backup=database.with_name(database.stem+'-before-genie-one-auth-'+stamp+'.sqlite3')
        with sqlite3.connect(backup) as copy: db.backup(copy)
        backup.chmod(0o600)
        if 'config_json' not in [r[1] for r in db.execute('PRAGMA table_info(task_runs)')]:
            db.execute('ALTER TABLE task_runs ADD COLUMN config_json TEXT')
        changed=0
        for row in rows:
            trigger,report=json.loads(row['trigger_config']),json.loads(row['report_config'])
            security=report.get('security',{})
            if security and security != identity.security:
                raise ValueError('Database contains a different creator; migration aborted')
            legacy=not security or report.get('agent') in ('genie_a1','genie_a2')
            if not legacy: continue
            report['security']=identity.security
            report['agent']='genie_one'
            if trigger.get('type') in ('agent','sql'):
                trigger['agent']='genie_one';trigger['context_agents']=['genie_one']
            review=report.get('draft_review') or {}
            if review.get('source_agent'): review['source_agent']='genie_one'
            # Old identity/provider receipts are not acceptance of the new integration.
            for kind in ('condition_sql','condition_agent'):
                if isinstance(report.get(kind),dict): report[kind].pop('test',None)
            db.execute('''UPDATE task_registry SET trigger_config=?,report_config=?,owner=?,enabled=false,
                lifecycle_status='draft',updated_at=? WHERE task_id=?''',
                (json.dumps(trigger,ensure_ascii=False),json.dumps(report,ensure_ascii=False),identity.label,
                 datetime.now(timezone.utc).isoformat(),row['task_id']))
            changed+=1
        # Preserve actual historical provider; add only the local ownership attestation.
        for row in db.execute('SELECT run_id,config_json FROM local_trials').fetchall() if db.execute("SELECT name FROM sqlite_master WHERE name='local_trials'").fetchone() else []:
            snapshot=json.loads(row['config_json']);snapshot.setdefault('report',{})['security']=identity.security
            raw=json.dumps(snapshot,ensure_ascii=False)
            db.execute('UPDATE local_trials SET config_json=? WHERE run_id=?',(raw,row['run_id']))
            db.execute('UPDATE task_runs SET config_json=? WHERE run_id=?',(raw,row['run_id']))
        for file in (database.parent/'manual-task-tests').glob('*/*/execution-snapshot.json'):
            snapshot=json.loads(file.read_text());snapshot.setdefault('report',{})['security']=identity.security
            # Attach history metadata without altering immutable report files or sending again.
            db.execute('UPDATE task_runs SET config_json=? WHERE run_id=? AND config_json IS NULL',
                (json.dumps(snapshot,ensure_ascii=False),file.parent.name))
    return {'migrated':changed,'backup':str(backup),'old_acceptance_invalidated':True,'cloud_writes':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--profile',default='business-ai-hub')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    identity=verified_identity(WorkspaceClient(profile=args.profile),'local_cli')
    print(json.dumps(migrate(args.database,identity,args.apply),ensure_ascii=False))

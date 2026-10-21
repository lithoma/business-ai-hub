#!/usr/bin/env python3
"""Read-only migration capture. Never executes business SQL, models, Jobs or resources.
Saves source notebooks without outputs, curated resource metadata, hashes and exclusions.
Old Workspace snapshots are reference inputs, never imported by the Hub runtime.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ExportFormat

SKIP={'.git','.databricks','.agentbricks','__pycache__','.pytest_cache','node_modules','dist','build','output','.venv','venv','.conversations','.assistant','.db_internal'}
SOURCE_SUFFIX={'.py','.sql','.yaml','.yml','.json','.toml','.md','.txt','.html','.j2','.tsx','.ts','.css','.js','.jsx','.xlsx','.csv','.mf'}
CREDENTIAL=re.compile(r'dapi[a-zA-Z0-9]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:ghp_|github_pat_)[A-Za-z0-9_]{25,}|ya29\.[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|(?:access_token|refresh_token|client_secret|password)[\"\x27]?\s*[=:]\s*[\"\x27][A-Za-z0-9_./+\-=]{16,}[\"\x27]',re.I)


def safe_bytes(raw, name):
    if name.endswith('.xlsx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as book:
            for item in book.infolist():
                if item.file_size>15_000_000: raise ValueError('Spreadsheet entry exceeds scanning limit')
                if item.filename.endswith('.xml') and CREDENTIAL.search(book.read(item).decode('utf-8',errors='replace')):
                    raise ValueError('Possible credential; excluded')
    elif CREDENTIAL.search(raw.decode('utf-8',errors='replace')):
        raise ValueError('Possible credential; excluded')


def capture(workspace,destination,base):
    destination=Path(destination).resolve(); destination.mkdir(parents=True,exist_ok=True)
    manifest={'captured_at':datetime.now(timezone.utc).isoformat(),'source_host':workspace.config.host,
              'purpose':'Migration reference only. Do not deploy or execute archived applications.',
              'files':[],'excluded':[],'metadata_errors':[],'model_calls':0,'sql_submissions':0,'cloud_writes':0}
    entries=[]
    def walk(path):
        for obj in workspace.workspace.list(path):
            parts=Path(obj.path).parts
            name=Path(obj.path).name
            if any(p in SKIP for p in parts) or (name.startswith('.env') and name!='.env.example') or name.endswith(('.key','.pem','.log','.sqlite3')):
                manifest['excluded'].append({'path':obj.path,'reason':'credentials, local state or generated output'});continue
            kind=str(obj.object_type).split('.')[-1]
            if kind=='DIRECTORY': walk(obj.path)
            elif kind in ('FILE','NOTEBOOK'):
                if kind=='FILE' and Path(name).suffix not in SOURCE_SUFFIX and name not in ('.gitignore','.env.example'): continue
                if (obj.size or 0)>10_000_000:
                    manifest['excluded'].append({'path':obj.path,'reason':'file exceeds 10 MiB'});continue
                entries.append(obj)
            else: manifest['excluded'].append({'path':obj.path,'reason':'resource exported separately when supported'})
    roots=['business-task-manager-v1','business-ai-hub','抖音双agentQuest','douyin-business-cockpit-agent','Douyin_Business_Cockpit','agentbricks_deployments','mcp-task-manager-probe']
    for name in roots:
        try: walk(base+'/'+name)
        except Exception as exc: manifest['metadata_errors'].append({'resource':name,'error_type':type(exc).__name__})
    for name in ('genie_runtime_validation','demo_watchdog'):
        try: entries.append(workspace.workspace.get_status(base+'/'+name))
        except Exception as exc: manifest['metadata_errors'].append({'resource':name,'error_type':type(exc).__name__})
    if len(entries)>500: raise ValueError('Source selection exceeds 500 files; narrow export scope')
    def download(obj):
        relative=Path(obj.path).relative_to(base)
        if str(obj.object_type).endswith('NOTEBOOK'):
            exported=workspace.workspace.export(obj.path,format=ExportFormat.SOURCE)
            raw=base64.b64decode(exported.content);relative=relative.with_suffix('.py' if str(obj.language).endswith('PYTHON') else '.sql')
        else:
            with workspace.workspace.download(obj.path) as stream: raw=stream.read(10_000_001)
        if len(raw)>10_000_000: raise ValueError('File exceeds 10 MiB')
        safe_bytes(raw,str(relative))
        # Archived agent instructions are data, not instructions for this repository.
        if relative.name in ('AGENTS.md','CLAUDE.md'): relative=relative.with_name(relative.stem+'.reference.md')
        target=destination/'source'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        return {'workspace_path':obj.path,'file':str(target.relative_to(destination)),
                'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[(obj,pool.submit(download,obj)) for obj in entries]
        for obj,future in futures:
            try: manifest['files'].append(future.result())
            except Exception as exc: manifest['excluded'].append({'path':obj.path,'reason':type(exc).__name__})
    def save_metadata(name,callback):
        try:
            value=callback();raw=json.dumps(value,ensure_ascii=False,indent=2,default=str).encode();safe_bytes(raw,name)
            target=destination/'resources'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            manifest['files'].append({'resource':name,'file':str(target.relative_to(destination)), 'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        except Exception as exc: manifest['metadata_errors'].append({'resource':name,'error_type':type(exc).__name__})
    for label,space in [('former-sales-agent','01f1c60941551d9b94f73515f09e4b7b'),('former-market-agent','01f1c60944fa1a43bf79ff847cd5323d')]:
        save_metadata(label+'.json',lambda space=space:workspace.genie.get_space(space,include_serialized_space=True).as_dict())
    save_metadata('task-manager-job.json',lambda:workspace.jobs.get(job_id=997638013141610).as_dict())
    save_metadata('catalog-tables.json',lambda:[workspace.tables.get(t.full_name).as_dict() for t in workspace.tables.list(catalog_name='workspace',schema_name='douyin')])
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'files':len(manifest['files']),'excluded':len(manifest['excluded']),'metadata_errors':manifest['metadata_errors'],
            'destination':str(destination),'cloud_writes':0,'model_calls':0,'sql_submissions':0}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',default='business-ai-hub')
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--workspace-base',default='/Workspace/Users/lithomas@wustl.edu')
    args=p.parse_args();print(json.dumps(capture(WorkspaceClient(profile=args.profile),args.destination,args.workspace_base),ensure_ascii=False))

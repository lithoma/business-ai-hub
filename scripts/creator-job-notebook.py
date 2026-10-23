# Databricks notebook source
# Import as a Workspace Python notebook. No credentials in widgets or output.
# This notebook is for a native Jobs Notebook task with Run as = task creator.
import json
from pathlib import Path
import sys

for name in ('repo_root','config_path','job_id','job_run_id'):
    dbutils.widgets.text(name,'')

repo_root=Path(dbutils.widgets.get('repo_root')).resolve()
if not str(repo_root).startswith('/Workspace/') or not (repo_root/'business-ai-hub/creator_job_client.py').is_file():
    raise RuntimeError('Approved Workspace source directory is missing')
sys.path.insert(0,str(repo_root/'business-ai-hub'))
from creator_job_client import JobConfig,run_job
from databricks.sdk import WorkspaceClient

config=JobConfig.load(dbutils.widgets.get('config_path'))
if Path(config.repo_root).resolve()!=repo_root:
    raise RuntimeError('Notebook source and approved configuration differ')
job_id=int(dbutils.widgets.get('job_id'))
job_run_id=int(dbutils.widgets.get('job_run_id'))
context=dbutils.notebook.entry_point.getDbutils().notebook().getContext()
if int(context.tags().apply('jobId'))!=job_id:
    raise RuntimeError('This entry requires the matching native Job context')

# Explicit runtime auth; environment M2M or a developer profile is never selected.
workspace=WorkspaceClient(host=config.workspace_host,auth_type='runtime',debug_headers=False,debug_truncate_bytes=0)
result=run_job(config,workspace,lambda:context.apiToken().get(),job_id,job_run_id)
# Compact operational status only. Reports and email bodies stay in the App.
print(json.dumps(result,ensure_ascii=False))
if result['state']!='completed':raise RuntimeError('One or more tasks need attention; inspect App run history')

#!/usr/bin/env python3
"""Databricks Apps entry. Offline checks require neither SDK auth nor cloud access."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'business-ai-hub'))
from environment_config import read_profile, missing_settings, apply_selected_profile


def inspect_config(path):
    profile=read_profile(path)
    if profile['environment']!='company' or profile['env'].get('HUB_AUTH_MODE') not in ('databricks_app','databricks_app_obo'):
        raise ValueError('run-app.py requires an explicit company Apps profile')
    missing=missing_settings(profile)
    files=['business-ai-hub/app.py','business-ai-hub/frontend/dist/index.html',
        'business-task-manager-v1/business-task-manager-v1/schedule_core.py',
        'business-task-manager-v1/business-task-manager-v1/task_engine.py',
        'business-task-manager-v1/business-task-manager-v1/templates/email/default.html.j2']
    missing_files=[file for file in files if not (ROOT/file).is_file()]
    return {'configuration_ready':not missing and not missing_files,'missing':missing,
        'missing_files':missing_files,'identity':'per_user_obo' if profile['env']['HUB_AUTH_MODE']=='databricks_app_obo' else 'shared_app_service_principal',
        'unattended_creator_credentials':'native_jobs_pending_live_acceptance' if profile['env'].get('HUB_CREATOR_JOBS_ENABLED') is True else 'native_jobs_not_enabled',
        'platform_sso_required':True,'cloud_permissions':'not_checked_offline',
        'cloud_calls':0,'scheduler_enabled':False,'automatic_email':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path(os.getenv('HUB_CONFIG_FILE',ROOT/'config/company-apps.yaml')))
    parser.add_argument('--check-config',action='store_true')
    args=parser.parse_args()
    try:result=inspect_config(args.config)
    except ValueError as exc:parser.error(str(exc))
    if args.check_config:
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['configuration_ready'] else 2
    if not result['configuration_ready']:parser.error('Apps 配置未完成：'+', '.join(result['missing']+result['missing_files']))
    os.environ['HUB_CONFIG_FILE']=str(args.config.resolve())
    apply_selected_profile()
    from apps_auth import runtime_settings
    runtime_settings()
    try:port=int(os.environ['DATABRICKS_APP_PORT'])
    except (KeyError,ValueError):parser.error('Missing/invalid platform DATABRICKS_APP_PORT')
    if not 1<=port<=65535:parser.error('Invalid platform app port')
    os.environ['HUB_TASK_MANAGER_ROOT']=str(ROOT/'business-task-manager-v1/business-task-manager-v1')
    import app
    import uvicorn
    # One process owns run claims. No scheduler is started by this entry point.
    uvicorn.run(app.app,host='0.0.0.0',port=port,workers=1,access_log=False,proxy_headers=False)
    return 0


if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Choose one public environment profile. Checking never connects to Databricks."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'business-ai-hub'))
from environment_config import read_profile,missing_settings,apply_selected_profile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--check-config',action='store_true',help='Inspect configuration only; no SDK/model/SQL calls')
    parser.add_argument('--port',type=int)
    args=parser.parse_args()
    profile=read_profile(args.config);missing=missing_settings(profile)
    if args.check_config:
        print(json.dumps({'environment':profile['environment'],'configuration_ready':not missing,
            'missing':missing,'model_configured':bool(profile['env'].get('TASK_PARSER_ENDPOINT')),
            'cloud_calls':0,'schedules_enabled':False},ensure_ascii=False,indent=2))
        return 0 if not missing else 2
    if missing:parser.error('公司配置尚未完成：'+', '.join(missing)+'；未连接 Databricks。')
    os.environ['HUB_CONFIG_FILE']=str(args.config.expanduser().resolve())
    apply_selected_profile()
    if profile['environment']=='local':
        options=profile['local']
        spec=importlib.util.spec_from_file_location('hub_local_launcher',ROOT/'scripts/dev-local.py')
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        application=launcher.build_app(Path(options['database']).expanduser(),endpoint=profile['env'].get('TASK_PARSER_ENDPOINT',''),
            profile=options['databricks_profile'],model_limit=None if options.get('interactive_model') else 0,
            interactive_analysis=options.get('interactive_analysis',False),conversation_workflow=options.get('conversation_workflow',False),
            analysis_provider='genie_one',genie_one_chat=options.get('genie_one_chat',False),
            manual_trials=options.get('manual_trials',False),trial_recipient=options.get('trial_recipient',''))
    else:
        import app
        application=app.app
    import uvicorn
    uvicorn.run(application,host='127.0.0.1',port=args.port or profile.get('local',{}).get('port',8765),access_log=False)
    return 0


if __name__=='__main__':raise SystemExit(main())

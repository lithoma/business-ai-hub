#!/usr/bin/env python3
"""Offline Job JSON generator. Never authenticates, creates a Job or unpauses it."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'business-ai-hub'))
from creator_job_client import JobConfig,job_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config/creator-job.example.json')
    parser.add_argument('--output',type=Path,help='New local output file; never overwrites an existing file')
    args=parser.parse_args()
    try:
        config=JobConfig.load(args.config)
        result={'job_settings':job_settings(config),'binding_to_complete_after_creation':{
            'workspace':config.workspace_host.rstrip('/'),'creator_subject':config.creator_subject,
            'job_id':'REPLACE_WITH_ACTUAL_JOB_ID','enabled':False},
            'cloud_calls':0,'schedule_paused':True}
    except (OSError,ValueError,TypeError) as exc:
        print(json.dumps({'configuration_ready':False,'reason':str(exc),'cloud_calls':0},ensure_ascii=False,indent=2))
        return 2
    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        with args.output.open('x',encoding='utf-8') as stream:stream.write(text)
    else:print(text,end='')
    return 0


if __name__=='__main__':raise SystemExit(main())

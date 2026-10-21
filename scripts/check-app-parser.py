#!/usr/bin/env python3
"""Single App-SP parser acceptance check. Never impersonates the CLI user or provisions resources.

Default: validate App OAuth identity only. --infer sends exactly ONE real request.
Only use --infer when explicitly authorized for the App identity.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'business-ai-hub'))
APP_ID = 'fbc62528-8dda-4dad-8835-c2c22016b422'
APP_SCIM_ID = '77487787926046'
SERVICE = 'system.ai.gpt-oss-20b'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', help='Existing App-SP OAuth M2M profile; never a personal CLI profile')
    parser.add_argument('--infer', action='store_true')
    args = parser.parse_args()
    os.environ['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + os.environ.get('PATH', '')
    from databricks.sdk import WorkspaceClient
    workspace = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    if workspace.config.client_id != APP_ID or workspace.config.auth_type != 'oauth-m2m':
        raise SystemExit('No inference: requires OAuth M2M for the existing Business AI Hub App principal; personal CLI credentials are not accepted.')
    # Read-only identity lookup; no personal READ METADATA privilege is required.
    identity = workspace.current_user.me()
    if str(identity.id) != APP_SCIM_ID:
        raise SystemExit('No inference: authenticated principal does not match the expected App Service Principal.')
    if not args.infer:
        print(json.dumps({'app_identity_verified': True, 'inference_requests': 0}))
        return
    from task_llm import LLMAccessError, generate_configuration
    from task_drafts import extract_proposal, review_proposal
    os.environ['TASK_PARSER_ENDPOINT'] = SERVICE
    requirements = '每周五生成竞品周报，使用竞品与市场 Agent；不设置业务触发条件。'
    try:
        response = generate_configuration(lambda: workspace, requirements)
        proposal = extract_proposal(response)
        review_proposal(proposal, requirements)
    except LLMAccessError as exc:
        print(json.dumps({'app_identity_verified': True, 'inference_requests': 1, 'http_status': exc.status,
            'error_code': exc.error_code, 'reported_missing_privileges': exc.missing_privileges,
            'message': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    intent = {'weekly': proposal.schedule.frequency == 'weekly', 'friday': proposal.schedule.weekday == 'friday',
              'competitor_agent': proposal.report.agent == 'genie_one', 'unconditional': proposal.trigger.type == 'always'}
    print(json.dumps({'app_identity_verified': True, 'inference_requests': 1, 'service': SERVICE,
        'structured_output_valid': True, 'intent_checks': intent, 'frequency': proposal.schedule.frequency,
        'weekday': proposal.schedule.weekday, 'agent': proposal.report.agent,
        'usage': response.get('usage'), 'draft_saved': False}, ensure_ascii=False))
    if not all(intent.values()):
        raise SystemExit('One-call check returned a structurally valid proposal with incorrect task intent. No retry.')


if __name__ == '__main__': main()

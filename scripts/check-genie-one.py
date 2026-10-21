#!/usr/bin/env python3
"""Read-only MCP discovery: no genie_ask, SQL, resource changes or model calls."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'business-ai-hub'))
from genie_one_mcp import Caller, GenieOneMCP


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',default='business-ai-hub')
    args=parser.parse_args()
    from databricks.sdk import WorkspaceClient
    try:
        workspace=WorkspaceClient(profile=args.profile)
        # This key labels one local CLI context, not production OBO verification.
        adapter=GenieOneMCP(workspace,Caller('local-cli:'+args.profile,'local_user'))
        tools=adapter.discover()
        print(json.dumps({'server_url':adapter.server_url,'identity_mode':'local_cli_only',
                          'tools':list(tools.values()),'inference_calls':0},ensure_ascii=False,indent=2))
    except Exception as exc:
        chain=exc
        evidence=[]
        while chain is not None:
            code=getattr(chain,'error_code',None)
            evidence.append({'type':type(chain).__name__,'code':str(code)[:100] if code else None})
            chain=chain.__cause__
        print(json.dumps({'discovery_succeeded':False,'evidence':evidence,'inference_calls':0,
                          'next_step':'检查当前 CLI 登录、Workspace 中该 MCP 是否可见，以及 EXECUTE/USE CATALOG/USE SCHEMA；不推断未明确报告的缺失权限。'},ensure_ascii=False,indent=2))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())

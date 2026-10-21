#!/usr/bin/env python3
"""Preview, or explicitly send ONE fixed project-adapter test email. No task execution."""
import argparse
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'business-task-manager-v1'/'business-task-manager-v1'))
sys.path.insert(0,str(ROOT/'business-ai-hub'))
from telemetry import record_action, span

SUBJECT = 'Business AI Hub 发信测试'
BODY = '这是一封手动发信测试邮件，不包含业务数据，也没有启动自动任务。'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sender',required=True)
    parser.add_argument('--recipient',required=True)
    parser.add_argument('--credentials',type=Path,default=Path.home()/'.config/business-ai-hub/gmail-credentials.json')
    parser.add_argument('--output',type=Path,default=Path.home()/'.local/share/business-ai-hub/email-test')
    parser.add_argument('--send',action='store_true',help='Explicit one-message action, never retry automatically')
    args=parser.parse_args()
    for value in (args.sender,args.recipient):
        address=Address(addr_spec=value)
        if not address.username or not address.domain or any(c in value for c in '\r\n,;<>'):
            parser.error('A single plain email address is required')
    message=EmailMessage()
    message['From']=args.sender; message['To']=args.recipient; message['Subject']=SUBJECT
    message.set_content(BODY,charset='utf-8')
    output=args.output.expanduser().resolve()
    if output.is_relative_to(ROOT): parser.error('Keep local email previews and receipts outside Git')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    fingerprint=hashlib.sha256(json.dumps([args.sender,args.recipient,SUBJECT,BODY],ensure_ascii=False).encode()).hexdigest()
    preview=output/(fingerprint+'.eml')
    preview.write_bytes(message.as_bytes())
    if not args.send:
        print(json.dumps({'status':'preview_only','preview':str(preview),'sender':args.sender,'recipient':args.recipient,
                          'subject':SUBJECT,'body':BODY,'emails_sent':0},ensure_ascii=False))
        return
    credentials_path=args.credentials.expanduser().resolve()
    if credentials_path.is_relative_to(ROOT) or not credentials_path.is_file():
        raise SystemExit('Project Gmail authorization is missing; no email sent. Complete authorize-gmail.py first.')
    values=json.loads(credentials_path.read_text())
    if values.get('authorized_email','').casefold()!=args.sender.casefold():
        raise SystemExit('Project-authorized sender does not match; no email sent. Verify account authorization first.')
    from google.oauth2.credentials import Credentials
    from report_email import GmailProvider, EmailRejected, EmailOutcomeUnknown
    credentials=Credentials(token=None,client_id=values['client_id'],client_secret=values['client_secret'],
                            refresh_token=values['refresh_token'],token_uri='https://oauth2.googleapis.com/token',
                            scopes=['https://www.googleapis.com/auth/gmail.send'])
    receipt=output/(fingerprint+'.json')
    # Exclusive claim precedes the send attempt. An uncertain outcome is not retried.
    try: fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        print(json.dumps({'status':'existing_attempt','receipt':str(receipt),'new_emails_sent':0}))
        return
    record={'sender':args.sender,'recipient':args.recipient,'subject':SUBJECT,'status':'sending',
            'attempted_at':datetime.now(timezone.utc).isoformat()}
    with os.fdopen(fd,'w') as stream: json.dump(record,stream,ensure_ascii=False)
    try:
        with span('gmail_send_test',kind='mail_adapter',provider='gmail') as event:
            message_id=GmailProvider(credentials).send(message.as_bytes())
            record.update(status='sent',provider_message_id=message_id)
            event['api_accepted']=True
    except EmailOutcomeUnknown:
        record.update(status='outcome_unknown',next_step='Inspect Sent mail before any manual retry')
    except EmailRejected as exc:
        record.update(status='rejected',reason=str(exc))
    except Exception:
        record.update(status='outcome_unknown',next_step='Inspect authorization and Sent mail; do not automatically retry')
    receipt.write_text(json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps({**record,'receipt':str(receipt)},ensure_ascii=False))
    if record['status']!='sent': raise SystemExit(1)


if __name__=='__main__':
    os.environ.setdefault('HUB_TEST_METRICS_DB',str(Path.home()/'.local/share/business-ai-hub/dev-metrics.sqlite3'))
    with record_action('manual_email_test'): main()

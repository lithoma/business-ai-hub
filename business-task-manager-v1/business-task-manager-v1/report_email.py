"""Provider-independent report email, approved SQL CSV exports and Gmail OAuth adapter.

No connector credentials are reused. Sending requires explicit deployment settings.
"""
from __future__ import annotations
import base64
import csv
import hashlib
import io
import json
import os
import re
from datetime import date, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Protocol
import requests
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from markdown_it import MarkdownIt
from markupsafe import Markup
from registry_tasks import validate_read_sql

MAX_MIME_BYTES = 20 * 1024 * 1024  # Conservative application cap including MIME encoding.
ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


class EmailConfigurationError(ValueError): pass
class ExportLimitError(ValueError): pass
class EmailRejected(RuntimeError): pass
class EmailOutcomeUnknown(RuntimeError): pass


def email_address(value):
    if not isinstance(value, str) or len(value) > 254 or re.search(r'[\s\r\n<>,;]', value):
        raise EmailConfigurationError('Use plain email addresses without display names')
    try:
        address = Address(addr_spec=value)
        if not address.username or not address.domain or '.' not in address.domain:
            raise ValueError()
    except ValueError as exc:
        raise EmailConfigurationError('Invalid email address') from exc
    return value


def safe_file(root, value):
    root = root.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise EmailConfigurationError('Template/query must be an installed project file')
    return path


def csv_cell(value):
    if value is None: return ''
    if isinstance(value, (date, datetime)): return value.isoformat()
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value  # Spreadsheet formula escaping applies to text, not negative numbers.
    return value


def export_csv(spark, root, export, variables, *, max_bytes=12 * 1024 * 1024):
    query = safe_file(root, export['sql_file']).read_text(encoding='utf-8')
    validate_read_sql(query)
    # Curated SQL supports the same explicit date bindings as task execution.
    names = set(re.findall(r'(?<!:):([A-Za-z_][A-Za-z_0-9]*)', query))
    if names - variables.keys(): raise EmailConfigurationError('Unknown export SQL parameter')
    max_rows = export.get('max_rows', 100000)
    if type(max_rows) is not int or not 1 <= max_rows <= 1000000:
        raise EmailConfigurationError('Invalid export row limit')
    frame = spark.sql(query, args={key: variables[key] for key in names}).limit(max_rows + 1)
    columns = frame.columns
    if not columns or len(columns) != len(set(columns)) or len(columns) > 16384:
        raise EmailConfigurationError('Export needs unique columns within Excel column limits')
    filename = export.get('filename', 'data.csv')
    if not re.fullmatch(r'[^/\\\x00-\x1f]{1,120}\.csv', filename, re.I):
        raise EmailConfigurationError('CSV filename must be a simple .csv name')
    buf = io.BytesIO()
    text = io.TextIOWrapper(buf, encoding='utf-8-sig', newline='', write_through=True)
    writer = csv.writer(text, lineterminator='\r\n')
    writer.writerow([csv_cell(name) for name in columns])
    count = 0
    for row in frame.toLocalIterator():
        count += 1
        if count > max_rows: raise ExportLimitError('CSV row limit exceeded; no partial attachment sent')
        writer.writerow([csv_cell(value) for value in tuple(row)])
        if buf.tell() > max_bytes: raise ExportLimitError('CSV byte limit exceeded')
    text.flush()
    content = buf.getvalue()
    text.detach()
    if len(content) > max_bytes: raise ExportLimitError('CSV byte limit exceeded')
    metadata = {'filename': filename, 'rows': count, 'columns': len(columns),
                'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    return content, metadata


def build_message(spark, root, email_config, run):
    task = json.loads(run['config_json'])
    delivery = task.get('report', {}).get('delivery', {})
    sender = email_address(email_config.get('from_address', ''))
    to = [email_address(item) for item in delivery.get('to', [])]
    cc = [email_address(item) for item in delivery.get('cc', [])]
    allowed = {email_address(item).lower() for item in email_config.get('allowed_recipients', [])}
    if not to or len(to + cc) > 50 or any(item.lower() not in allowed for item in to + cc):
        raise EmailConfigurationError('Recipients must be explicitly configured and allowed')
    template_id = delivery.get('template_id', 'default')
    template = email_config.get('templates', {}).get(template_id)
    if not ID.fullmatch(template_id) or not template:
        raise EmailConfigurationError('Email template is not installed')
    variables = task.get('_execution_context')
    if not variables:
        raise EmailConfigurationError('Saved report lacks execution date context; do not re-export with today\'s dates')
    attachments, artifacts = [], []
    ids = delivery.get('csv_exports', [])
    if len(ids) > 5 or len(ids) != len(set(ids)):
        raise EmailConfigurationError('At most five distinct CSV exports')
    if ids and task.get('report', {}).get('security'):
        # Never read business exports under the metadata/Job Spark identity.
        raise EmailConfigurationError('Creator-scoped CSV export adapter is required before exporting user-owned task data')
    for key in ids:
        export = email_config.get('exports', {}).get(key)
        if not ID.fullmatch(key) or not export:
            raise EmailConfigurationError('CSV dataset is not installed')
        content, metadata = export_csv(spark, root, export, variables,
                                       max_bytes=int(email_config.get('max_attachment_bytes', 12 * 1024 * 1024)))
        attachments.append((content, metadata['filename']))
        artifacts.append(dict(metadata, export_id=key))
    markdown = MarkdownIt('commonmark', {'html': False}).enable('table').disable('image')
    context = dict(variables, task_name=run['task_name'], run_id=run['run_id'],
                   trigger_reason=run.get('trigger_reason', ''), report_text=run['report_text'],
                   report_html=Markup(markdown.render(run['report_text'])), attachments=artifacts)
    env = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
    body = env.from_string(safe_file(root, template['file']).read_text(encoding='utf-8')).render(**context)
    subject_template = delivery.get('subject_template') or '{{ task_name }} | {{ dispatch_date }}'
    subject = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined).from_string(subject_template).render(**context)
    if not subject.strip() or len(subject) > 200 or '\r' in subject or '\n' in subject:
        raise EmailConfigurationError('Invalid email subject')
    message = EmailMessage(policy=SMTP)
    message['From'] = sender; message['To'] = ', '.join(to)
    if cc: message['Cc'] = ', '.join(cc)
    message['Subject'] = subject
    # A stable Message-ID helps operator reconciliation; Gmail does NOT promise deduplication.
    message['Message-ID'] = '<bah-' + run['run_id'] + '@' + sender.split('@')[1] + '>'
    message.set_content(run['report_text'], charset='utf-8')
    message.add_alternative(body, subtype='html', charset='utf-8')
    for content, filename in attachments:
        message.add_attachment(content, maintype='text', subtype='csv', filename=filename)
    raw = message.as_bytes()
    cap = min(int(email_config.get('max_message_bytes', MAX_MIME_BYTES)), MAX_MIME_BYTES)
    if len(raw) > cap: raise ExportLimitError('Complete encoded MIME exceeds application message size limit')
    return raw, artifacts


class MailProvider(Protocol):
    def send(self, raw_message: bytes) -> str: ...


class GmailProvider:
    def __init__(self, credentials): self.credentials = credentials

    @classmethod
    def from_environment(cls):
        from google.oauth2.credentials import Credentials
        values = [os.environ.get(key, '') for key in ('GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET', 'GMAIL_REFRESH_TOKEN')]
        if not all(values): raise EmailConfigurationError('Gmail OAuth not configured; connector authorization is separate')
        return cls(Credentials(token=None, client_id=values[0], client_secret=values[1], refresh_token=values[2],
                               token_uri='https://oauth2.googleapis.com/token',
                               scopes=['https://www.googleapis.com/auth/gmail.send']))

    def send(self, raw_message):
        from google.auth.transport.requests import Request
        # Fail before Gmail invocation if refresh/auth is unavailable.
        try:
            if not self.credentials.valid: self.credentials.refresh(Request())
        except Exception as exc:
            raise EmailRejected('Gmail OAuth refresh failed; no send attempted') from exc
        try:
            response = requests.post('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                headers={'Authorization': 'Bearer ' + self.credentials.token},
                json={'raw': base64.urlsafe_b64encode(raw_message).decode('ascii')},
                timeout=(5, 60), allow_redirects=False)
        except requests.RequestException as exc:
            raise EmailOutcomeUnknown('Gmail send outcome unknown; inspect Sent mail before retrying') from exc
        if response.status_code >= 500:
            raise EmailOutcomeUnknown('Gmail server error; send outcome uncertain')
        if not 200 <= response.status_code < 300:
            raise EmailRejected('Gmail rejected send request (HTTP %s)' % response.status_code)
        try:
            message_id = response.json().get('id')
            if not isinstance(message_id, str) or not message_id: raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise EmailOutcomeUnknown('Gmail accepted request but returned no message ID') from exc
        return message_id


def deliver_emails(spark, root, config, ledger, day, provider=None):
    """Per-report mail worker; no replay of SENDING/ACCEPTED/UNKNOWN/FAILED states."""
    if config.get('enabled') is not True:
        return {'accepted': 0, 'disabled': True}
    provider = provider or GmailProvider.from_environment()
    count, failed, unknown = 0, 0, 0
    for run in ledger.pending_email_reports(day, int(config.get('max_messages_per_dispatch', 20))):
        if not ledger.claim_email(run['run_id']): continue
        artifacts = []
        try:
            raw, artifacts = build_message(spark, root, config, run)
            message_id = provider.send(raw)
        except EmailOutcomeUnknown:
            unknown += 1
            ledger.email_result(run['run_id'], 'EMAIL_UNKNOWN', error='ProviderOutcomeUnknown', artifacts=artifacts)
            continue
        except Exception as exc:
            failed += 1
            ledger.email_result(run['run_id'], 'EMAIL_FAILED', error=type(exc).__name__, artifacts=artifacts)
            continue
        ledger.email_result(run['run_id'], 'EMAIL_ACCEPTED', message_id=message_id, artifacts=artifacts)
        count += 1
    return {'accepted': count, 'failed': failed, 'unknown': unknown, 'disabled': False}

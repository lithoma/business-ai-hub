#!/usr/bin/env python3
"""Build a source-only company transfer ZIP; never authenticate or call cloud APIs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = 'business-ai-hub-transfer'
SKIP_PARTS = {'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache',
              '.ruff_cache', '.venv', 'venv', '.git', 'coverage', 'build'}
SKIP_FILES = {'.DS_Store', 'APP_PY_APPEND.txt', 'GENIE_CODE_SETUP_PROMPT.txt',
              'alert_outbox.sql', 'OUTBOX_EMPTY_TABLE.sql', 'README_部署说明.md'}
SCRIPTS = ['run-hub.py', 'run-app.py', 'dev-local.py', 'preview-local.py', 'authorize-gmail.py',
           'check-genie-one.py', 'check-app-parser.py', 'test-local-tasks.py',
           'test-email.py', 'test-scheduler.py', 'migrate-local-identity.py', 'package-company.py',
           'prepare-creator-job.py','creator-job-notebook.py']
DOCS = ['CONDITION-SQL.md', 'CONVERSATION-WORKFLOW.md', 'EMAIL-DELIVERY.md',
        'LOCAL-TRIAL-UI.md', 'RUNS-AND-SCHEDULING.md', 'MILESTONE-2B.md', 'MILESTONE-2C.md',
        'OBO-AND-TASK-IDENTITY.md','CREATOR-JOBS.md','PLATFORM-TEST-PLAN.md','REVIEW-INDEX.md','GIT-SYNC.md']
PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'databricks_pat': re.compile(rb'\bdapi[a-fA-F0-9]{32,}\b'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'google_access_token': re.compile(rb'\bya29\.[A-Za-z0-9_-]{30,}'),
    'google_refresh_token': re.compile(rb'\b1//[A-Za-z0-9_-]{35,}'),
    'google_client_secret': re.compile(rb'\bGOCSPX-[A-Za-z0-9_-]{20,}'),
    'jwt': re.compile(rb'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b'),
    'aws_key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'credential_in_url': re.compile(rb'https?://[^\s/\x22\x27:@]+:[^\s/\x22\x27@]+@'),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def allowed(path):
    if set(path.relative_to(ROOT).parts) & SKIP_PARTS:
        return False
    if path.is_symlink():
        raise ValueError('Symlinks are not allowed in transfer input: ' + str(path.relative_to(ROOT)))
    name = path.name
    if name in SKIP_FILES or name.endswith(('.pyc', '.pyo', '.tsbuildinfo', '.log', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.pem', '.key')):
        return False
    if name.startswith('.env') and name != '.env.example':
        return False
    if name == '.databrickscfg' or name.startswith(('client_secret', 'gmail-credentials')):
        return False
    return True


def inputs():
    for folder in ['business-ai-hub', 'business-task-manager-v1/business-task-manager-v1', 'config', 'database', 'deployment', 'examples/authorization', 'docs/company-transfer']:
        for path in sorted((ROOT / folder).rglob('*')):
            if path.is_file() and allowed(path):
                yield path, path.relative_to(ROOT)
    for name in SCRIPTS:
        path = ROOT / 'scripts' / name
        yield path, path.relative_to(ROOT)
    for name in DOCS:
        path = ROOT / 'docs' / name
        yield path, path.relative_to(ROOT)
    evidence=ROOT/'docs/validation/creator-jobs-offline-2026-10-23.json'
    yield evidence,evidence.relative_to(ROOT)
    preparation = ROOT / 'docs/company-transfer'
    if not preparation.is_dir():
        preparation = ROOT / 'company-preparation'
    for path in sorted(preparation.rglob('*')):
        if path.is_file() and allowed(path):
            yield path, Path('company-preparation') / path.relative_to(preparation)
    yield ROOT / '.gitignore', Path('.gitignore')


def git_state():
    executable = shutil.which('git')
    fallback = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/git'
    if fallback.is_file():
        executable = str(fallback)
    if not executable:
        return {'revision': None, 'working_tree_changed': None}
    def run(*args):
        result = subprocess.run([executable, '-C', str(ROOT), *args], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    return {'revision': run('rev-parse', 'HEAD'), 'branch': run('branch', '--show-current'),
            'working_tree_changed': bool(run('status', '--porcelain')),
            'source_selection': 'Current on-disk files, including uncommitted profile implementation; not git archive HEAD.'}


def validate_members(archive):
    expected = json.loads(archive.read(ARCHIVE_ROOT + '/MANIFEST.json'))
    files = expected['files']
    names = archive.namelist()
    if len(set(names)) != len(names) or len(names) != len(files) + 1:
        raise ValueError('Unexpected archive members')
    for name, info in files.items():
        if sha(archive.read(ARCHIVE_ROOT + '/' + name)) != info['sha256']:
            raise ValueError('Archive hash mismatch: ' + name)
    if archive.testzip() is not None:
        raise ValueError('ZIP CRC verification failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.suffix.lower() != '.zip' or output.exists():
        parser.error('--output must be a new .zip path; existing artifacts are not overwritten')
    if not (ROOT / 'business-ai-hub/frontend/dist/index.html').is_file():
        parser.error('Build frontend/dist before packaging')
    output.parent.mkdir(parents=True, exist_ok=True)
    transformations = []
    with tempfile.TemporaryDirectory(prefix='business-ai-hub-transfer-') as temp:
        staging = Path(temp) / ARCHIVE_ROOT
        staging.mkdir()
        seen = set()
        for source, relative in inputs():
            if relative in seen:
                raise ValueError('Duplicate input: ' + str(relative))
            seen.add(relative)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            # Only the transfer copy changes; active local files are not touched.
            if 'tasks' in relative.parts and relative.name == 'config.yaml':
                new_data, count = re.subn(rb'(?m)^enabled:[^\r\n]*', b'enabled: false  # Disabled in company transfer copy; review before activation.', data)
                if count != 1:
                    raise ValueError('Expected one task enabled field: ' + str(relative))
                transformations.append({'path': relative.as_posix(), 'source_sha256': sha(data),
                                        'reason': 'Disable YAML example in transfer copy only'})
                data = new_data
            target.write_bytes(data)
        shutil.copyfile(staging / 'company-preparation/START-HERE.md', staging / 'README-FIRST.md')
        # Deployment-copy aliases keep both launch paths at the extracted root.
        for name in ('app.yaml','requirements.txt'):
            shutil.copyfile(staging / 'deployment/databricks-apps' / name,staging / name)
        files, findings = {}, []
        for path in sorted(staging.rglob('*')):
            if not path.is_file():
                continue
            relative, data = path.relative_to(staging).as_posix(), path.read_bytes()
            for label, pattern in PATTERNS.items():
                if pattern.search(data):
                    findings.append({'path': relative, 'pattern': label})
            files[relative] = {'sha256': sha(data), 'bytes': len(data)}
        if findings:
            # Report paths only. Never print the matching secret value.
            raise ValueError('Potential credentials detected; ZIP not written: ' + json.dumps(findings))
        required = ['config/local.yaml', 'config/company-local.yaml', 'config/company.yaml', 'scripts/run-hub.py',
                    'app.yaml','requirements.txt','config/company-apps.yaml','scripts/run-app.py',
                    'business-ai-hub/environment_config.py', 'business-ai-hub/frontend/dist/index.html',
                    'business-ai-hub/app.py', 'business-task-manager-v1/business-task-manager-v1/task_engine.py',
                    'company-preparation/GENIE-CODE-PROMPT.txt']
        if set(required) - files.keys():
            raise ValueError('Required files missing from package')
        manifest = {'package': ARCHIVE_ROOT, 'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                    **git_state(), 'cloud_calls': 0, 'secret_pattern_findings': 0,
                    'transformations': transformations, 'files': files,
                    'dependency_wheels_included': False, 'company_ready': False}
        (staging / 'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(staging.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(staging.parent).as_posix())
        with zipfile.ZipFile(output) as archive:
            validate_members(archive)
    digest = sha(output.read_bytes())
    checksum = output.with_suffix('.zip.sha256')
    checksum.write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    print(json.dumps({'zip': str(output), 'bytes': output.stat().st_size, 'files': len(files) + 1,
                      'sha256_file': str(checksum), 'verification': 'CRC and every file hash passed',
                      'cloud_calls': 0, 'credential_pattern_findings': 0}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

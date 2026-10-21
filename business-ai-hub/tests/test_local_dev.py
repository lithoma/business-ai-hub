"""Actual HTTP + SQLite local CRUD. No model/cloud mocks or remote operations."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


class LocalDevTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{port}'
        cls.process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/dev-local.py'), '--database',
            str(Path(cls.tmp.name) / 'dev.sqlite3'), '--port', str(port)], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + '/api/health', timeout=1).close(); return
            except (urllib.error.URLError, TimeoutError): time.sleep(.05)
        cls.process.terminate(); cls.process.wait(timeout=5)
        raise RuntimeError('Local test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.wait(timeout=5); cls.tmp.cleanup()

    def request(self, path, method='GET', body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as exc: return exc.code, json.load(exc)

    def test_real_local_database_create_update_refresh_delete(self):
        payload = {'task_id': 'local-test', 'name': 'Friday competitor report',
                   'description': 'local SQLite only', 'schedule': {'frequency': 'weekly', 'weekday': 'friday'},
                   'trigger': {'type': 'always'}, 'report': {'agent': 'genie_a2', 'prompt': 'Competitor weekly report'}}
        status, body = self.request('/api/tasks/create', 'POST', payload)
        self.assertEqual(status, 200, body)
        _, body = self.request('/api/tasks')
        saved = [task for task in body['tasks'] if task.get('task_id') == 'local-test'][0]
        self.assertEqual(saved['schedule']['weekday'], 'friday'); self.assertFalse(saved['enabled'])
        self.assertEqual(saved['lifecycle_status'], 'draft')
        status, body = self.request('/api/tasks/local-test', 'PUT', {'name': 'Updated locally', 'expected_updated_at': saved['updated_at']})
        self.assertEqual(status, 200, body)
        status, _ = self.request('/api/tasks/local-test', 'PUT', {'name': 'stale', 'expected_updated_at': saved['updated_at']})
        self.assertEqual(status, 409)
        status, body = self.request('/api/tasks/local-test', 'DELETE', {'confirm': True})
        self.assertEqual(status, 200, body)
        _, body = self.request('/api/tasks')
        self.assertFalse(any(task.get('task_id') == 'local-test' for task in body['tasks']))

    def test_cloud_parser_genie_and_execution_remain_disabled(self):
        status, body = self.request('/api/tasks/generate', 'POST', {'requirements': 'Every Friday give me a competitor weekly report'})
        self.assertEqual(status, 503); self.assertIn('Model not configured', body['detail'])
        status, _ = self.request('/api/chat', 'POST', {'agent': 'genie_a1', 'question': 'No remote call'})
        self.assertEqual(status, 503)
        status, history = self.request('/api/reports')
        self.assertEqual(status, 200); self.assertEqual(history['runs'], [])


if __name__ == '__main__': unittest.main()

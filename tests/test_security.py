import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_backend import BackendTests, pdf_bytes
from app.security import SecurityMiddleware


class SecurityTests(unittest.TestCase):
    setUp = BackendTests.setUp
    tearDown = BackendTests.tearDown
    upload = BackendTests.upload
    def test_upload_auth_runs_before_form_parser(self):
        from starlette.requests import Request
        with patch.object(Request, '_get_form', side_effect=AssertionError('parser reached')):
            for headers, expected in [({}, 401), (self.employee, 403)]:
                response = self.client.post('/documents', headers=headers,
                    content=b'bad multipart',
                    # No actual form parsing should be attempted.
                )
                self.assertEqual(response.status_code, expected)

    def test_private_default_and_no_cache(self):
        response = self.client.post('/documents', headers=self.admin,
                                   files={'file': ('secret.txt', b'SECRET')})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['visibility'], 'admin')
        self.assertEqual(self.client.get('/documents', headers=self.employee).json()['documents'], [])
        for response in [response, self.client.get('/me'), self.client.get('/me', headers=self.admin)]:
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')

    def test_fail_closed_on_invalid_runtime_config_and_duplicate_keys(self):
        with patch.dict(os.environ, {'EMPLOYEE_API_KEY': os.environ['ADMIN_API_KEY']}):
            self.assertEqual(self.client.get('/me', headers=self.admin).status_code, 503)
        self.assertEqual(self.client.get('/me', headers=[('X-API-Key', self.admin['X-API-Key']),
                                                       ('X-API-Key', self.employee['X-API-Key'])]).status_code, 401)

    def test_json_body_limit_and_validation_redaction(self):
        r = self.client.post('/search', headers=self.employee,
                             content=b'x' * (65536 + 1))
        self.assertEqual(r.status_code, 413)
        marker = 'PRIVATE_INPUT_MARKER'
        r = self.client.post('/assistants', headers=self.admin, json={'name': marker * 100})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn(marker, r.text)
        r = self.client.post('/assistants', headers=self.admin, json={'name': 'x', 'role': 'admin'})
        self.assertEqual(r.status_code, 422)

    def test_pdf_timeout_and_worker_failure_leave_no_records(self):
        for outcome in [subprocess.TimeoutExpired('parser', 15),
                        subprocess.CompletedProcess([], 1, stdout=b'')]:
            with patch('app.documents.subprocess.run', side_effect=outcome if isinstance(outcome, Exception) else None,
                       return_value=outcome):
                self.assertEqual(self.upload('bad.pdf', b'%PDF-test').status_code, 422)
        self.assertEqual(self.client.get('/documents', headers=self.admin).json()['documents'], [])

    def test_pdf_worker_does_not_inherit_api_keys(self):
        original = subprocess.run
        with patch('app.documents.subprocess.run', wraps=original) as run:
            self.assertEqual(self.upload('good.pdf', pdf_bytes()).status_code, 201)
            self.assertNotIn('ADMIN_API_KEY', run.call_args.kwargs['env'])
            self.assertNotIn('DATABASE_PATH', run.call_args.kwargs['env'])

    def test_db_permissions(self):
        if os.name == 'posix':
            self.assertEqual(Path(os.environ['DATABASE_PATH']).stat().st_mode & 0o777, 0o600)

    def test_list_pagination_and_id_length(self):
        self.upload('a.txt', b'A')
        self.upload('b.txt', b'B')
        r = self.client.get('/documents?limit=1&offset=1', headers=self.admin)
        self.assertEqual(len(r.json()['documents']), 1)
        self.assertEqual(self.client.get('/documents?limit=101', headers=self.admin).status_code, 422)
        a = self.client.post('/assistants', headers=self.admin, json={'name': 'x'}).json()
        r = self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin,
                            json={'document_ids': ['x' * 101]})
        self.assertEqual(r.status_code, 422)

    def test_raw_asgi_stream_limits_auth_and_body_timeout(self):
        async def check(headers, chunks, expected, receive_allowed=True):
            called = False
            async def downstream(scope, receive, send):
                nonlocal called
                called = True
            middleware = SecurityMiddleware(downstream)
            events = []
            async def receive():
                self.assertTrue(receive_allowed, 'body read before authentication')
                return {'type': 'http.request', 'body': chunks.pop(0), 'more_body': bool(chunks)}
            async def send(message):
                events.append(message)
            scope = {'type': 'http', 'path': '/search', 'method': 'POST', 'headers': headers}
            await middleware(scope, receive, send)
            self.assertFalse(called)
            self.assertEqual(events[0]['status'], expected)
        auth = [(b'x-api-key', self.admin['X-API-Key'].encode())]
        asyncio.run(check([], [b''], 401, False))
        asyncio.run(check(auth, [b'a' * 32768, b'b' * 32769], 413))
        asyncio.run(check(auth + [(b'content-length', b'9' * 5000)], [b''], 400, False))
        asyncio.run(check(auth + [(b'content-length', b'1')], [b'xx'], 400))
        with patch('app.security.BODY_TIMEOUT', 0):
            asyncio.run(check(auth, [b''], 408))


del BackendTests

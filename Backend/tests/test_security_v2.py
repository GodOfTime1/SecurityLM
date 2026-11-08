import asyncio
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import httpx
from tests.test_backend import BackendTests
from app.db import connection
from app.model_client import (MAX_MODEL_BYTES, ModelRequest, ModelResponse, ModelUnavailable,
                              OllamaModel, get_model_client)


class SecurityV2Tests(unittest.TestCase):
    setUp, tearDown, upload = BackendTests.setUp, BackendTests.tearDown, BackendTests.upload

    def setup_chat(self):
        doc = self.upload('source.txt', b'VPN CONFIDENTIAL-TEXT').json()
        a = self.client.post('/assistants', headers=self.admin, json={'name': 'IT'}).json()
        self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin, json={'document_ids': [doc['id']]})
        self.client.put(f"/assistants/{a['id']}/tools", headers=self.admin, json={'tools': ['create_ticket']})
        c = self.client.post('/conversations', headers=self.employee, json={'assistant_id': a['id']}).json()
        return doc, a, c

    def send(self, c, text='VPN ticket', reply=None):
        def call():
            return self.client.post(f"/conversations/{c['id']}/messages", headers=self.employee, json={'content': text})
        if reply is None:
            return call()
        with patch('app.chat.get_model_client') as model:
            model.return_value.generate.return_value = reply
            return call()

    def draft(self):
        d, a, c = self.setup_chat()
        r = self.send(c)
        self.assertEqual(r.status_code, 201, r.text)
        return d, a, c, r.json()['assistant_message']['actions'][0]

    def approve(self, action, version=1):
        return self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={'version': version})

    def test_individual_identities_isolate_conversations_and_actions(self):
        entries = [{'key': self.admin['X-API-Key'], 'id': 'a1', 'role': 'admin'},
                   {'key': self.employee['X-API-Key'], 'id': 'e1', 'role': 'employee'},
                   {'key': 'person2-' + 'c' * 32, 'id': 'e2', 'role': 'employee'}]
        with patch.dict(os.environ, {'USER_API_KEYS': json.dumps(entries)}):
            _, _, c, action = self.draft()
            other = {'X-API-Key': entries[2]['key']}
            for method, path, body in [
                ('GET', f"/conversations/{c['id']}", None),
                ('DELETE', f"/conversations/{c['id']}", None),
                ('POST', f"/conversations/{c['id']}/messages", {'content': 'VPN'}),
                ('GET', f"/actions/{action['id']}", None),
                ('PATCH', f"/actions/{action['id']}", {'version': 1, 'arguments': action['arguments']}),
                ('POST', f"/actions/{action['id']}/approve", {'version': 1}),
                ('POST', f"/actions/{action['id']}/cancel", {})]:
                self.assertEqual(self.client.request(method, path, headers=other, json=body).status_code, 404)
            self.assertEqual(self.client.get('/conversations', headers=other).json()['conversations'], [])

    def test_individual_identity_config_fails_closed(self):
        for value in ['{}', '[]', 'broken', '[{"key":3,"id":"x","role":"employee"}]']:
            with patch.dict(os.environ, {'USER_API_KEYS': value}):
                self.assertEqual(self.client.get('/me', headers=self.admin).status_code, 503)
        with patch.dict(os.environ, {'USER_API_KEYS': json.dumps([
            {'key': 'new-' + 'n' * 32, 'id': 'one', 'role': 'employee'}])}):
            self.assertEqual(self.client.get('/me', headers=self.employee).status_code, 401)

    def test_uncited_source_revocation_redacts_answer_actions_and_history(self):
        d, _, c = self.setup_chat()
        reply = ModelResponse('CONFIDENTIAL-TEXT', [{'tool': 'create_ticket', 'arguments': {
            'title': 'CONFIDENTIAL-TEXT', 'description': 'private text'}}])
        action = self.send(c, reply=reply).json()['assistant_message']['actions'][0]
        with connection() as db:
            db.execute("UPDATE documents SET visibility='admin' WHERE id=?", (d['id'],))
        r = self.client.get(f"/conversations/{c['id']}", headers=self.employee)
        self.assertNotIn('CONFIDENTIAL-TEXT', r.text)
        self.assertTrue(r.json()['messages'][1]['redacted'])
        self.assertEqual(self.client.get(f"/actions/{action['id']}", headers=self.employee).status_code, 404)
        self.assertEqual(self.approve(action).status_code, 404)
        with patch('app.chat.get_model_client') as model:
            model.return_value.generate.return_value = ModelResponse('No source')
            self.assertEqual(self.send(c, 'hello').status_code, 201)
            self.assertNotIn('CONFIDENTIAL-TEXT', str(model.return_value.generate.call_args.args[0].history))

    def test_transitive_history_sources_and_collection_removal(self):
        _, a, c = self.setup_chat()
        self.assertEqual(self.send(c, reply=ModelResponse('CONFIDENTIAL-TEXT')).status_code, 201)
        self.assertEqual(self.send(c, 'summarize that', ModelResponse('DERIVED-SECRET')).status_code, 201)
        self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin, json={'document_ids': []})
        history = self.client.get(f"/conversations/{c['id']}", headers=self.employee).text
        self.assertNotIn('CONFIDENTIAL-TEXT', history)
        self.assertNotIn('DERIVED-SECRET', history)

    def test_access_change_during_model_call_prevents_storage(self):
        d, _, c = self.setup_chat()
        def generate(req):
            with connection() as db:
                db.execute("UPDATE documents SET visibility='admin' WHERE id=?", (d['id'],))
            return ModelResponse('CONFIDENTIAL-TEXT')
        with patch('app.chat.get_model_client') as model:
            model.return_value.generate.side_effect = generate
            self.assertEqual(self.send(c).status_code, 409)
        with connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_legacy_answers_fail_closed(self):
        _, _, c = self.setup_chat()
        with connection() as db:
            db.execute("INSERT INTO messages(id,conversation_id,role,content) VALUES ('legacy',?,'assistant','OLD-SECRET')", (c['id'],))
        r = self.client.get(f"/conversations/{c['id']}", headers=self.employee)
        self.assertNotIn('OLD-SECRET', r.text)
        self.assertTrue(r.json()['messages'][0]['redacted'])

    def test_approval_exact_version_and_replay(self):
        _, _, _, action = self.draft()
        args = dict(action['arguments'], title='NEW reviewed title')
        r = self.client.patch(f"/actions/{action['id']}", headers=self.employee, json={'version': 1, 'arguments': args})
        self.assertEqual(r.json()['version'], 2)
        for v, status in [(1, 409), (2, 200), (2, 409)]:
            self.assertEqual(self.approve(action, v).status_code, status)
        with connection() as db:
            self.assertEqual([r['title'] for r in db.execute('SELECT title FROM tickets')], ['NEW reviewed title'])

    def test_concurrent_edit_cannot_replace_approved_arguments(self):
        import app.actions as actions
        _, _, _, action = self.draft()
        entered, attempted = threading.Event(), threading.Event()
        original_owned = actions._owned
        def observed(db, aid, user):
            row = original_owned(db, aid, user)
            if not entered.is_set():
                entered.set()
                self.assertTrue(attempted.wait(3))
            return row
        def edit():
            self.assertTrue(entered.wait(3))
            attempted.set()
            return self.client.patch(f"/actions/{action['id']}", headers=self.employee,
                json={'version': 1, 'arguments': dict(action['arguments'], title='UNREVIEWED')})
        with patch('app.actions._owned', side_effect=observed), ThreadPoolExecutor(2) as pool:
            a, e = pool.submit(self.approve, action), pool.submit(edit)
            self.assertEqual(a.result(timeout=10).status_code, 200)
            self.assertEqual(e.result(timeout=10).status_code, 409)
        with connection() as db:
            self.assertEqual(db.execute('SELECT title FROM tickets').fetchone()[0], action['arguments']['title'])

    def test_revoked_tool_cannot_execute(self):
        _, a, _, action = self.draft()
        self.client.put(f"/assistants/{a['id']}/tools", headers=self.admin, json={'tools': []})
        self.assertEqual(self.approve(action).status_code, 403)

    def test_tool_failure_rolls_back_partial_effects(self):
        from app.tools import TOOLS, Tool
        _, _, _, action = self.draft()
        tool = TOOLS['create_ticket']
        def broken(args, user, aid, db):
            tool.execute(args, user, aid, db)
            raise RuntimeError('sensitive internal failure')
        with patch.dict(TOOLS, {'create_ticket': Tool(tool.name, tool.description, tool.args_model, broken)}):
            r = self.approve(action)
        self.assertEqual(r.json()['status'], 'failed')
        self.assertNotIn('sensitive', r.text)
        with connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 0)

    def test_invalid_model_output_not_stored(self):
        _, _, c = self.setup_chat()
        for reply in [ModelResponse('x' * 16001), ModelResponse('x', [{}] * 4), {'text': 'x'}]:
            self.assertEqual(self.send(c, reply=reply).status_code, 503)
        with connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_model_config_validation(self):
        for url in ['file:///etc/passwd', 'http://user:secret@localhost', 'http://localhost/?token=secret',
                    'http://remote.example', 'https://remote.example', 'http://localhost:bad']:
            with self.assertRaises(ModelUnavailable):
                OllamaModel(url, 'test', 10)
        for value in ['nan', 'inf', '-1', 'not-a-number', '10000']:
            with patch.dict(os.environ, {'MODEL_BACKEND': 'ollama', 'MODEL_TIMEOUT_SECONDS': value}):
                with self.assertRaises(ModelUnavailable):
                    get_model_client()

    def test_model_total_timeout(self):
        async def slow(*args):
            await asyncio.Event().wait()
        model = OllamaModel('http://localhost:11434', 'test', 1)
        model.timeout = 0.01
        with patch.object(model, '_request', side_effect=slow), self.assertRaises(ModelUnavailable):
            model.generate(ModelRequest('', '', '', [], 'VPN', [], []))

    def test_model_transport_limits_and_disables_proxies_redirects(self):
        original = httpx.AsyncClient
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'x' * (MAX_MODEL_BYTES + 1)
        class SafeBytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"message": {"content": "safe"}}'
        for kind in ['oversize', 'redirect', 'compressed', 'success']:
            def handler(req):
                if kind == 'oversize': return httpx.Response(200, stream=Bytes())
                if kind == 'redirect': return httpx.Response(302, headers={'location': 'https://other.example'})
                if kind == 'compressed': return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=Bytes())
                return httpx.Response(200, stream=SafeBytes())
            def factory(**kwargs):
                self.assertFalse(kwargs['trust_env'])
                self.assertFalse(kwargs['follow_redirects'])
                return original(**kwargs, transport=httpx.MockTransport(handler))
            model = OllamaModel('http://localhost:11434', 'test', 1)
            with patch('app.model_client.httpx.AsyncClient', side_effect=factory):
                if kind == 'success':
                    self.assertEqual(model.generate(ModelRequest('', '', '', [], 'VPN', [], [])).text, 'safe')
                else:
                    with self.assertRaises(ModelUnavailable):
                        model.generate(ModelRequest('', '', '', [], 'VPN', [], []))


del BackendTests

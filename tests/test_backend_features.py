import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.test_chat import ChatBase, FakeModel
from app.auth import User
from app.db import connection, initialize
from app.main import app
from app.model_client import ModelUnavailable
from app.turns import reserve_turn, release_turn, check_reservation


class BackendFeatureTests(ChatBase):
    def replacement(self, doc, text=b'VPN NEW-GUIDE', version=1, headers=None, name='updated.txt'):
        return self.client.put(f"/documents/{doc['id']}", headers=headers or self.admin,
            data={'expected_version': str(version)}, files={'file': (name, text)})

    def post_turn(self, conv, request_id, text='VPN ticket', revision=None, headers=None):
        body = {'content': text, 'request_id': request_id}
        if revision is not None:
            body['expected_revision'] = revision
        return self.client.post(f"/conversations/{conv['id']}/messages", headers=headers or self.employee, json=body)

    def test_replacement_preserves_identity_acl_collection_and_changes_search(self):
        d = self.upload('old.txt', b'VPN OLD-GUIDE')
        a = self.assistant([d['id']])
        chunks = self.client.get(f"/documents/{d['id']}/chunks", headers=self.employee).json()['chunks']
        r = self.replacement(d)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()['id'], r.json()['visibility'], r.json()['version']), (d['id'], 'employee', 2))
        self.assertEqual(self.client.get(f"/assistants/{a['id']}", headers=self.admin).json()['document_ids'], [d['id']])
        result = self.client.post('/search', headers=self.employee, json={'query': 'NEW-GUIDE'}).json()['results']
        self.assertTrue(result)
        self.assertFalse(set(c['chunk_id'] for c in chunks) & {c['chunk_id'] for c in result})
        self.assertEqual(self.client.post('/search', headers=self.employee, json={'query': 'OLD'}).json()['results'], [])
        self.assertEqual(self.client.get('/documents', headers=self.admin).json()['documents'][0]['version'], 2)

    def test_private_replacement_does_not_publish(self):
        d = self.upload('private.txt', b'VPN secret', 'admin')
        self.assertEqual(self.replacement(d).json()['visibility'], 'admin')
        self.assertEqual(self.client.get('/documents', headers=self.employee).json()['documents'], [])

    def test_replacement_auth_happens_before_form_parser(self):
        from starlette.requests import Request
        with patch.object(Request, '_get_form', side_effect=AssertionError('form reached')):
            for headers, expected in [({}, 401), (self.employee, 403)]:
                self.assertEqual(self.client.put('/documents/id', headers=headers, content=b'invalid').status_code, expected)

    def test_failed_or_stale_replacement_preserves_original(self):
        d = self.upload('old.txt', b'VPN ORIGINAL')
        for payload, name, expected in [(b'', 'x.txt', 422), (b'broken', 'x.pdf', 422),
                                        (b'bad', 'x.exe', 415), (b'x' * (10 * 1024 * 1024 + 1), 'x.txt', 413)]:
            self.assertEqual(self.replacement(d, payload, name=name).status_code, expected)
        self.assertEqual(self.replacement(d, b'VPN CURRENT').status_code, 200)
        self.assertEqual(self.replacement(d, b'VPN STALE', version=1).status_code, 409)
        r = self.client.get(f"/documents/{d['id']}/chunks", headers=self.employee)
        self.assertIn('CURRENT', r.text)
        self.assertNotIn('STALE', r.text)
        self.assertEqual(self.replacement({'id': 'missing'}).status_code, 404)

    def test_replacement_transaction_rolls_back_on_indexing_failure(self):
        d = self.upload('old.txt', b'VPN ORIGINAL')
        with connection() as db:
            db.execute("""CREATE TRIGGER fail_replacement BEFORE INSERT ON chunks
                WHEN NEW.text LIKE '%FAIL-MARKER%' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.put(f"/documents/{d['id']}", headers=self.admin,
                data={'expected_version': '1'}, files={'file': ('new.txt', b'FAIL-MARKER')})
            self.assertEqual(r.status_code, 500)
        self.assertIn('ORIGINAL', self.client.get(f"/documents/{d['id']}/chunks", headers=self.employee).text)
        with connection() as db:
            self.assertEqual(db.execute('SELECT version FROM documents WHERE id=?', (d['id'],)).fetchone()[0], 1)
        self.assertTrue(self.client.post('/search', headers=self.employee, json={'query': 'ORIGINAL'}).json()['results'])

    def test_replacement_withholds_old_answer_and_blocks_old_action(self):
        d = self.upload('old.txt', b'VPN OLD-SECRET')
        c = self.conversation(self.assistant([d['id']], ['create_ticket']))
        msg = self.say(c, 'VPN ticket').json()['assistant_message']
        action = msg['actions'][0]
        self.assertEqual(self.replacement(d).status_code, 200)
        r = self.client.get(f"/conversations/{c['id']}", headers=self.employee)
        self.assertNotIn('OLD-SECRET', r.text)
        self.assertTrue(r.json()['messages'][1]['redacted'])
        self.assertEqual(self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={'version': 1}).status_code, 404)

    def test_concurrent_replacements_have_one_winner(self):
        d = self.upload('old.txt', b'VPN ORIGINAL')
        from app.documents import replace_document
        barrier = threading.Barrier(2)
        def replace(text):
            barrier.wait(timeout=3)
            try:
                return replace_document(d['id'], 1, 'new.txt', [text])['version']
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(replace, 'VPN FIRST'), pool.submit(replace, 'VPN SECOND')
            self.assertEqual(sorted([first.result(5), second.result(5)]), [2, 409])

    def test_retry_replays_same_ids_without_model_or_duplicate_draft(self):
        c = self.conversation(self.assistant(tools=['create_ticket']))
        request_id = str(uuid4())
        model = FakeModel('Answer', [{'tool': 'create_ticket', 'arguments': {'title': 'VPN', 'description': 'help'}}])
        with patch('app.chat.get_model_client', return_value=model):
            first, retry = self.post_turn(c, request_id), self.post_turn(c, request_id)
        self.assertEqual((first.status_code, retry.status_code), (201, 201))
        self.assertEqual(len(model.requests), 1)
        self.assertFalse(first.json()['replayed'])
        self.assertTrue(retry.json()['replayed'])
        for key in ['user_message', 'assistant_message']:
            self.assertEqual(first.json()[key]['id'], retry.json()[key]['id'])
        self.assertEqual((self.count('messages'), self.count('actions')), (2, 1))
        self.assertEqual(first.json()['revision'], 1)

    def test_reused_request_id_rejects_changed_payload(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        self.assertEqual(self.post_turn(c, rid).status_code, 201)
        self.assertEqual(self.post_turn(c, rid, text='changed').status_code, 409)
        self.assertEqual(self.post_turn(c, rid, revision=1).status_code, 409)
        self.assertEqual(self.count('messages'), 2)

    def test_model_failure_can_retry_same_key(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        with patch('app.chat.get_model_client', return_value=FakeModel(error=ModelUnavailable('down'))):
            self.assertEqual(self.post_turn(c, rid).status_code, 503)
        self.assertEqual(self.count('messages'), 0)
        self.assertEqual(self.post_turn(c, rid).status_code, 201)
        self.assertEqual(self.count('messages'), 2)

    def test_pending_same_or_different_request_conflicts_then_uses_latest_history(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        entered, unblock = threading.Event(), threading.Event()
        model = FakeModel('first answer')
        original = model.generate
        def blocked(req):
            entered.set()
            self.assertTrue(unblock.wait(5))
            return original(req)
        with patch('app.chat.get_model_client', return_value=model), patch.object(model, 'generate', side_effect=blocked), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.post_turn, c, rid)
            try:
                self.assertTrue(entered.wait(3))
                for key in [rid, str(uuid4())]:
                    r = self.post_turn(c, key)
                    self.assertEqual(r.status_code, 409)
                    self.assertEqual(r.headers['retry-after'], '1')
                self.assertEqual(self.count('messages'), 0)
            finally:
                unblock.set()
            self.assertEqual(first.result(5).status_code, 201)
        model2 = FakeModel('second answer')
        with patch('app.chat.get_model_client', return_value=model2):
            self.assertEqual(self.post_turn(c, str(uuid4()), text='second').status_code, 201)
        self.assertEqual([h['content'] for h in model2.requests[0].history], ['VPN ticket', 'first answer'])

    def test_unrelated_conversation_can_run_while_first_is_reserved(self):
        a = self.assistant()
        first, second = self.conversation(a), self.conversation(a)
        reservation, _ = reserve_turn(first['id'], str(uuid4()), 'busy', None, User('demo-employee', 'employee'))
        try:
            self.assertEqual(self.post_turn(second, str(uuid4())).status_code, 201)
        finally:
            release_turn(reservation)

    def test_expected_revision_and_retry_after_later_turn(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        self.assertEqual(c['revision'], 0)
        self.assertEqual(self.post_turn(c, rid, revision=0).status_code, 201)
        self.assertEqual(self.post_turn(c, str(uuid4()), revision=0).status_code, 409)
        self.assertEqual(self.post_turn(c, str(uuid4()), revision=1).json()['revision'], 2)
        self.assertTrue(self.post_turn(c, rid, revision=0).json()['replayed'])
        self.assertEqual(self.client.get(f"/conversations/{c['id']}", headers=self.employee).json()['revision'], 2)

    def test_retry_rechecks_sources_and_current_action_status(self):
        d = self.upload('old.txt', b'VPN OLD-SECRET')
        c = self.conversation(self.assistant([d['id']], ['create_ticket']))
        rid = str(uuid4())
        action = self.post_turn(c, rid).json()['assistant_message']['actions'][0]
        self.client.post(f"/actions/{action['id']}/cancel", headers=self.employee)
        self.assertEqual(self.post_turn(c, rid).json()['assistant_message']['actions'][0]['status'], 'cancelled')
        self.replacement(d)
        replay = self.post_turn(c, rid)
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()['assistant_message']['redacted'])
        self.assertNotIn('OLD-SECRET', replay.text)
        self.assertEqual(self.count('messages'), 2)

    def test_expired_attempt_is_fenced_after_takeover(self):
        c = self.conversation(self.assistant())
        user, rid = User('demo-employee', 'employee'), str(uuid4())
        old, _ = reserve_turn(c['id'], rid, 'hello', None, user)
        with connection() as db:
            db.execute('UPDATE chat_requests SET lease_until=0')
        new, _ = reserve_turn(c['id'], rid, 'hello', None, user)
        release_turn(old)  # must not release the new reservation
        with connection() as db:
            self.assertEqual(db.execute('SELECT token FROM chat_requests').fetchone()[0], new.token)
            with self.assertRaises(HTTPException):
                check_reservation(db, old, user)
            check_reservation(db, new, user)
        release_turn(new)
        self.assertEqual(self.post_turn(c, rid, text='hello').status_code, 201)

    def test_reservation_is_visible_to_another_process(self):
        c = self.conversation(self.assistant())
        user = User('demo-employee', 'employee')
        reservation, _ = reserve_turn(c['id'], str(uuid4()), 'hello', None, user)
        code = '''import sys
from uuid import uuid4
from fastapi import HTTPException
from app.auth import User
from app.turns import reserve_turn,release_turn
try:
 r,_=reserve_turn(sys.argv[1],str(uuid4()),'other',None,User('demo-employee','employee'))
 release_turn(r)
 print(200)
except HTTPException as e:print(e.status_code)
'''
        try:
            result = subprocess.run([sys.executable, '-c', code, c['id']], capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(result.stdout.strip(), '409')
        finally:
            release_turn(reservation)

    def test_replay_survives_new_client_and_delete_cascades_receipts(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        first = self.post_turn(c, rid).json()
        with TestClient(app) as other:
            retry = other.post(f"/conversations/{c['id']}/messages", headers=self.employee,
                               json={'content': 'VPN ticket', 'request_id': rid}).json()
            self.assertTrue(retry['replayed'])
            self.assertEqual(first['assistant_message']['id'], retry['assistant_message']['id'])
        self.client.delete(f"/conversations/{c['id']}", headers=self.employee)
        self.assertEqual(self.count('chat_requests'), 0)
        self.assertEqual(self.post_turn(c, rid).status_code, 404)

    def test_request_id_format_and_cross_owner_replay(self):
        c = self.conversation(self.assistant())
        rid = str(uuid4())
        self.assertEqual(self.post_turn(c, 'invalid').status_code, 422)
        self.assertEqual(self.post_turn(c, rid).status_code, 201)
        self.assertEqual(self.post_turn(c, rid, headers=self.admin).status_code, 404)

    def test_old_database_migration_is_repeatable(self):
        # Existing tables/data survive; initialization may run on every restart.
        d = self.upload('old.txt', b'VPN ORIGINAL')
        c = self.conversation(self.assistant([d['id']]))
        with connection() as db:
            db.execute('DROP INDEX idx_one_pending_turn')
            db.execute('DROP TABLE chat_requests')
            db.execute('ALTER TABLE documents DROP COLUMN version')
            db.execute('ALTER TABLE conversations DROP COLUMN revision')
        initialize()
        initialize()
        self.assertEqual(self.client.get('/documents', headers=self.admin).json()['documents'][0]['version'], 1)
        self.assertEqual(self.client.get(f"/conversations/{c['id']}", headers=self.employee).json()['revision'], 0)
        self.assertEqual(self.post_turn(c, str(uuid4())).status_code, 201)

    def test_replacement_during_generation_rejects_outdated_answer_then_retry_works(self):
        d = self.upload('old.txt', b'VPN OLD-SECRET')
        c = self.conversation(self.assistant([d['id']]))
        rid = str(uuid4())
        model = FakeModel('OLD-SECRET')
        original = model.generate
        def generate(req):
            self.assertEqual(self.replacement(d).status_code, 200)
            return original(req)
        with patch('app.chat.get_model_client', return_value=model), patch.object(model, 'generate', side_effect=generate):
            self.assertEqual(self.post_turn(c, rid).status_code, 409)
        self.assertEqual(self.count('messages'), 0)
        retry = self.post_turn(c, rid)
        self.assertEqual(retry.status_code, 201)
        self.assertIn('NEW-GUIDE', retry.json()['assistant_message']['content'])

    def test_failed_message_commit_rolls_back_and_releases_reservation(self):
        c = self.conversation(self.assistant(tools=['create_ticket']))
        rid = str(uuid4())
        with connection() as db:
            db.execute("""CREATE TRIGGER fail_assistant BEFORE INSERT ON messages
                WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(f"/conversations/{c['id']}/messages", headers=self.employee,
                           json={'request_id': rid, 'content': 'VPN ticket'})
            self.assertEqual(r.status_code, 500)
        self.assertEqual((self.count('messages'), self.count('actions')), (0, 0))
        with connection() as db:
            self.assertEqual(db.execute('SELECT status FROM chat_requests').fetchone()[0], 'failed')
            db.execute('DROP TRIGGER fail_assistant')
        self.assertEqual(self.post_turn(c, rid).status_code, 201)
        self.assertEqual((self.count('messages'), self.count('actions')), (2, 1))

    def test_parallel_startup_migrations(self):
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(initialize), pool.submit(initialize)
            first.result(5)
            second.result(5)


del ChatBase

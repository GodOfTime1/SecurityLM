import json
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from tests.test_chat import ChatBase, FakeModel
from app.db import connection
from app.deployment import validate_deployment
from app.main import app
from app.model_client import ModelUnavailable


class DeploymentTests(ChatBase):
    def setUp(self):
        super().setUp()
        self.client.__exit__(None,None,None)
        self.admin = {'X-API-Key': 'admin-' + 'a' * 48}
        self.employee = {'X-API-Key': 'employee-' + 'b' * 48}
        entries = [{'id':'organizer','role':'admin','key':self.admin['X-API-Key']},
                   {'id':'judge-one','role':'employee','key':self.employee['X-API-Key']}]
        self.prod = patch.dict(os.environ, {'APP_ENV':'production','USER_API_KEYS':json.dumps(entries),
            'ALLOWED_HOSTS':'api.demo.example','CORS_ORIGINS':'https://demo.example',
            'TRUSTED_PROXY_IPS':'127.0.0.1','MODEL_BACKEND':'echo'})
        self.prod.start()
        app.middleware_stack = None
        self.client = TestClient(app,base_url='https://api.demo.example')
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.prod.stop()
        self.env.stop()
        self.temp.cleanup()
        app.middleware_stack = None

    def test_production_startup_rejects_unsafe_settings(self):
        for name,value in [('USER_API_KEYS',''),('ALLOWED_HOSTS','*'),('CORS_ORIGINS','*'),
            ('CORS_ORIGINS','http://demo.example'),('DATABASE_PATH','relative.db'),
            ('TRUSTED_PROXY_IPS','0.0.0.0/0'),('TRUSTED_PROXY_IPS','*'),
            ('TRUSTED_PROXY_IPS',''),('CHAT_REQUESTS_PER_MINUTE','0')]:
            with self.subTest(name=name,value=value),patch.dict(os.environ,{name:value}):
                with self.assertRaises((RuntimeError,ValueError)):
                    with TestClient(app,base_url='https://api.demo.example'):
                        pass
        validate_deployment()

    def test_shared_keys_rejected_in_production(self):
        raw = os.environ.pop('USER_API_KEYS')
        try:
            self.assertEqual(self.client.get('/me',headers=self.admin).status_code,503)
            with self.assertRaises(RuntimeError):validate_deployment()
        finally:
            os.environ['USER_API_KEYS'] = raw

    def test_docs_hidden_and_data_authenticated(self):
        for url in ['/docs','/docs/','/redoc','/openapi.json']:
            self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.get('/documents').status_code,401)
        self.assertEqual(self.client.get('/health').status_code,200)

    def test_host_https_and_origin_restrictions(self):
        self.assertEqual(self.client.get('/me',headers={**self.admin,'Host':'evil.example'}).status_code,400)
        self.assertEqual(self.client.get('http://api.demo.example/me',headers=self.admin).status_code,400)
        self.assertEqual(self.client.get('http://api.demo.example/me',headers={**self.admin,'X-Forwarded-Proto':'https'}).status_code,400)
        self.assertEqual(self.client.get('/me',headers={**self.admin,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/me',headers={**self.admin,'Origin':'null'}).status_code,403)
        r=self.client.get('/me',headers={**self.admin,'Origin':'https://demo.example'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['access-control-allow-origin'],'https://demo.example')
        self.assertEqual(r.headers['x-content-type-options'],'nosniff')
        self.assertIn('max-age=',r.headers['strict-transport-security'])

    def test_preflight_does_not_need_key_and_allows_only_declared_headers(self):
        headers={'Origin':'https://demo.example','Access-Control-Request-Method':'POST',
                 'Access-Control-Request-Headers':'X-API-Key,Content-Type'}
        self.assertEqual(self.client.options('/conversations',headers=headers).status_code,200)
        headers['Access-Control-Request-Headers']='X-Admin-Role'
        self.assertEqual(self.client.options('/conversations',headers=headers).status_code,400)

    def test_ip_limit_runs_before_body_parser_and_stays_bounded(self):
        with patch.dict(os.environ,{'IP_REQUESTS_PER_MINUTE':'2'}), patch('starlette.requests.Request._get_form',side_effect=AssertionError('parser reached')):
            statuses=[self.client.post('/documents',content=b'bad').status_code for _ in range(3)]
        self.assertEqual(statuses,[401,401,429])

    def test_user_limit_is_persistent_and_independent_per_identity(self):
        with patch.dict(os.environ,{'USER_REQUESTS_PER_MINUTE':'1'}):
            self.assertEqual(self.client.get('/me',headers=self.employee).status_code,200)
            r=self.client.get('/me',headers=self.employee)
            self.assertEqual(r.status_code,429)
            self.assertGreaterEqual(int(r.headers['retry-after']),1)
            with TestClient(app,base_url='https://api.demo.example') as second:
                self.assertEqual(second.get('/me',headers=self.employee).status_code,429)
            self.assertEqual(self.client.get('/me',headers=self.admin).status_code,200)

    def test_chat_limit_prevents_extra_model_call(self):
        c=self.conversation(self.assistant())
        fake=FakeModel()
        with patch.dict(os.environ,{'CHAT_REQUESTS_PER_MINUTE':'1'}),patch('app.chat.get_model_client',return_value=fake):
            self.assertEqual(self.say(c,'hello').status_code,201)
            self.assertEqual(self.say(c,'again').status_code,429)
        self.assertEqual(len(fake.requests),1)

    def test_document_count_quota_is_atomic_under_parallel_upload(self):
        barrier=threading.Barrier(2)
        def upload():
            barrier.wait(3)
            return self.client.post('/documents',headers=self.admin,files={'file':('a.txt',b'VPN')}).status_code
        with patch.dict(os.environ,{'MAX_DOCUMENTS':'1'}),ThreadPoolExecutor(2) as pool:
            a,b=pool.submit(upload),pool.submit(upload)
            self.assertEqual(sorted([a.result(5),b.result(5)]),[201,429])
        self.assertEqual(self.count('documents'),1)

    def test_document_byte_quota_preserves_existing_file_on_replacement(self):
        d=self.upload('original.txt',b'VPN abc')
        with patch.dict(os.environ,{'MAX_DOCUMENT_TEXT_BYTES':'7','MAX_DOCUMENTS':'1'}):
            r=self.client.put(f"/documents/{d['id']}",headers=self.admin,data={'expected_version':'1'},
                             files={'file':('new.txt',b'VPN abcdef')})
            self.assertEqual(r.status_code,413)
            self.assertIn('VPN abc',self.client.get(f"/documents/{d['id']}/chunks",headers=self.admin).text)
            os.environ['MAX_DOCUMENT_TEXT_BYTES']='14'  # 7 archived + 7 current bytes
            r=self.client.put(f"/documents/{d['id']}",headers=self.admin,data={'expected_version':'1'},
                             files={'file':('new.txt',b'VPN new')})
            self.assertEqual(r.status_code,200)

    def test_conversation_and_assistant_quotas(self):
        with patch.dict(os.environ,{'MAX_ASSISTANTS':'1','MAX_CONVERSATIONS_PER_USER':'1'}):
            a=self.assistant()
            self.assertEqual(self.client.post('/assistants',headers=self.admin,json={'name':'extra'}).status_code,429)
            self.conversation(a)
            self.assertEqual(self.client.post('/conversations',headers=self.employee,json={'assistant_id':a['id']}).status_code,429)
            self.assertEqual(self.client.post('/conversations',headers=self.admin,json={'assistant_id':a['id']}).status_code,201)

    def test_message_quota_blocks_new_turn_but_allows_completed_replay(self):
        from uuid import uuid4
        c=self.conversation(self.assistant())
        body={'content':'hello','request_id':str(uuid4())}
        with patch.dict(os.environ,{'MAX_MESSAGES_PER_CONVERSATION':'2'}):
            r=self.client.post(f"/conversations/{c['id']}/messages",headers=self.employee,json=body)
            self.assertEqual(r.status_code,201)
            self.assertEqual(self.say(c,'new').status_code,429)
            self.assertTrue(self.client.post(f"/conversations/{c['id']}/messages",headers=self.employee,json=body).json()['replayed'])

    def test_failed_request_receipt_quota(self):
        c=self.conversation(self.assistant())
        with patch.dict(os.environ,{'MAX_REQUEST_IDS_PER_CONVERSATION':'1'}),patch('app.chat.get_model_client',return_value=FakeModel(error=ModelUnavailable('down'))):
            self.assertEqual(self.say(c,'hello').status_code,503)
            self.assertEqual(self.say(c,'another').status_code,429)
        self.assertEqual(self.count('messages'),0)

    def test_request_logs_omit_queries_keys_and_unknown_paths(self):
        secret='PRIVATE_LOG_MARKER'
        with self.assertLogs('uvicorn.error',level='INFO') as captured:
            r=self.client.get('/'+secret+'?key='+secret,headers={**self.admin,'X-Request-ID':secret})
        self.assertEqual(r.status_code,404)
        logs=' '.join(captured.output)
        self.assertNotIn(secret,logs)
        self.assertNotIn(self.admin['X-API-Key'],logs)
        self.assertNotEqual(r.headers['x-request-id'],secret)
        self.assertIn(r.headers['x-request-id'],logs)

    def test_invite_generator_creates_private_file_and_refuses_overwrite(self):
        p=Path(self.temp.name)/'invites.json'
        args=[sys.executable,'scripts/create_invites.py','--admin','owner','--employee','judge','--output',str(p)]
        r=subprocess.run(args,capture_output=True,text=True,check=True)
        entries=json.loads(p.read_text())
        self.assertEqual([e['role'] for e in entries],['admin','employee'])
        self.assertTrue(all(len(e['key'])>=43 and e['key'] not in r.stdout for e in entries))
        if os.name=='posix':self.assertEqual(p.stat().st_mode & 0o777,0o600)
        self.assertNotEqual(subprocess.run(args,capture_output=True).returncode,0)
        self.assertEqual(json.loads(p.read_text()),entries)

    def test_user_rate_limit_is_atomic(self):
        barrier=threading.Barrier(2)
        def request():
            barrier.wait(3)
            return self.client.get('/me',headers=self.employee).status_code
        with patch.dict(os.environ,{'USER_REQUESTS_PER_MINUTE':'1'}),ThreadPoolExecutor(2) as pool:
            a,b=pool.submit(request),pool.submit(request)
            self.assertEqual(sorted([a.result(5),b.result(5)]),[200,429])

    def test_rate_window_resets_without_deleting_newer_buckets(self):
        from app.limits import rate_limit
        with patch.dict(os.environ,{'USER_REQUESTS_PER_MINUTE':'1'}):
            with patch('app.limits.time.time',return_value=6000):
                rate_limit('judge-one')
            with patch('app.limits.time.time',return_value=6060):
                rate_limit('judge-one')
            # A delayed old-window request must not remove the newer bucket.
            with patch('app.limits.time.time',return_value=5940):
                rate_limit('another-identity')
            with patch('app.limits.time.time',return_value=6060):
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as caught:
                    rate_limit('judge-one')
                self.assertEqual(caught.exception.status_code,429)

    def test_ticket_quota_bounds_records_retained_after_conversation_deletion(self):
        c=self.conversation(self.assistant(tools=['create_ticket']))
        with patch.dict(os.environ,{'MAX_TICKETS':'1'}):
            first=self.say(c,'ticket one').json()['assistant_message']['actions'][0]
            second=self.say(c,'ticket two').json()['assistant_message']['actions'][0]
            def approve(a):
                return self.client.post(f"/actions/{a['id']}/approve",headers=self.employee,json={'version':1})
            self.assertEqual(approve(first).json()['status'],'executed')
            self.assertEqual(approve(second).json()['status'],'failed')
        self.assertEqual(self.count('tickets'),1)

    def test_model_selector_shares_chat_rate_budget(self):
        entry={'id':'candidate','name':'Synthetic test candidate','runtime_model':'test:tag',
               'tasks':['document_qa'],'deployment':'local','estimated_memory_gb':4}
        self.assertEqual(self.client.post('/models',headers=self.admin,json=entry).status_code,201)
        fake=FakeModel(json.dumps({'answer':'Candidate fits the catalog filters.',
            'requirements':{},'recommended_model_ids':['candidate'],'follow_up_questions':[]}))
        body={'message':'Help choose','requirements':{'task':'document_qa','deployment':'local','available_memory_gb':8}}
        with patch.dict(os.environ,{'CHAT_REQUESTS_PER_MINUTE':'1'}),patch('app.model_selector.get_advisor_client',return_value=fake):
            self.assertEqual(self.client.post('/model-selector/chat',headers=self.employee,json=body).status_code,200)
            self.assertEqual(self.client.post('/model-selector/chat',headers=self.employee,json=body).status_code,429)
        self.assertEqual(len(fake.requests),1)


del ChatBase

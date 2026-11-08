import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import zipfile
from fastapi import HTTPException
from starlette.responses import JSONResponse
from tests.test_chat import ChatBase, FakeModel
from app.db import connection, initialize
from app.model_client import ModelRequest, ModelResponse, ModelUnavailable, OllamaModel
from app.advisor_client import get_advisor_client
from app.pii import preview, protect_messages, SensitiveDataError
from app.seed_demo import seed_demo
from app.security import SecurityMiddleware
from app.inference import generate_reply
from scripts.package_backend import package


class ReviewFixTests(ChatBase):
    def replace(self,d):
        r=self.client.put(f"/documents/{d['id']}",headers=self.admin,data={'expected_version':'1'},
                          files={'file':('new.txt',b'VPN NEW-TEXT')})
        self.assertEqual(r.status_code,200,r.text)

    def test_archived_answers_keep_original_citations_but_use_current_acls(self):
        d=self.upload('original.txt',b'VPN OLD-TEXT')
        a=self.assistant([d['id']])
        employee=self.conversation(a)
        admin=self.conversation(a,self.admin)
        self.say(employee,'VPN')
        self.say(admin,'VPN',self.admin)
        self.replace(d)
        msg=self.client.get(f"/conversations/{employee['id']}",headers=self.employee).json()['messages'][1]
        self.assertIn('OLD-TEXT',msg['content'])
        self.assertTrue(msg['stale_sources'])
        self.assertEqual(msg['citations'][0]['filename'],'original.txt')
        self.assertEqual(msg['citations'][0]['document_version'],1)
        with connection() as db:db.execute("UPDATE documents SET visibility='admin' WHERE id=?",(d['id'],))
        self.assertNotIn('OLD-TEXT',self.client.get(f"/conversations/{employee['id']}",headers=self.employee).text)
        self.assertIn('OLD-TEXT',self.client.get(f"/conversations/{admin['id']}",headers=self.admin).text)
        self.client.delete(f"/documents/{d['id']}",headers=self.admin)
        self.assertNotIn('OLD-TEXT',self.client.get(f"/conversations/{admin['id']}",headers=self.admin).text)
        self.assertEqual(self.count('archived_chunks'),0)

    def test_archived_answers_are_not_reintroduced_to_new_model_history(self):
        d=self.upload('original.txt',b'VPN OLD-TEXT')
        c=self.conversation(self.assistant([d['id']]))
        self.say(c,'VPN')
        self.replace(d)
        fake=FakeModel('New answer [1]')
        with patch('app.chat.get_model_client',return_value=fake):self.assertEqual(self.say(c,'VPN').status_code,201)
        self.assertNotIn('OLD-TEXT',str(fake.requests))
        self.assertIn('NEW-TEXT',str(fake.requests))

    def test_archived_sources_still_require_collection_membership(self):
        d=self.upload('original.txt',b'VPN OLD-TEXT')
        a=self.assistant([d['id']])
        c=self.conversation(a)
        self.say(c,'VPN')
        self.replace(d)
        self.client.put(f"/assistants/{a['id']}/documents",headers=self.admin,json={'document_ids':[]})
        self.assertNotIn('OLD-TEXT',self.client.get(f"/conversations/{c['id']}",headers=self.employee).text)

    def test_uncited_passage_can_influence_an_answer_and_must_remain_tracked(self):
        one=self.upload('one.txt',b'VPN FIRST-CONTEXT')
        two=self.upload('two.txt',b'VPN SECOND-CONTEXT')
        c=self.conversation(self.assistant([one['id'],two['id']]))
        fake=FakeModel()
        def generate(req):
            fake.requests.append(req)
            self.assertEqual(len(req.passages),2)
            return ModelResponse('Used second passage: '+req.passages[1].text+' [1]')
        with patch('app.chat.get_model_client',return_value=fake),patch.object(fake,'generate',side_effect=generate):
            self.assertEqual(self.say(c,'VPN').status_code,201)
        with connection() as db:
            did=db.execute('SELECT document_id FROM chunks WHERE id=?',(fake.requests[0].passages[1].chunk_id,)).fetchone()[0]
            db.execute("UPDATE documents SET visibility='admin' WHERE id=?",(did,))
        msg=self.client.get(f"/conversations/{c['id']}",headers=self.employee).json()['messages'][1]
        self.assertTrue(msg['redacted'])

    def test_development_cors_handles_preflights_without_bypassing_auth(self):
        with patch.dict(os.environ,{'DEV_CORS_ORIGINS':'http://localhost:5173'}):
            headers={'Origin':'http://localhost:5173','Access-Control-Request-Method':'POST',
                     'Access-Control-Request-Headers':'X-API-Key,Content-Type'}
            r=self.client.options('/conversations',headers=headers)
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['access-control-allow-origin'],'http://localhost:5173')
            self.assertEqual(self.client.get('/documents',headers={'Origin':headers['Origin']}).status_code,401)
            headers['Origin']='http://evil.example'
            self.assertEqual(self.client.options('/conversations',headers=headers).status_code,400)

    def test_body_slots_do_not_remain_held_by_slow_downstream_requests(self):
        async def check():
            started=0
            all_started=asyncio.Event()
            release=asyncio.Event()
            async def downstream(scope,receive,send):
                nonlocal started
                if scope['path']=='/slow':
                    await receive()
                    started+=1
                    if started==8:all_started.set()
                    await release.wait()
                await JSONResponse({'ok':True})(scope,receive,send)
            app=SecurityMiddleware(downstream)
            async def request(path):
                events=[]
                async def receive():return {'type':'http.request','body':b'','more_body':False}
                async def send(m):events.append(m)
                scope={'type':'http','path':path,'method':'GET','headers':[(b'x-api-key',self.employee['X-API-Key'].encode())]}
                await app(scope,receive,send)
                return events[0]['status']
            tasks=[asyncio.create_task(request('/slow')) for _ in range(8)]
            try:
                await asyncio.wait_for(all_started.wait(),2)
                self.assertEqual(await request('/me'),200)
                self.assertEqual(await request('/health'),200)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        asyncio.run(check())

    def test_inference_is_bounded_and_exception_releases_its_slot(self):
        ready,release=threading.Event(),threading.Event()
        class Slow:
            def generate(self,request):
                ready.set()
                if not release.wait(3):raise RuntimeError('test timeout')
                return ModelResponse('ok')
        req=ModelRequest('','','',[],'q',[],[])
        with patch('app.inference.slots',threading.BoundedSemaphore(1)),ThreadPoolExecutor(1) as pool:
            future=pool.submit(generate_reply,Slow(),req)
            try:
                self.assertTrue(ready.wait(2))
                with self.assertRaises(ModelUnavailable):generate_reply(Slow(),req)
                self.assertEqual(self.client.get('/me',headers=self.employee).status_code,200)
            finally:release.set()
            self.assertEqual(future.result(3).text,'ok')
            self.assertEqual(generate_reply(FakeModel('again'),req).text,'again')

    def test_sqlite_mode_timeout_and_busy_error(self):
        with connection() as db:
            self.assertIn(db.execute('PRAGMA journal_mode').fetchone()[0],{'wal','delete'})
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0],5000)
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(HTTPException) as caught:
                with connection() as other:
                    other.execute('PRAGMA busy_timeout=0')
                    other.execute('BEGIN IMMEDIATE')
            self.assertEqual(caught.exception.status_code,503)
        with self.assertRaises(sqlite3.OperationalError):
            with connection() as db:db.execute('not valid SQL')

    def test_unpatched_sqlite_falls_back_and_explicit_wal_fails_closed(self):
        with patch('app.db.sqlite3.sqlite_version_info',(3,45,0)):
            initialize()
            with connection() as db:self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            with patch.dict(os.environ,{'SQLITE_JOURNAL_MODE':'wal'}),self.assertRaises(RuntimeError):initialize()

    def test_preview_masks_supported_pii_and_secret_material(self):
        cases={'alice@example.org':'email','123-45-6789':'ssn','(212) 555-0100':'phone',
               '4111 1111 1111 1111':'credit_card','{"api_key":"abcdefghijklmnop"}':'secret'}
        for text,category in cases.items():
            r=self.client.post('/privacy/check',headers=self.employee,json={'text':text})
            self.assertEqual(r.status_code,200)
            self.assertIn(category,r.json()['categories'])
            self.assertNotIn(text,r.json()['redacted_text'])
        self.assertEqual(self.client.post('/privacy/check',json={'text':'x'}).status_code,401)
        self.assertEqual(preview('Names and health details can be sensitive without a regex match')['detected'],False)
        self.assertIn('not proof',preview('hello')['coverage'])

    def test_remote_sensitive_payload_blocked_before_model_request(self):
        model=OllamaModel('https://controlled.example','test',1,allow_remote=True)
        self.assertEqual(model.data_policy,'block_sensitive')
        req=ModelRequest('test','','',[],'Email alice@example.org',[],[])
        with patch.object(model,'_request',side_effect=AssertionError('network reached')),self.assertRaises(ModelUnavailable):model.generate(req)

    def test_redaction_changes_outgoing_payload_not_original_request(self):
        model=OllamaModel('https://controlled.example','test',1,allow_remote=True,data_policy='redact')
        payloads=[]
        async def request(payload):payloads.append(payload);return ModelResponse('ok')
        req=ModelRequest('test','','',[],'Email alice@example.org',[],[])
        with patch.object(model,'_request',side_effect=request):self.assertEqual(model.generate(req).text,'ok')
        self.assertNotIn('alice@example.org',json.dumps(payloads))
        self.assertIn('alice@example.org',req.question)
        req.question='api_key=abcdefghijklmnop'
        with patch.object(model,'_request',side_effect=AssertionError('network reached')),self.assertRaises(ModelUnavailable):model.generate(req)

    def test_advisor_egress_policy_does_not_inherit_document_policy(self):
        with patch.dict(os.environ,{'MODEL_DATA_POLICY':'allow','ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'test',
            'ADVISOR_BASE_URL':'https://controlled.example','ADVISOR_ALLOW_REMOTE':'1','ADVISOR_DATA_POLICY':'block_sensitive'}):
            advisor=get_advisor_client()
            self.assertEqual(advisor.adapter.data_policy,'block_sensitive')

    def test_seed_is_repeatable_and_does_not_call_model_or_forge_real_catalog(self):
        first=seed_demo()
        second=seed_demo()
        self.assertEqual(first,second)
        self.assertEqual((self.count('documents'),self.count('assistants'),self.count('model_catalog')),(2,1,1))
        c=self.conversation({'id':first['assistant_id']})
        self.assertNotIn('ADMIN-CANARY',self.say(c,'VPN').text)
        r=self.client.post('/model-selector/chat',headers=self.employee,
            json={'message':'Choose','requirements':{'task':'document_qa','deployment':'either'}})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['mode'],'demo')
        self.assertIn('Synthetic',r.json()['recommendations'][0]['name'])
        with patch.dict(os.environ,{'APP_ENV':'production'}),self.assertRaises(RuntimeError):seed_demo()

    def test_source_packager_ignores_local_data_even_if_gitignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files={'app/main.py':'# source','requirements.txt':'','tests/test_x.py':'# test',
                   '.github/workflows/backend-ci.yml':'name: test','.env':'SECRET',
                   'runtime/company.db':'SECRET','.venv/lib/private.py':'SECRET','.git/config':'SECRET',
                   'app/__pycache__/main.pyc':'SECRET','fixtures/invites-demo.json':'SECRET',
                   'fixtures/model-catalog.example.json':'{}','old.zip':'SECRET'}
            for name,text in files.items():
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
            output=root/'dist/source.zip'
            package(root,output)
            with zipfile.ZipFile(output) as z:
                self.assertEqual(set(z.namelist()),{'backend/app/main.py','backend/requirements.txt',
                    'backend/tests/test_x.py','backend/.github/workflows/backend-ci.yml','backend/fixtures/model-catalog.example.json'})

    def test_private_archived_version_is_not_declassified_with_new_public_version(self):
        from app.auth import User
        from app.conversations import message_view
        d=self.upload('private.txt',b'VPN PRIVATE-ARCHIVE','admin')
        c=self.conversation(self.assistant([d['id']]),self.admin)
        self.say(c,'VPN',self.admin)
        self.replace(d)
        with connection() as db:
            db.execute("UPDATE documents SET visibility='employee' WHERE id=?",(d['id'],))
            row=db.execute("SELECT * FROM messages WHERE conversation_id=? AND role='assistant'",(c['id'],)).fetchone()
            view=message_view(db,row,User('demo-admin','employee'))
            self.assertTrue(view['redacted'])
            self.assertNotIn('PRIVATE-ARCHIVE',str(view))


del ChatBase

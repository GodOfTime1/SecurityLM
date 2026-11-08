import json
import os
import unittest
from unittest.mock import patch
from tests.test_chat import ChatBase, FakeModel
from tests.test_model_selector import ModelSelectorTests
from app.advisor_client import AdvisorClient, AdvisorSettings, get_advisor_client, get_advisor_settings
from app.model_client import ModelRequest, ModelResponse, ModelUnavailable, ToolSpec, get_model_client


class AdvisorInfrastructureTests(ChatBase):
    entry=ModelSelectorTests.entry
    add=ModelSelectorTests.add
    chat=ModelSelectorTests.chat

    def status(self):
        return self.client.get('/model-selector/status',headers=self.employee)

    def test_status_requires_identity_and_reports_catalog_and_demo_truthfully(self):
        self.assertEqual(self.client.get('/model-selector/status').status_code,401)
        r=self.status().json()
        self.assertEqual((r['state'],r['demo'],r['active_catalog_count']),('demo',True,0))
        self.assertFalse(r['connection_checked'])
        self.assertFalse(r['can_request_advice'])
        self.add()
        self.assertTrue(self.status().json()['can_request_advice'])
        self.assertTrue(self.chat().json()['mode']=='demo')

    def test_advisor_defaults_to_demo_independently_of_document_backend(self):
        self.add()
        with patch.dict(os.environ,{'MODEL_BACKEND':'ollama','OLLAMA_URL':'INVALID-DOCUMENT-ENDPOINT','SELECTOR_MODEL':'old-tag'}):
            old=os.environ.pop('ADVISOR_BACKEND')
            try:
                self.assertEqual(get_advisor_settings().backend,'echo')
                r=self.chat()
                self.assertEqual(r.status_code,200,r.text)
                self.assertEqual(r.json()['mode'],'demo')
            finally:os.environ['ADVISOR_BACKEND']=old

    def test_two_model_clients_use_distinct_servers_models_and_timeouts(self):
        with patch.dict(os.environ,{'MODEL_BACKEND':'ollama','OLLAMA_URL':'http://127.0.0.1:11435',
            'OLLAMA_DEFAULT_MODEL':'assistant-model','MODEL_TIMEOUT_SECONDS':'90',
            'ADVISOR_BACKEND':'ollama','ADVISOR_BASE_URL':'http://127.0.0.1:11434',
            'ADVISOR_MODEL':'advisor-model','ADVISOR_TIMEOUT_SECONDS':'20'}):
            doc,advisor=get_model_client(),get_advisor_client()
            self.assertEqual(doc.base_url,'http://127.0.0.1:11435')
            self.assertEqual(doc.default_model,'assistant-model')
            self.assertEqual(doc.timeout,90)
            self.assertEqual(advisor.adapter.base_url,'http://127.0.0.1:11434')
            self.assertEqual(advisor.settings.model,'advisor-model')
            self.assertEqual(advisor.settings.timeout,20)
            self.assertEqual(os.environ['OLLAMA_URL'],'http://127.0.0.1:11435')

    def test_disabled_advisor_leaves_document_chat_working(self):
        self.add()
        conv=self.conversation(self.assistant())
        with patch.dict(os.environ,{'ADVISOR_BACKEND':'disabled'}):
            self.assertEqual(self.status().json()['state'],'disabled')
            self.assertFalse(self.status().json()['can_request_advice'])
            self.assertEqual(self.chat().status_code,503)
            self.assertEqual(self.chat(requirements={'task':'coding'}).status_code,503)
            self.assertEqual(self.say(conv,'hello').status_code,201)

    def test_missing_live_model_does_not_inherit_document_or_legacy_model(self):
        self.add()
        with patch.dict(os.environ,{'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'',
            'SELECTOR_MODEL':'legacy-advisor','OLLAMA_DEFAULT_MODEL':'document-model'}):
            self.assertEqual(self.status().json()['state'],'needs_configuration')
            r=self.chat()
            self.assertEqual(r.status_code,503)
            self.assertIn('ADVISOR_MODEL',r.text)

    def test_invalid_settings_do_not_echo_secrets(self):
        self.add()
        for update in [{'ADVISOR_BACKEND':'unsupported'},
                       {'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'x','ADVISOR_TIMEOUT_SECONDS':'nan'},
                       {'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'x','ADVISOR_TIMEOUT_SECONDS':'121'},
                       {'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'x','ADVISOR_ALLOW_REMOTE':'yes'},
                       {'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'x','ADVISOR_BASE_URL':'https://user:SECRET_MARKER@host.example'}]:
            with self.subTest(update=update),patch.dict(os.environ,update):
                self.assertEqual(self.status().json()['state'],'needs_configuration')
                self.assertNotIn('SECRET_MARKER',self.status().text)
                self.assertEqual(self.chat().status_code,503)

    def test_remote_permission_is_independent_and_requires_https(self):
        with patch.dict(os.environ,{'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'x',
            'ADVISOR_BASE_URL':'https://controlled.example','OLLAMA_ALLOW_REMOTE':'1','ADVISOR_ALLOW_REMOTE':'0'}):
            with self.assertRaises(ModelUnavailable):get_advisor_client()
            with patch.dict(os.environ,{'ADVISOR_ALLOW_REMOTE':'1'}):
                self.assertEqual(get_advisor_client().settings.base_url,'https://controlled.example')
                with patch.dict(os.environ,{'ADVISOR_BASE_URL':'http://controlled.example'}):
                    with self.assertRaises(ModelUnavailable):get_advisor_client()

    def test_status_does_not_probe_or_disclose_model_server(self):
        self.add()
        with patch.dict(os.environ,{'ADVISOR_BACKEND':'ollama','ADVISOR_MODEL':'private-runtime-tag',
            'ADVISOR_BASE_URL':'http://127.0.0.1:19999'}),patch('app.model_client.httpx.AsyncClient',side_effect=AssertionError('unexpected network')):
            r=self.status()
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['state'],'configured')
            self.assertFalse(r.json()['connection_checked'])
            self.assertNotIn('19999',r.text)
            self.assertNotIn('private-runtime-tag',r.text)

    def test_binding_pins_model_and_rejects_document_or_tool_requests(self):
        fake=FakeModel('{}')
        client=AdvisorClient(fake,AdvisorSettings('ollama',model='fixed-advisor'))
        req=ModelRequest('selected-assistant-model','','',[],'q',[],[],mode='model_selector')
        client.generate(req)
        self.assertEqual(fake.requests[0].model,'fixed-advisor')
        self.assertEqual(req.model,'selected-assistant-model')
        req.mode='document_qa'
        with self.assertRaises(ModelUnavailable):client.generate(req)
        req.mode='model_selector'
        req.tools=[ToolSpec('create_ticket','x',{})]
        with self.assertRaises(ModelUnavailable):client.generate(req)

    def test_selector_requests_carry_response_schema(self):
        self.add()
        fake=FakeModel(json.dumps({'answer':'Match','requirements':{},'recommended_model_ids':['small-local'],'follow_up_questions':[]}))
        with patch('app.model_selector.get_advisor_client',return_value=fake):
            self.assertEqual(self.chat().status_code,200)
        self.assertIn('recommended_model_ids',fake.requests[0].response_schema['properties'])

    def test_native_schema_format_is_applied_only_when_requested(self):
        from app.model_client import OllamaModel
        model=OllamaModel('http://127.0.0.1:11434','test',1)
        payloads=[]
        async def generate(payload):
            payloads.append(payload)
            return ModelResponse('{}')
        req=ModelRequest('test','','',[],'q',[],[],mode='model_selector',response_schema={'type':'object'})
        with patch.object(model,'_request',side_effect=generate):
            model.generate(req)
            req.response_schema=None
            model.generate(req)
        self.assertEqual(payloads[0]['format'],{'type':'object'})
        self.assertNotIn('format',payloads[1])


del ChatBase,ModelSelectorTests

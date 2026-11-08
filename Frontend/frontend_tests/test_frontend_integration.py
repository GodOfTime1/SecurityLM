"""Run from the frontend folder with the backend folder on PYTHONPATH."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from api_client import APIError, BackendAPI, backend_url, new_turn
from help_content import bounded_selector_history
import json
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.temp.name) / "demo.db"),
            "APP_ENV": "development", "MODEL_BACKEND": "echo", "ADVISOR_BACKEND": "echo",
            "ADMIN_API_KEY": "admin-" + "a" * 32, "EMPLOYEE_API_KEY": "employee-" + "b" * 32,
            "BACKEND_URL": "http://127.0.0.1:8000"})
        self.env.start()
        os.environ.pop("USER_API_KEYS", None)
        self.client = TestClient(app)
        self.client.__enter__()
        self.drop_reply = False
        self.fail_selector = False
        self.missing_presets = False
        self.selector_payloads = []

        def forward(request):
            if self.missing_presets and request.url.path in {'/model-presets','/runtime/status'}:
                return httpx.Response(404,json={'detail':'Not found'})
            if request.url.path=='/model-selector/chat':
                self.selector_payloads.append(json.loads(request.read()))
                if self.fail_selector:
                    return httpx.Response(503,json={'detail':'private upstream diagnostics must not be shown'})
            response = self.client.request(request.method, str(request.url),
                headers=dict(request.headers), content=request.read())
            if self.drop_reply and request.url.path.endswith("/messages") and response.status_code == 201:
                self.drop_reply = False
                raise httpx.ReadTimeout("simulated lost response")
            return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

        self.transport = httpx.MockTransport(forward)
        original = BackendAPI.__init__
        transport = self.transport

        def init(instance, key, **kwargs):
            original(instance, key, transport=transport)

        self.patcher = patch.object(BackendAPI, "__init__", init)
        self.patcher.start()
        self.admin = BackendAPI(os.environ["ADMIN_API_KEY"])
        self.employee = BackendAPI(os.environ["EMPLOYEE_API_KEY"])
        self.assistant = self.admin.request("POST", "/assistants", json={"name": "IT Helper"})

    def tearDown(self):
        self.patcher.stop()
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def page(self, path="home.py", employee=False):
        page = AppTest.from_file(str(ROOT / "home.py"), default_timeout=15)
        page.session_state["api_key"] = os.environ["EMPLOYEE_API_KEY" if employee else "ADMIN_API_KEY"]
        page.run()
        if path != "home.py":
            page.switch_page(path).run()
        self.assertFalse(page.exception, str(page.exception))
        return page

    def click(self, page, label):
        next(b for b in page.button if b.label == label).click().run()
        self.assertFalse(page.exception, str(page.exception))

    def test_upload_attach_chat_retry_replace_and_delete(self):
        doc = self.admin.upload("vpn.md", b"VPN instructions: connect using the company client.", "employee")
        self.admin.request("PUT", f"/assistants/{self.assistant['id']}/documents", json={"document_ids": [doc["id"]]})
        conversation = self.employee.request("POST", "/conversations", json={"assistant_id": self.assistant["id"]})
        payload = new_turn("VPN instructions", conversation["revision"])
        first = self.employee.request("POST", f"/conversations/{conversation['id']}/messages", json=payload)
        retry = self.employee.request("POST", f"/conversations/{conversation['id']}/messages", json=payload)
        self.assertTrue(retry["replayed"])
        self.assertEqual(first["assistant_message"]["id"], retry["assistant_message"]["id"])
        self.assertTrue(first["assistant_message"]["citations"])
        self.admin.request("PUT", f"/documents/{doc['id']}", files={"file": ("vpn.md", b"VPN updated instructions")}, data={"expected_version": "1"})
        old = self.employee.conversation(conversation["id"])["messages"][-1]
        self.assertTrue(old["stale_sources"])
        self.admin.request("DELETE", f"/documents/{doc['id']}")
        self.assertTrue(self.employee.conversation(conversation["id"])["messages"][-1]["redacted"])

    def test_employee_cannot_see_private_docs_or_mutate(self):
        doc = self.admin.upload("secret.md", b"Private VPN details", "admin")
        self.assertEqual(self.employee.all_items("/documents", "documents"), [])
        with self.assertRaises(APIError):
            self.employee.request("DELETE", f"/documents/{doc['id']}")
        page = self.page("pages/document_upload.py", employee=True)
        self.assertFalse(page.file_uploader)

    def test_home_creates_conversation_and_sends_message(self):
        page = self.page(employee=True)
        self.click(page, "New conversation")
        page.chat_input[0].set_value("Hello").run()
        self.assertFalse(page.exception, str(page.exception))
        self.assertEqual(len(page.chat_message), 2)
        cid = next(s.value for s in page.selectbox if s.label == "Conversation")
        self.assertNotIn("pending_" + cid, page.session_state)

    def test_lost_chat_response_retains_payload_and_replays(self):
        page = self.page(employee=True)
        self.click(page, "New conversation")
        self.drop_reply = True
        page.chat_input[0].set_value("Hello again").run()
        cid = next(s.value for s in page.selectbox if s.label == "Conversation")
        self.assertEqual(page.session_state["pending_" + cid]["content"], "Hello again")
        self.click(page, "Show pending-message controls")
        self.click(page, "Retry pending message")
        self.assertEqual(len(page.chat_message), 2)
        self.assertNotIn("pending_" + cid, page.session_state)

    def test_document_page_saves_collection_and_previews_safe_text(self):
        doc = self.admin.upload("<b>vpn</b>.md", b"VPN setup text", "employee")
        page = self.page("pages/document_upload.py")
        page.multiselect[0].select(doc["id"])
        self.click(page, "Save document collection")
        self.assertEqual(self.admin.request("GET", f"/assistants/{self.assistant['id']}")["document_ids"], [doc["id"]])
        self.click(page, "Open")
        self.assertTrue(any("VPN setup text" in t.value for t in page.text))

    def test_catalog_model_is_saved_with_version_check(self):
        self.admin.request("POST", "/models", json={"id": "test-model", "name": "Test fixture",
            "runtime_model": "fixture-tag", "tasks": ["document_qa"]})
        page = self.page("pages/model_settings.py")
        self.click(page, "Save model choice")
        self.assertEqual(self.admin.request("GET", f"/assistants/{self.assistant['id']}")["model"], "fixture-tag")

    def test_backend_destination_and_credential_redirect_protection(self):
        with patch.dict(os.environ, {"BACKEND_URL": "http://example.com"}):
            with self.assertRaises(APIError):
                backend_url()
        self.admin.transport = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://example.com"}))
        with self.assertRaises(APIError):
            self.admin.request("GET", "/me")

    def test_profile_creation_confirms_success_and_selects_new_profile(self):
        page = self.page()
        page.text_input[0].set_value("Finance Helper")
        page.text_area[0].set_value("Explain our expense policy")
        self.click(page, "Create assistant profile")
        self.assertTrue(any("Assistant profile created" in s.value for s in page.success))
        created = next(a for a in self.admin.all_items("/assistants", "assistants") if a["name"] == "Finance Helper")
        self.assertEqual(page.session_state["active_assistant_id"], created["id"])
        self.assertTrue(any(t.value == "Finance Helper" for t in page.title))
        page.switch_page("pages/document_upload.py").run()
        self.assertEqual(page.session_state["active_assistant_id"], created["id"])
        page.switch_page("home.py").run()
        self.assertTrue(any(t.value == "Finance Helper" for t in page.title))

    def test_empty_catalog_does_not_clutter_chat_and_employee_settings_blocked(self):
        page = self.page()
        self.assertFalse(any("catalog" in s.value.lower() for s in page.info))
        self.assertTrue(any("DEMO MODE" in s.value for s in page.warning))
        settings = self.page("pages/model_settings.py", employee=True)
        self.assertTrue(any("administrator" in s.value for s in settings.info))
        self.assertFalse(any(b.label == "Save model choice" for b in settings.button))

    def test_first_profile_onboarding_and_empty_name_validation(self):
        self.admin.request("DELETE", f"/assistants/{self.assistant['id']}")
        page = self.page()
        self.assertTrue(any(t.value == "Build your document assistant" for t in page.title))
        self.click(page, "Create assistant profile")
        self.assertTrue(page.warning)
        self.assertEqual(self.admin.all_items("/assistants", "assistants"), [])

    def test_three_presets_can_be_selected_without_claiming_live_inference(self):
        page=self.page()
        for name in ['Snake2','Llama4','Megladon8']:
            self.assertTrue(any(h.value==name for h in page.subheader))
        self.click(page,'Choose Snake2')
        assistant=self.admin.request('GET','/assistants/'+self.assistant['id'])
        self.assertEqual(assistant['model'],'preset:snake2')
        self.assertTrue(any('DEMO MODE' in w.value for w in page.warning))
        self.assertTrue(any('Snake2'==m.value for m in page.metric))

    def advisor_catalog(self):
        self.admin.request('POST','/models',json={'id':'help-fixture','name':'Help fixture',
            'runtime_model':'fixture-only','tasks':['document_qa'],'deployment':'remote'})

    def ask_advisor(self,page):
        next(s for s in page.selectbox if s.label=='Task').select('document_qa')
        next(s for s in page.selectbox if s.label=='Deployment').select('either')
        page.run()
        page.text_area[0].set_value('Help me choose a document assistant')
        self.click(page,'Ask model advisor')

    def test_help_navigation_and_reviewed_privacy_answer(self):
        page=self.page('pages/help.py')
        self.click(page,'Privacy & storage')
        answer=page.session_state['help_guide_messages'][-1]['content']
        self.assertIn('SQLite database',answer)
        self.assertIn('context leaves the backend',answer)
        self.assertIn('not a privacy certification',answer)
        self.assertFalse(self.selector_payloads)  # Guide clicks do not call a model.
        page.switch_page('pages/document_upload.py').run()
        page.switch_page('home.py').run()
        self.assertFalse(page.exception)

    def test_help_advisor_calls_api_and_applies_versioned_recommendation(self):
        self.advisor_catalog()
        self.admin.upload('secret.md',b'COMPANY-DOCUMENT-CANARY','admin')
        page=self.page('pages/help.py')
        self.ask_advisor(page)
        self.assertEqual(page.session_state['advisor_last']['mode'],'demo')
        self.assertEqual(self.selector_payloads[-1]['requirements']['task'],'document_qa')
        self.assertNotIn('COMPANY-DOCUMENT-CANARY',json.dumps(self.selector_payloads))
        self.assertEqual(len(page.session_state['advisor_messages']),2)
        self.click(page,'Apply to IT Helper')
        current=self.admin.request('GET','/assistants/'+self.assistant['id'])
        self.assertEqual(current['model'],'fixture-only')
        self.assertTrue(any('does not install or start' in s.value for s in page.success))

    def test_help_advisor_employee_can_ask_but_not_apply(self):
        self.advisor_catalog()
        page=self.page('pages/help.py',employee=True)
        self.ask_advisor(page)
        self.assertTrue(page.session_state['advisor_last']['recommendations'])
        self.assertFalse(any(b.label.startswith('Apply to ') for b in page.button))
        self.assertFalse(any(b.label=='Check document runtime' for b in page.button))

    def test_help_advisor_disabled_and_failure_are_not_fake_answers(self):
        self.advisor_catalog()
        with patch.dict(os.environ,{'ADVISOR_BACKEND':'disabled'}):
            page=self.page('pages/help.py')
            self.assertTrue(next(b for b in page.button if b.label=='Ask model advisor').disabled)
        page=self.page('pages/help.py')
        self.fail_selector=True
        self.ask_advisor(page)
        self.assertNotIn('advisor_last',page.session_state)
        self.assertNotIn('advisor_messages',page.session_state)
        self.assertTrue(page.error)
        self.assertNotIn('private upstream diagnostics',page.error[0].value)

    def test_advisor_history_is_bounded_and_excludes_other_roles(self):
        messages=[{'role':'user' if i%2 else 'assistant','content':str(i)+'x'*1999} for i in range(20)]
        messages.append({'role':'system','content':'not an allowed history role'})
        history=bounded_selector_history(messages)
        self.assertLessEqual(sum(len(m['content']) for m in history),4000)
        self.assertLessEqual(len(history),10)
        self.assertTrue(all(len(m['content'])<=2000 and m['role']!='system' for m in history))

    def test_old_backend_missing_presets_does_not_block_existing_chat(self):
        self.missing_presets=True
        page=self.page(employee=True)
        self.click(page,'New conversation')
        page.chat_input[0].set_value('Hello from an older backend').run()
        self.assertFalse(page.exception)
        self.assertEqual(len(page.chat_message),2)


if __name__ == "__main__":
    unittest.main()

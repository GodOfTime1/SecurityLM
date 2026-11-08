"""Run from the frontend folder with the backend folder on PYTHONPATH."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest

from api_client import APIError, BackendAPI, backend_url, new_turn
from help_content import bounded_selector_history
from document_service import upload_batch, link_documents
from types import SimpleNamespace
from unittest.mock import Mock
import session_cache
from ui_common import plain
import json
try:
    from app.main import app
except ModuleNotFoundError as exc:
    if exc.name not in {"app", "app.main"}:
        raise
    app = None

if app is not None:
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(app is None, "Backend source is not supplied; set PYTHONPATH to its directory to run contract integration tests.")
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
        self.calls = []
        self.fail_link = False

        def forward(request):
            self.calls.append((request.method, request.url.path))
            if self.fail_link and request.method == 'PUT' and request.url.path.endswith('/documents'):
                self.fail_link = False
                return httpx.Response(503, json={'detail': 'Unavailable'})
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
        self.assertEqual(len(page.chat_message), 2)  # Saved response is not duplicated optimistically.
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
        next(x for x in page.text_input if x.label=="Profile name").set_value("Finance Helper")
        next(x for x in page.text_area if x.label=="What should it help with?").set_value("Explain our expense policy")
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
        self.assertTrue(any("Document chat: Demo" in s.value for s in page.markdown))
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
        page=self.page('pages/model_settings.py')
        for name in ['Quick','Balanced','In-depth']:
            self.assertTrue(any(h.value==name for h in page.subheader))
        self.click(page,'Choose Quick')
        assistant=self.admin.request('GET','/assistants/'+self.assistant['id'])
        self.assertEqual(assistant['model'],'preset:snake2')
        self.assertTrue(any('Document chat: Demo' in w.value for w in page.markdown))
        page.switch_page('home.py').run()
        self.assertTrue(any('Quick'==m.value for m in page.metric))

    def advisor_catalog(self):
        self.admin.request('POST','/models',json={'id':'help-fixture','name':'Help fixture',
            'runtime_model':'fixture-only','tasks':['document_qa'],'deployment':'remote'})

    def ask_advisor(self,page):
        next(s for s in page.selectbox if s.label=='Task').select('document_qa')
        next(s for s in page.selectbox if s.label=='Deployment').select('either')
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

    def test_status_cached_but_identity_and_conversations_are_fresh(self):
        page = self.page()
        self.click(page, 'New conversation')
        self.calls.clear()
        page.run()
        page.run()
        self.assertEqual(self.calls.count(('GET', '/me')), 2)
        self.assertEqual(self.calls.count(('GET', '/conversations')), 2)
        self.assertFalse(any(path in session_cache.ALLOWED for method, path in self.calls))
        self.click(page, 'Refresh conversation')
        self.assertIn(('GET', '/runtime/status'), self.calls)

    def test_upload_link_preserves_collection_and_retry_never_reuploads(self):
        original = self.admin.upload('original.md', b'Existing knowledge', 'employee')
        link_documents(self.admin, self.assistant['id'], [original['id']])
        upload = SimpleNamespace(name='new.md', getvalue=lambda: b'New knowledge')
        self.fail_link = True
        result = upload_batch(self.admin, [upload], 'employee', self.assistant['id'])
        self.assertTrue(result['uploaded_ids'])
        self.assertFalse(result['linked'])
        self.assertTrue(result['link_error'])
        self.calls.clear()
        link_documents(self.admin, self.assistant['id'], result['uploaded_ids'])
        self.assertNotIn(('POST', '/documents'), self.calls)
        actual = self.admin.request('GET', '/assistants/'+self.assistant['id'])['document_ids']
        self.assertCountEqual(actual, [original['id']] + result['uploaded_ids'])
        self.assertEqual(len(self.admin.all_items('/documents', 'documents')), 2)

    def test_upload_ui_links_selected_assistant(self):
        upload = SimpleNamespace(name='linked.md', getvalue=lambda: b'Upload from form')
        with patch('streamlit.file_uploader', return_value=[upload]):
            page = self.page('pages/document_upload.py')
            self.click(page, 'Upload documents')
        current = self.admin.request('GET', '/assistants/'+self.assistant['id'])
        self.assertEqual(len(current['document_ids']), 1)
        self.assertTrue(any('linked to your assistant' in s.value for s in page.success))

    def test_optimistic_user_message_is_rendered_before_request(self):
        import ui_common
        events = []
        original_render = ui_common.render_message
        original_request = BackendAPI.request
        def render(message):
            if message.get('pending'): events.append('render')
            return original_render(message)
        def request(api, method, path, **kwargs):
            if method == 'POST' and path.endswith('/messages'): events.append('request')
            return original_request(api, method, path, **kwargs)
        with patch.object(ui_common, 'render_message', render), patch.object(BackendAPI, 'request', request):
            page = self.page(employee=True)
            self.click(page, 'New conversation')
            page.chat_input[0].set_value('Visible before inference').run()
        self.assertEqual(events, ['render', 'request'])
        self.assertEqual(len(page.chat_message), 2)

    def test_delete_conversation_requires_confirmation(self):
        page = self.page(employee=True)
        self.click(page, 'New conversation')
        self.assertTrue(next(b for b in page.button if b.label == 'Delete conversation').disabled)
        next(c for c in page.checkbox if c.label == 'Delete this conversation and its messages').check().run()
        self.click(page, 'Delete conversation')
        self.assertEqual(self.employee.all_items('/conversations', 'conversations'), [])

    def test_help_exposes_all_seven_topics(self):
        from help_content import TOPICS
        page = self.page('pages/help.py')
        self.assertEqual(len(TOPICS), 7)
        labels = {b.label for b in page.button}
        self.assertTrue(all(title in labels for title, answer in TOPICS.values()))

    def test_source_deletion_withdraws_answer_on_next_ui_run(self):
        doc = self.admin.upload('vpn.md', b'VPN instructions connect to company client.', 'employee')
        link_documents(self.admin, self.assistant['id'], [doc['id']])
        page = self.page(employee=True)
        self.click(page, 'New conversation')
        page.chat_input[0].set_value('VPN instructions').run()
        self.assertEqual(len(page.chat_message), 2)
        self.admin.request('DELETE', '/documents/'+doc['id'])
        page.run()
        self.assertTrue(any('withheld' in text.value.lower() for text in page.warning))
        self.assertFalse(any('company client' in text.value for text in page.text))

    def test_uploaded_metadata_is_escaped_in_headings(self):
        name = '![track](https://tracker.invalid/pixel)'
        current = self.admin.request('POST', '/assistants', json={'name': name})
        page = self.page()
        page.session_state['active_assistant_id'] = current['id']
        page.run()
        self.assertTrue(any(title.value == plain(name) for title in page.title))
        self.assertFalse(any(title.value == name for title in page.title))

    def test_display_budgets_match_backend_token_budgets(self):
        from app.model_client import PRESET_TOKEN_BUDGETS
        from constants import PRESET_BY_ID,active_reply_tokens
        reported={p['id']:p for p in self.admin.request('GET','/model-presets')['presets']}
        self.assertEqual({'preset:'+key:active_reply_tokens(value,reported[key]) for key,value in PRESET_BY_ID.items()},PRESET_TOKEN_BUDGETS)

    def test_saved_task_instructions_reach_the_actual_model_request(self):
        page=self.page('pages/model_settings.py')
        next(x for x in page.selectbox if x.label=='Primary task').select('troubleshooting')
        next(x for x in page.text_area if x.label=='Purpose & audience').set_value('Help employees use VPN')
        next(x for x in page.text_area if x.label=='Additional guidance').set_value('Explain acronyms and give numbered steps.')
        self.click(page,'Save assistant behavior')
        saved=self.admin.request('GET','/assistants/'+self.assistant['id'])
        self.assertIn('Task mode: troubleshooting',saved['system_prompt'])
        self.assertIn('Explain acronyms',saved['system_prompt'])
        import app.chat as chat
        original=chat.generate_reply;requests=[]
        def capture(client,request):
            requests.append(request);return original(client,request)
        with patch.object(chat,'generate_reply',capture):
            home=self.page(employee=True);self.click(home,'New conversation')
            home.chat_input[0].set_value('How do I use VPN?').run()
        self.assertFalse(home.exception)
        self.assertEqual(requests[0].system_prompt,saved['system_prompt'])
        self.assertEqual(requests[0].purpose,'Help employees use VPN')

    def test_behavior_conflict_blocks_unsaved_overwrite(self):
        page=self.page('pages/model_settings.py')
        self.admin.request('PATCH','/assistants/'+self.assistant['id'],json={'purpose':'Changed elsewhere'})
        self.calls.clear();self.click(page,'Save assistant behavior')
        self.assertNotIn(('PATCH','/assistants/'+self.assistant['id']),self.calls)
        self.assertTrue(any('changed while you were editing' in x.value for x in page.warning))

    def test_linked_source_search_respects_assistant_collection(self):
        allowed=self.admin.upload('linked.md',b'VPN linked setup company client','employee')
        self.admin.upload('unlinked.md',b'VPN UNLINKED-CANARY','employee')
        self.admin.upload('secret.md',b'VPN PRIVATE-CANARY','admin')
        link_documents(self.admin,self.assistant['id'],[allowed['id']])
        page=self.page(employee=True)
        next(x for x in page.text_input if x.label=='Search linked knowledge').set_value('VPN')
        self.click(page,'Find source passages')
        self.assertTrue(any('linked setup' in x.value for x in page.text))
        self.assertFalse(any('CANARY' in x.value for x in page.text))

    def test_advisor_prefill_uses_purpose_and_actual_catalog(self):
        self.advisor_catalog()
        self.admin.request('PATCH','/assistants/'+self.assistant['id'],json={'purpose':'Summarize IT manuals'})
        page=self.page('pages/help.py');self.click(page,'Use this assistant’s purpose')
        self.assertIn('Summarize IT manuals',page.text_area[0].value)
        next(x for x in page.selectbox if x.label=='Deployment').select('either')
        self.click(page,'Ask model advisor')
        self.assertIn('Summarize IT manuals',self.selector_payloads[-1]['message'])
        self.assertTrue(page.session_state['advisor_last']['recommendations'])

    def ticket_page(self):
        self.admin.request('PUT','/assistants/'+self.assistant['id']+'/tools',json={'tools':['create_ticket']})
        page=self.page(employee=True);self.click(page,'New conversation')
        page.chat_input[0].set_value('Create a ticket for my VPN connection').run()
        self.assertFalse(page.exception,str(page.exception))
        return page

    def test_ticket_requires_explicit_version_review_and_executes_once(self):
        page=self.ticket_page()
        self.assertTrue(next(x for x in page.button if x.label=='Approve & create ticket').disabled)
        self.assertEqual(self.admin.all_items('/tickets','tickets'),[])
        next(x for x in page.checkbox if x.label=='I reviewed this version and want to create the ticket').check().run()
        self.assertEqual(self.admin.all_items('/tickets','tickets'),[])
        self.click(page,'Approve & create ticket')
        tickets=self.admin.all_items('/tickets','tickets');self.assertEqual(len(tickets),1)
        page.run();self.assertEqual(len(self.admin.all_items('/tickets','tickets')),1)
        self.assertFalse(any(x.label=='Approve & create ticket' for x in page.button))

    def test_ticket_edit_requires_review_of_new_version(self):
        page=self.ticket_page()
        next(x for x in page.checkbox if x.label=='I reviewed this version and want to create the ticket').check().run()
        next(x for x in page.text_input if x.label=='Ticket title').set_value('VPN trouble for employee')
        self.click(page,'Save ticket draft')
        self.assertTrue(next(x for x in page.button if x.label=='Approve & create ticket').disabled)
        self.assertEqual(self.admin.all_items('/tickets','tickets'),[])
        self.click(page,'Cancel ticket draft')
        self.assertFalse(any(x.label=='Approve & create ticket' for x in page.button))
        self.assertEqual(self.admin.all_items('/tickets','tickets'),[])

    def test_pinned_notes_reach_model_beyond_recent_history_and_restore(self):
        import app.chat as chat
        from conversation_memory import unpack_prompt
        original=chat.generate_reply;requests=[]
        def capture(client,request):requests.append(request);return original(client,request)
        page=self.page(employee=True);self.click(page,'New conversation')
        next(x for x in page.text_area if x.label=='Pinned notes').set_value('Use Windows 11 and numbered steps.')
        self.click(page,'Save conversation notes')
        with patch.object(chat,'generate_reply',capture):
            for n in range(7):
                page.chat_input[0].set_value('VPN question '+str(n)).run()
                self.assertFalse(page.exception,str(page.exception))
        question,notes=unpack_prompt(requests[-1].question)
        self.assertEqual(question,'VPN question 6')
        self.assertIn('Windows 11',notes)
        self.assertLessEqual(len(requests[-1].history),10)
        fresh=self.page(employee=True)
        self.assertEqual(next(x for x in fresh.text_area if x.label=='Pinned notes').value,notes)

    def test_pinned_notes_do_not_leak_to_another_conversation(self):
        page=self.page(employee=True);self.click(page,'New conversation')
        next(x for x in page.text_area if x.label=='Pinned notes').set_value('CONVERSATION-ONLY-CANARY')
        self.click(page,'Save conversation notes')
        page.chat_input[0].set_value('First chat').run()
        self.click(page,'New conversation')
        self.assertEqual(next(x for x in page.text_area if x.label=='Pinned notes').value,'')
        page.chat_input[0].set_value('Second chat').run()
        cid=next(x for x in page.selectbox if x.label=='Conversation').value
        self.assertNotIn('CANARY',self.employee.conversation(cid)['messages'][0]['content'])

    def test_clear_pinned_notes_does_not_restore_older_pins(self):
        from conversation_memory import restored_notes
        page=self.page(employee=True);self.click(page,'New conversation')
        next(x for x in page.text_area if x.label=='Pinned notes').set_value('Original preference')
        self.click(page,'Save conversation notes');page.chat_input[0].set_value('Hello').run()
        next(x for x in page.text_area if x.label=='Pinned notes').set_value('')
        self.click(page,'Save conversation notes');page.chat_input[0].set_value('Start fresh').run()
        cid=next(x for x in page.selectbox if x.label=='Conversation').value
        self.assertEqual(restored_notes(self.employee.conversation(cid)['messages']),'')

    def test_answer_review_is_a_visible_user_turn(self):
        page=self.page(employee=True);self.click(page,'New conversation')
        page.chat_input[0].set_value('VPN instructions').run()
        self.click(page,'Check latest answer')
        cid=next(x for x in page.selectbox if x.label=='Conversation').value
        messages=self.employee.conversation(cid)['messages']
        self.assertEqual(len(messages),4)
        self.assertIn('Review your previous answer',messages[2]['content'])
        self.assertIn('VPN instructions',messages[2]['content'])


class HelperTests(unittest.TestCase):
    def test_metadata_ttl_scope_copy_and_explicit_invalidation(self):
        fake = SimpleNamespace(key='first-key', request=Mock(return_value={'state':'demo'}))
        state = {}
        with patch.object(session_cache, 'st', SimpleNamespace(session_state=state)), \
                patch.object(session_cache, 'backend_url', return_value='http://127.0.0.1:8000'), \
                patch.object(session_cache, 'monotonic', return_value=100) as clock:
            first = session_cache.metadata(fake, '/runtime/status')
            first['state'] = 'changed'
            self.assertEqual(session_cache.metadata(fake, '/runtime/status')['state'], 'demo')
            self.assertEqual(fake.request.call_count, 1)
            clock.return_value = 131
            session_cache.metadata(fake, '/runtime/status')
            self.assertEqual(fake.request.call_count, 2)
            fake.key = 'different-key'
            session_cache.metadata(fake, '/runtime/status')
            self.assertEqual(fake.request.call_count, 3)
            session_cache.invalidate_metadata()
            session_cache.metadata(fake, '/runtime/status')
            self.assertEqual(fake.request.call_count, 4)

    def test_sensitive_endpoints_cannot_be_cached(self):
        for path in ['/me', '/documents', '/conversations', '/conversations/example']:
            with self.assertRaises(ValueError): session_cache.metadata(None, path)

    def test_failed_metadata_request_is_not_cached(self):
        fake = SimpleNamespace(key='key', request=Mock(side_effect=APIError('Unavailable')))
        state = {}
        with patch.object(session_cache, 'st', SimpleNamespace(session_state=state)), \
                patch.object(session_cache, 'backend_url', return_value='http://127.0.0.1:8000'):
            for attempt in range(2):
                with self.assertRaises(APIError): session_cache.metadata(fake, '/runtime/status')
        self.assertEqual(fake.request.call_count, 2)
        self.assertNotIn('_setup_metadata', state)

    def test_config_preserves_theme_and_is_idempotent(self):
        from configure_frontend import update_config, configure, SETTINGS
        import tomllib
        old = '# My theme\n[theme]\nprimaryColor = "#abcdef"\n[server]\nport = 8502\nmaxUploadSize = 200\n'
        new = update_config(old)
        parsed = tomllib.loads(new)
        self.assertEqual(parsed['theme']['primaryColor'], '#abcdef')
        self.assertEqual(parsed['server']['port'], 8502)
        for section, values in SETTINGS.items():
            for key, value in values.items(): self.assertEqual(parsed[section][key], value)
        self.assertEqual(update_config(new), new)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'.streamlit/config.toml'
            path.parent.mkdir()
            path.write_text(old)
            _, backup = configure(directory)
            try: self.assertEqual(backup.read_text(), old)
            finally: backup.unlink()
            self.assertIsNone(configure(directory)[1])


if __name__ == "__main__":
    unittest.main()

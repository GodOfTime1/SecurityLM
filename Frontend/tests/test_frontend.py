"""Frontend tests run without backend source or a live service: python -m unittest discover -s tests -v."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import Mock, patch

import httpx
from streamlit.testing.v1 import AppTest
from api_client import APIError, BackendAPI, backend_url, validate_file, new_turn
from constants import MAX_UPLOAD_BYTES
from document_service import upload_batch, link_documents
from frontend_fixture import FrontendFixture
import ui_common

ROOT=Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def api(self, handler): return BackendAPI('test-secret',transport=httpx.MockTransport(handler))

    def test_external_request_paths_cannot_receive_key(self):
        send=Mock()
        for path in ['https://evil.invalid/me','//evil.invalid/me','/\\evil.invalid/me','me']:
            with self.assertRaises(APIError):self.api(send).request('GET',path)
        send.assert_not_called()

    def test_redirect_does_not_forward_credential(self):
        send=Mock(return_value=httpx.Response(302,headers={'location':'https://evil.invalid'}))
        with self.assertRaises(APIError):self.api(send).request('GET','/me')
        self.assertEqual(send.call_count,1)

    def test_untrusted_error_body_is_never_echoed(self):
        api=self.api(lambda r:httpx.Response(500,json={'detail':'PRIVATE-CANARY'}))
        with self.assertRaises(APIError) as error:api.request('GET','/documents')
        self.assertNotIn('PRIVATE-CANARY',str(error.exception))

    def test_invalid_success_json_becomes_api_error(self):
        with self.assertRaises(APIError):self.api(lambda r:httpx.Response(200,text='oops')).request('GET','/me')

    def test_timeout_does_not_claim_mutation_failed(self):
        def timeout(r):raise httpx.ReadTimeout('private diagnostic')
        with self.assertRaisesRegex(APIError,'may already be saved'):self.api(timeout).request('POST','/documents')
        with self.assertRaisesRegex(APIError,'Refresh and try again'):self.api(timeout).request('GET','/documents')

    def test_repeated_pagination_fails_instead_of_looping(self):
        send=Mock(return_value=httpx.Response(200,json={'documents':[{'id':str(n)} for n in range(100)]}))
        with self.assertRaisesRegex(APIError,'changed while loading'):self.api(send).all_items('/documents','documents')
        self.assertEqual(send.call_count,2)

    def test_malformed_list_is_a_readable_error(self):
        for payload in [[],{}, {'documents':None},{'documents':[{}]}]:
            with self.assertRaises(APIError):self.api(lambda r:httpx.Response(200,json=payload)).all_items('/documents','documents')

    def test_file_boundaries(self):
        for name,data in [('ok.md',b''),('bad.exe',b'x'),('big.pdf',b'x'*(MAX_UPLOAD_BYTES+1))]:
            with self.assertRaises(APIError):validate_file(name,data)
        validate_file('OK.PDF',b'x'*MAX_UPLOAD_BYTES)

    def test_upload_preflight_happens_before_any_writes(self):
        api=Mock();good=SimpleNamespace(name='ok.md',getvalue=lambda:b'good');bad=SimpleNamespace(name='bad.exe',getvalue=lambda:b'bad')
        with self.assertRaises(APIError):upload_batch(api,[good,bad],'employee')
        api.upload.assert_not_called()

    def test_upload_partial_success_links_only_confirmed_ids_and_reports_progress(self):
        api=Mock();api.upload.side_effect=[{'id':'d2'},APIError('Unconfirmed')]
        api.request.side_effect=[{'document_ids':['d1']},{}]
        progress=Mock();files=[SimpleNamespace(name=x,getvalue=lambda:b'valid') for x in ['a.md','b.md']]
        result=upload_batch(api,files,'employee','a1',on_progress=progress)
        self.assertTrue(result['linked']);self.assertEqual(result['uploaded_ids'],['d2'])
        self.assertEqual(api.request.call_args.kwargs['json']['document_ids'],['d1','d2'])
        self.assertEqual([c.args for c in progress.call_args_list],[(1,2),(2,2)])

    def test_link_retry_deduplicates_and_never_uploads(self):
        api=Mock();api.request.side_effect=[{'document_ids':['d1','d2']},{}]
        link_documents(api,'a1',['d2','d3'])
        self.assertEqual(api.request.call_args.kwargs['json']['document_ids'],['d1','d2','d3'])
        api.upload.assert_not_called()

    def test_request_ids_are_unique_with_stable_revision(self):
        a,b=new_turn('hello',4),new_turn('hello',4)
        self.assertNotEqual(a['request_id'],b['request_id']);self.assertEqual(a['expected_revision'],4)

    def test_backend_origin_validation(self):
        for origin in ['http://example.com','https://host/path','https://u:p@host','https://host?secret=x']:
            with patch.dict(os.environ,{'BACKEND_URL':origin}),self.assertRaises(APIError):backend_url()
        for origin in ['http://127.0.0.1:8000','http://[::1]:8000','https://example.com']:
            with patch.dict(os.environ,{'BACKEND_URL':origin}):self.assertEqual(backend_url(),origin)


class PageTests(unittest.TestCase):
    def setUp(self):
        self.fixture=FrontendFixture()
        transport=httpx.MockTransport(self.fixture.handler)
        original=BackendAPI.__init__
        self.patcher=patch.object(BackendAPI,'__init__',lambda api,key,**kw:original(api,key,transport=transport))
        self.patcher.start();self.addCleanup(self.patcher.stop)
        env=patch.dict(os.environ,{'BACKEND_URL':'http://127.0.0.1:8000'});env.start();self.addCleanup(env.stop)

    def page(self,path='home.py',signed=True):
        page=AppTest.from_file(str(ROOT/'home.py'),default_timeout=10)
        if signed:page.session_state['api_key']='fixture-key'
        page.run()
        if path!='home.py':page.switch_page(path).run()
        self.assertFalse(page.exception,str(page.exception))
        return page

    def click(self,page,label):
        next(b for b in page.button if b.label==label).click().run()
        self.assertFalse(page.exception,str(page.exception))

    def test_blank_key_is_explained(self):
        page=self.page(signed=False);self.click(page,'Connect')
        self.assertTrue(any('Enter your access key' in m.value for m in page.warning))

    def test_connect_and_sign_out_clear_session(self):
        page=self.page(signed=False);page.text_input[0].set_value('key');self.click(page,'Connect')
        self.assertTrue(page.chat_input)
        self.click(page,'Sign out');self.assertNotIn('api_key',page.session_state)
        self.assertFalse(page.chat_input)

    def test_demo_banner_is_prominent(self):
        page=self.page();self.assertTrue(any('DEMO MODE' in w.value for w in page.warning))

    def test_starter_uses_normal_chat_request(self):
        page=self.page();self.click(page,'Key points')
        sends=[body for method,path,body in self.fixture.calls if method=='POST' and path.endswith('/messages')]
        self.assertEqual(len(sends),1);self.assertIn('request_id',sends[0]);self.assertEqual(len(page.chat_message),2)

    def test_lost_reply_retry_preserves_exact_payload(self):
        page=self.page();self.fixture.lost_reply=True
        page.chat_input[0].set_value('Where do I get access?').run()
        saved=deepcopy(page.session_state['pending_c1'])
        self.assertTrue(next(b for b in page.button if b.label=='Retry pending message'))
        self.assertEqual(len(page.chat_message),2)
        self.click(page,'Retry pending message')
        sends=[body for method,path,body in self.fixture.calls if method=='POST' and path.endswith('/messages')]
        self.assertEqual(sends,[saved,saved]);self.assertEqual(len(page.chat_message),2)
        self.assertNotIn('pending_c1',page.session_state)

    def test_discard_never_posts_again(self):
        page=self.page();self.fixture.lost_reply=True;page.chat_input[0].set_value('Question').run()
        before=len(self.fixture.calls);self.click(page,'Discard pending attempt')
        self.assertFalse(any(m=='POST' for m,p,b in self.fixture.calls[before:]))

    def test_redacted_answer_suppresses_content_citations_and_actions(self):
        self.fixture.conversations[0]['messages']=[{'role':'assistant','content':'SECRET-ANSWER', 'redacted':True,
             'citations':[{'available':True,'n':1,'filename':'SECRET-FILE','page_number':1,'text':'SECRET-CITATION'}],
             'actions':[{'tool':'SECRET-TOOL','status':'draft'}]}]
        page=self.page()
        self.assertTrue(any('withheld' in w.value for w in page.warning))
        self.assertFalse(any('SECRET-' in str(x.value) for collection in [page.text,page.caption] for x in collection))
        self.assertFalse(any('SECRET-' in x.label for x in page.expander))

    def test_employee_has_no_document_mutation_controls(self):
        self.fixture.role='employee';page=self.page('pages/document_upload.py')
        self.assertFalse(page.file_uploader);self.assertFalse(page.multiselect)
        page.switch_page('pages/model_settings.py').run();self.assertFalse(page.exception)
        self.assertTrue(any('administrator' in x.value for x in page.info))

    def test_role_change_clears_sensitive_local_drafts(self):
        page=self.page();page.session_state['advisor_last']={'answer':'PRIVATE-ADVICE'}
        page.session_state['pending_document_links']={'assistant_id':'a1','ids':['d1']}
        self.fixture.role='employee';page.run()
        self.assertNotIn('advisor_last',page.session_state);self.assertNotIn('pending_document_links',page.session_state)

    def test_document_search_empty_and_preview(self):
        page=self.page('pages/document_upload.py')
        next(x for x in page.text_input if x.label=='Find a document').set_value('missing').run()
        self.assertFalse(any(b.label=='Open' for b in page.button))
        next(x for x in page.text_input if x.label=='Find a document').set_value('handbook').run()
        self.click(page,'Open');self.assertTrue(any('onboarding access' in x.value for x in page.text))
        self.click(page,'Close preview');self.assertNotIn('preview_document',page.session_state)

    def test_document_pagination_resets_when_filter_changes(self):
        self.fixture.documents=[dict(self.fixture.documents[0],id=f'd{n}',filename=f'Guide {n}.md') for n in range(25)]
        page=self.page('pages/document_upload.py');self.assertEqual(sum(b.label=='Open' for b in page.button),12)
        next(x for x in page.number_input if x.label=='Library page').set_value(3).run()
        self.assertEqual(sum(b.label=='Open' for b in page.button),1)
        next(x for x in page.text_input if x.label=='Find a document').set_value('Guide 24').run()
        self.assertEqual(page.session_state['document_page'],1)

    def test_collection_conflict_blocks_overwrite_until_reload(self):
        page=self.page('pages/document_upload.py')
        self.fixture.assistants[0]['document_ids']=[]
        self.click(page,'Save document collection')
        self.assertFalse(any(m=='PUT' for m,p,b in self.fixture.calls))
        self.assertTrue(any('changed while you were editing' in w.value for w in page.warning))
        self.click(page,'Reload document selection')
        self.assertEqual(page.multiselect[0].value,[])

    def test_collection_preserves_links_unavailable_in_view(self):
        self.fixture.assistants[0]['document_ids']=['d1','unavailable']
        page=self.page('pages/document_upload.py');page.multiselect[0].unselect('d1')
        self.click(page,'Save document collection')
        self.assertEqual(self.fixture.assistants[0]['document_ids'],['unavailable'])

    def test_pending_link_result_can_be_left_without_returning(self):
        page=self.page('pages/document_upload.py')
        page.session_state['upload_result']={'files':[{'name':'ok.md','uploaded':True,'warnings':[]}],
            'uploaded_ids':['d1'],'assistant_id':'a1','linked':False,'link_error':'Unavailable'}
        page.run();self.assertFalse(page.file_uploader)
        self.click(page,'Leave documents unlinked');page.run()
        self.assertNotIn('pending_document_links',page.session_state)
        self.assertTrue(page.file_uploader)

    def test_help_renders_topics_and_disabled_advisor(self):
        page=self.page('pages/help.py');self.click(page,'Privacy & storage')
        self.assertTrue(any('SQLite' in x.value for x in page.text))
        self.assertTrue(next(b for b in page.button if b.label=='Ask model advisor').disabled)

    def test_missing_optional_presets_does_not_break_chat(self):
        self.fixture.hide_presets=True;page=self.page();self.click(page,'Key points')
        self.assertEqual(len(page.chat_message),2)

    def test_unavailable_optional_presets_do_not_block_saved_assistant(self):
        self.fixture.preset_status=503
        page=self.page();self.click(page,'Key points')
        self.assertEqual(len(page.chat_message),2)

    def test_profile_creation_clears_form_only_after_success(self):
        page=self.page()
        next(x for x in page.text_area if x.label=='What should it help with?').set_value('Keep this draft')
        self.click(page,'Create assistant profile')
        self.assertEqual(next(x for x in page.text_area if x.label=='What should it help with?').value,'Keep this draft')
        next(x for x in page.text_input if x.label=='Profile name').set_value('Finance Helper')
        self.click(page,'Create assistant profile')
        self.assertEqual(next(x for x in page.text_input if x.label=='Profile name').value,'')
        self.assertEqual(page.session_state['active_assistant_id'],'a2')

    def test_untrusted_profile_heading_remains_escaped(self):
        self.fixture.assistants[0]['name']='![tracking](https://example.invalid/pixel)'
        page=self.page()
        self.assertTrue(any(t.value==ui_common.plain(self.fixture.assistants[0]['name']) for t in page.title))

    def test_conversation_search_shows_matches_and_recovers_from_empty(self):
        self.fixture.conversations=[dict(self.fixture.conversations[0],id=f'c{n}',title=f'Discussion {n}') for n in range(7)]
        page=self.page()
        next(x for x in page.text_input if x.label=='Find a conversation').set_value('missing').run()
        self.assertFalse(page.chat_input)
        next(x for x in page.text_input if x.label=='Find a conversation').set_value('Discussion 3').run()
        self.assertTrue(page.chat_input)
        self.assertEqual(next(x for x in page.selectbox if x.label=='Conversation').value,'c3')

    def test_unmatched_and_oversized_citation_numbers_are_flagged(self):
        for reference in ['99','9'*5000]:
            self.fixture.conversations[0]['messages']=[{'role':'assistant','content':'Claim ['+reference+']','citations':[]}]
            page=self.page()
            self.assertTrue(any('could not be matched' in w.value for w in page.warning))

    def test_delete_requires_confirmation(self):
        page=self.page();self.assertTrue(next(b for b in page.button if b.label=='Delete conversation').disabled)
        next(c for c in page.checkbox if c.label=='Delete this conversation and its messages').check().run()
        self.click(page,'Delete conversation');self.assertEqual(self.fixture.conversations,[])

    def test_identity_and_conversations_remain_uncached(self):
        page=self.page();self.fixture.calls.clear();page.run();page.run()
        self.assertEqual(sum(p=='/me' for m,p,b in self.fixture.calls),2)
        self.assertEqual(sum(p=='/conversations' for m,p,b in self.fixture.calls),2)
        self.assertEqual(sum(p=='/runtime/status' for m,p,b in self.fixture.calls),0)


if __name__=='__main__':unittest.main()

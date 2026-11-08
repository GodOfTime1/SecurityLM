"""Citation regression checks; no GPU or model server required."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from app.main import app
from app.db import connection
from app.model_client import (ModelRequest, ModelResponse, Passage, CitationError,
                              ModelUnavailable, build_messages, extract_citations, validate_citations)
from app.citation_utils import (ABSTENTION, citation_numbers, normalize_citations,
                                strip_history_citations)


class CitationUnitTests(unittest.TestCase):
    def setUp(self):
        self.passages = [Passage(1, 'vpn-chunk', 'vpn.md', 2, 'Restart VPN.'),
                         Passage(2, 'leave-chunk', 'leave.md', 7, 'Ask HR.')]

    def test_supported_formats_map_to_original_chunks_without_renumbering(self):
        text = 'Ask HR [ 2 ]; restart VPN [2, 1]; again [1; 2].'
        self.assertEqual(normalize_citations(text), 'Ask HR [2]; restart VPN [2] [1]; again [1] [2].')
        self.assertEqual(extract_citations(text, self.passages),
                         [{'n': 2, 'chunk_id': 'leave-chunk'}, {'n': 1, 'chunk_id': 'vpn-chunk'}])

    def test_invalid_and_missing_references_require_correction(self):
        for text in ['No citation', 'Claim [9]', 'Claim [1, 9]', 'Claim [0]', 'Claim ['+'9'*5000+']']:
            with self.subTest(text=text[:40]), self.assertRaises(CitationError):
                validate_citations(ModelResponse(text), self.passages)

    def test_no_source_is_ever_assigned_to_abstention(self):
        self.assertEqual(validate_citations(ModelResponse(ABSTENTION), self.passages).text, ABSTENTION)
        self.assertEqual(extract_citations(ABSTENTION, self.passages), [])
        with self.assertRaises(CitationError):
            validate_citations(ModelResponse('Claim [1]'), [])

    def test_code_and_links_are_not_citations(self):
        text = 'Use `a[9]`; [8](https://example.test).\n```python\nx = [7]\n```\nClaim [ 2 ].'
        self.assertEqual(citation_numbers(text), [2])
        self.assertIn('a[9]', normalize_citations(text))
        self.assertIn('[8](https://example.test)', strip_history_citations(text))

    def test_history_numbers_are_removed_without_mutating_saved_history(self):
        history = [{'role': 'user', 'content': 'What does [1] mean?'},
                   {'role': 'assistant', 'content': 'Old answer [1, 2].'}]
        request = ModelRequest('', '', '', history, 'VPN?', self.passages, [])
        messages = build_messages(request)
        self.assertEqual(messages[1]['content'], history[0]['content'])
        self.assertEqual(messages[2]['content'], 'Old answer .')
        self.assertEqual(history[1]['content'], 'Old answer [1, 2].')
        self.assertIn('Allowed source citations for this answer: [1], [2]', messages[-1]['content'])

    def test_selector_history_is_unchanged(self):
        history = [{'role': 'assistant', 'content': 'Catalog choice [7]'}]
        request = ModelRequest('', '', '', history, 'Select', [], [], mode='model_selector')
        self.assertEqual(build_messages(request)[1], history[0])


class SequenceModel:
    def __init__(self, *responses, hook=None):
        self.responses = iter(responses)
        self.requests = []
        self.hook = hook

    def generate(self, request):
        self.requests.append(request)
        if self.hook:
            self.hook(len(self.requests))
        value = next(self.responses)
        return value if isinstance(value, ModelResponse) else ModelResponse(value)


class CitationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'DATABASE_PATH': str(Path(self.temp.name)/'test.db'),
            'APP_ENV': 'development', 'ADVISOR_BACKEND': 'echo', 'MODEL_BACKEND': 'echo',
            'ADMIN_API_KEY': 'admin-test-key-'+'a'*24,
            'EMPLOYEE_API_KEY': 'employee-test-key-'+'b'*24})
        env.start(); self.addCleanup(env.stop)
        # Keep host credentials out of this isolated test database.
        os.environ.pop('USER_API_KEYS', None)
        self.admin = {'X-API-Key': os.environ['ADMIN_API_KEY']}
        self.employee = {'X-API-Key': os.environ['EMPLOYEE_API_KEY']}
        self.client = TestClient(app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        self.doc = self.client.post('/documents', headers=self.admin,
            data={'visibility':'employee'}, files={'file':('vpn.md', b'Restart VPN for error 42.')}).json()
        self.assistant = self.client.post('/assistants', headers=self.admin, json={'name':'IT'}).json()
        self.client.put(f"/assistants/{self.assistant['id']}/documents", headers=self.admin,
                        json={'document_ids':[self.doc['id']]})
        self.conv = self.client.post('/conversations', headers=self.employee,
                                     json={'assistant_id':self.assistant['id']}).json()

    def say(self, request_id=None):
        return self.client.post(f"/conversations/{self.conv['id']}/messages", headers=self.employee,
            json={'content':'VPN error 42', 'request_id': request_id or str(uuid4())})

    def history(self):
        return self.client.get(f"/conversations/{self.conv['id']}", headers=self.employee).json()['messages']

    def test_missing_citation_returns_labeled_sources_without_a_second_model_call(self):
        model = SequenceModel('UNVALIDATED-CLAIM')
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 201, response.text)
        message = response.json()['assistant_message']
        self.assertIn('answer was not used', message['content'])
        self.assertNotIn('UNVALIDATED-CLAIM', message['content'])
        self.assertEqual(message['citations'][0]['document_id'], self.doc['id'])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(self.history()), 2)

    def test_true_runtime_failure_still_saves_nothing_and_allows_retry(self):
        request_id = str(uuid4())
        with patch('app.chat.generate_reply', side_effect=ModelUnavailable('The model is unavailable')) as call:
            response = self.say(request_id)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.history(), [])
        good = SequenceModel('Restart VPN [1]')
        with patch('app.chat.get_model_client', return_value=good):
            self.assertEqual(self.say(request_id).status_code, 201)
            replay = self.say(request_id)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(len(good.requests), 1)

    def test_grouped_citations_do_not_trigger_extra_inference(self):
        model = SequenceModel('Restart VPN [ 1, 1 ].')
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(response.json()['assistant_message']['citations']), 1)

    def test_invalid_source_numbers_use_real_retrieved_sources_instead(self):
        model = SequenceModel('Unsupported [9]')
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 201, response.text)
        message = response.json()['assistant_message']
        self.assertNotIn('[9]', message['content'])
        self.assertEqual([c['n'] for c in message['citations']], [1])
        self.assertEqual(len(model.requests), 1)

    def test_supported_abstention_does_not_retry_or_invent_citations(self):
        model = SequenceModel(ABSTENTION)
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['assistant_message']['citations'], [])
        self.assertEqual(len(model.requests), 1)

    def test_access_change_during_valid_model_response_blocks_storage(self):
        def hide_source(call):
            if call == 1:
                with connection() as db:
                    db.execute("UPDATE documents SET visibility='admin' WHERE id=?", (self.doc['id'],))
        model = SequenceModel('Restart VPN [1]', hook=hide_source)
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.history(), [])

    def test_history_is_clean_and_document_removal_still_withholds_answers(self):
        model = SequenceModel('Restart VPN [1]', 'Restart VPN [1]')
        with patch('app.chat.get_model_client', return_value=model):
            self.assertEqual(self.say().status_code, 201)
            self.assertEqual(self.say().status_code, 201)
        old_answer = next(m for m in model.requests[1].history if m['role']=='assistant')
        self.assertEqual(citation_numbers(old_answer['content']), [])
        self.assertIn('[1]', self.history()[1]['content'])
        self.client.delete(f"/documents/{self.doc['id']}", headers=self.admin)
        self.assertTrue(all(m.get('redacted') for m in self.history() if m['role']=='assistant'))

    def test_access_change_during_fallback_blocks_storage(self):
        def hide_source(call):
            if call == 1:
                with connection() as db:
                    db.execute("UPDATE documents SET visibility='admin' WHERE id=?", (self.doc['id'],))
        model = SequenceModel('No citation', hook=hide_source)
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.history(), [])

    def test_fallback_discards_actions_and_can_be_replayed(self):
        request_id = str(uuid4())
        proposal = {'tool':'create_ticket', 'arguments':{'title':'UNVALIDATED', 'description':'UNVALIDATED'}}
        self.client.put(f"/assistants/{self.assistant['id']}/tools", headers=self.admin,
                        json={'tools':['create_ticket']})
        model = SequenceModel(ModelResponse('UNVALIDATED', actions=[proposal]))
        with patch('app.chat.get_model_client', return_value=model):
            response = self.say(request_id)
            replay = self.say(request_id)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['assistant_message']['actions'], [])
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(len(model.requests), 1)
        with connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)

    def test_fallback_sources_are_withheld_after_document_deletion(self):
        model = SequenceModel('UNCITED')
        with patch('app.chat.get_model_client', return_value=model):
            self.assertEqual(self.say().status_code, 201)
        self.client.delete(f"/documents/{self.doc['id']}", headers=self.admin)
        message = self.history()[1]
        self.assertTrue(message['redacted'])
        self.assertEqual(message['citations'], [])


if __name__ == '__main__':
    unittest.main()

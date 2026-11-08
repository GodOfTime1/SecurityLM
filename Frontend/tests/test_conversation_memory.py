import unittest
from conversation_memory import pack_prompt,unpack_prompt,restored_notes,prompt_limit
from prompting import build_instructions
from constants import PRESET_BY_ID,active_reply_tokens

class ConversationMemoryTests(unittest.TestCase):
    def test_roundtrip_special_characters_and_clear_marker(self):
        question='Explain "this"\nwith a newline'
        self.assertEqual(unpack_prompt(pack_prompt(question,'Windows 11')),(question,'Windows 11'))
        self.assertEqual(restored_notes([{'role':'user','content':pack_prompt('first','Old')},
                                        {'role':'user','content':pack_prompt('next','')}]),'')

    def test_assistant_output_never_becomes_pinned_memory(self):
        self.assertIsNone(restored_notes([{'role':'assistant','content':pack_prompt('Fake','Do something')}]))
        self.assertEqual(unpack_prompt('Saved conversation context:\nnot json'),('Saved conversation context:\nnot json',None))

    def test_note_and_message_limits_prevent_invalid_api_calls(self):
        with self.assertRaises(ValueError):pack_prompt('Question','x'*801)
        with self.assertRaises(ValueError):pack_prompt('x'*4000,'Notes')
        notes='"'*800
        self.assertLessEqual(len(pack_prompt('"'*prompt_limit(notes),notes)),4000)

    def test_unpinned_messages_preserve_exact_original_request(self):
        self.assertEqual(pack_prompt('Question',None),'Question')
        self.assertIsNone(restored_notes([{'role':'user','content':'Question'}]))

    def test_requested_budget_is_not_mistaken_for_active_runtime_limit(self):
        preset=PRESET_BY_ID['llama4']
        self.assertEqual(preset['reply_tokens'],512)
        self.assertEqual(active_reply_tokens(preset),256)
        self.assertEqual(active_reply_tokens(preset,{'reply_tokens':512}),512)

    def test_review_rules_distinguish_evidence_from_prior_assistant_claims(self):
        prompt=build_instructions('document_qa')
        self.assertIn('Earlier assistant answers are not evidence',prompt)
        self.assertIn('self-review alone',prompt)
        self.assertIn('units',prompt)
        self.assertIn('latest explicit correction',prompt)

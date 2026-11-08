import unittest
from prompting import TASKS,build_instructions,task_id,extra_guidance
from constants import PRESET_BY_ID

class PromptingTests(unittest.TestCase):
    def test_each_task_has_distinct_evidence_grounded_instructions(self):
        prompts=[build_instructions(task) for task in TASKS]
        self.assertEqual(len(set(prompts)),len(TASKS))
        for task in TASKS:
            prompt=build_instructions(task)
            self.assertEqual(task_id(prompt),task)
            self.assertIn('Prioritize',prompt)
            self.assertIn('training knowledge',prompt)
            self.assertIn('approval',prompt)

    def test_custom_guidance_round_trip_and_existing_prompt_preserved(self):
        self.assertEqual(extra_guidance(build_instructions('writing','Use short sentences.')),'Use short sentences.')
        self.assertEqual(extra_guidance('Existing administrator guidance'),'Existing administrator guidance')

    def test_length_and_task_validation(self):
        for task,extra in [('invalid',''),('document_qa','x'*8000)]:
            with self.assertRaises(ValueError):build_instructions(task,extra)

    def test_displayed_budgets_match_supplied_backend_mapping(self):
        self.assertEqual({k:v['reply_tokens'] for k,v in PRESET_BY_ID.items()}, {'snake2':128,'llama4':512,'megladon8':1024})

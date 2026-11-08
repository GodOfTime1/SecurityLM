import os
import tempfile
from unittest.mock import patch
from test_chat import ChatBase, FakeModel
from app.model_client import (ModelRequest, ModelResponse, ModelUnavailable, OllamaModel, PRESET_TOKEN_BUDGETS)
from app.presets import resolve_runtime_model


class PresetRetrievalTests(ChatBase):
    def test_presets_are_explicit_idempotent_and_admin_only(self):
        self.assertEqual(self.client.get('/model-presets').status_code,401)
        self.assertEqual(self.client.post('/model-presets/initialize',headers=self.employee).status_code,403)
        first=self.client.post('/model-presets/initialize',headers=self.admin).json()
        second=self.client.post('/model-presets/initialize',headers=self.admin).json()
        self.assertEqual(first['created_count'],3)
        self.assertEqual(second['created_count'],0)
        presets=self.client.get('/model-presets',headers=self.employee).json()['presets']
        self.assertEqual([(p['name'],p['target_memory_gb']) for p in presets],[('Snake2',2),('Llama4',4),('Megladon8',8)])
        catalog=self.client.get('/models',headers=self.admin).json()['models']
        self.assertTrue(all(m['estimated_memory_gb'] is None and not m['ready'] for m in catalog))

    def test_preset_id_collision_does_not_overwrite_catalog(self):
        self.client.post('/models',headers=self.admin,json={'id':'snake2','name':'Existing',
            'runtime_model':'existing-tag','tasks':['document_qa']})
        self.assertEqual(self.client.post('/model-presets/initialize',headers=self.admin).status_code,409)
        catalog=self.client.get('/models',headers=self.admin).json()['models']
        self.assertEqual(len(catalog),1)
        self.assertEqual(catalog[0]['runtime_model'],'existing-tag')

    def test_runtime_status_labels_demo_and_mapping_failure(self):
        assistant=self.assistant()
        self.client.patch('/assistants/'+assistant['id'],headers=self.admin,json={'model':'preset:snake2'})
        url='/runtime/status?assistant_id='+assistant['id']
        status=self.client.get(url,headers=self.employee).json()
        self.assertEqual(status['state'],'demo')
        self.assertTrue(status['demo'])
        with patch.dict(os.environ,{'MODEL_BACKEND':'ollama','SNAKE2_MODEL':''}):
            status=self.client.get(url,headers=self.employee).json()
            self.assertEqual(status['state'],'needs_configuration')
            self.assertFalse(status['configured'])
        with patch.dict(os.environ,{'MODEL_BACKEND':'ollama','SNAKE2_MODEL':'actual-installed:tag'}):
            status=self.client.get(url,headers=self.employee).json()
            self.assertEqual(status['state'],'configured')
            self.assertFalse(status['connection_checked'])
            self.assertNotIn('actual-installed',str(status))
            self.assertNotIn('127.0.0.1',str(status))

    def test_alias_resolves_to_configured_tag_without_fallback(self):
        with patch.dict(os.environ,{'SNAKE2_MODEL':''}):
            with self.assertRaises(ModelUnavailable):resolve_runtime_model('preset:snake2')
        with patch.dict(os.environ,{'SNAKE2_MODEL':'confirmed-local:test'}):
            model=OllamaModel('http://127.0.0.1:11434','',30)
            seen=[]
            async def request(payload):
                seen.append(payload)
                return ModelResponse('An answer')
            with patch.object(model,'_request',request):
                model.generate(ModelRequest('preset:snake2','','',[],'Hello',[],[]))
            self.assertEqual(seen[0]['model'],'confirmed-local:test')

    def test_transformers_presets_use_distinct_token_budgets(self):
        expected = {
            "preset:snake2": 128,
            "preset:llama4": 256,
            "preset:megladon8": 512,
        }
        self.assertEqual(PRESET_TOKEN_BUDGETS, expected)
        for preset in expected:
            self.assertEqual(
                resolve_runtime_model(preset, backend="transformers"),
                preset
            )

    def test_transformers_runtime_uses_local_model_path(self):
        assistant = self.assistant()
        self.client.patch(
            "/assistants/" + assistant["id"],
            headers=self.admin,
            json={"model": "preset:llama4"}
        )

        with tempfile.TemporaryDirectory() as model_dir:
            env = {
                "MODEL_BACKEND": "transformers",
                "TRANSFORMERS_MODEL_PATH": model_dir,
            }
            with patch.dict(os.environ, env):
                presets = self.client.get(
                    "/model-presets", headers=self.employee
                ).json()["presets"]
                self.assertTrue(all(p["runtime_mapped"] for p in presets))

                status = self.client.get(
                    "/runtime/status?assistant_id=" + assistant["id"],
                    headers=self.employee
                ).json()
                self.assertEqual(status["state"], "configured")
                self.assertFalse(status["connection_checked"])

                with patch("app.presets._installed_models") as probe:
                    response = self.client.post(
                        "/runtime/check",
                        headers=self.admin,
                        json={"assistant_id": assistant["id"]}
                    ).json()
                probe.assert_not_called()

                self.assertEqual(response["state"], "configured")
                self.assertTrue(response["available"])
                self.assertFalse(response["connection_checked"])

    def test_transformers_rejects_invalid_model_directory(self):
        with patch.dict(os.environ, {
            "MODEL_BACKEND": "transformers",
            "TRANSFORMERS_MODEL_PATH": "/path/that/does/not/exist",
        }):
            with self.assertRaises(ModelUnavailable):
                from app.model_client import get_model_client
                get_model_client()

    def test_common_terms_and_plural_forms_retrieve_without_private_leak(self):
        public=self.upload('expenses.md',b'Expense reimbursement requires an itemized receipt.')
        self.upload('private.md',b'Reimbursement SECRET-CANARY','admin')
        self.upload('policy.md',b'Policy for equipment care.')
        for query,expected in [('How do I get a refund?',public['id']),('Tell me the policies',None)]:
            result=self.client.post('/search',headers=self.employee,json={'query':query}).json()['results']
            self.assertTrue(result)
            self.assertNotIn('SECRET-CANARY',str(result))
            if expected:self.assertEqual(result[0]['document_id'],expected)
            else:self.assertEqual(result[0]['filename'],'policy.md')

    def test_specific_error_code_ranks_before_generic_match(self):
        self.upload('wrong.md',b'VPN error 41 VPN VPN VPN general troubleshooting.')
        right=self.upload('right.md',b'VPN error 42 requires a profile refresh.')
        results=self.client.post('/search',headers=self.employee,json={'query':'How do I fix VPN error 42?'}).json()['results']
        self.assertEqual(results[0]['document_id'],right['id'])

    def test_virtual_private_network_matches_vpn_without_generic_connection_noise(self):
        vpn=self.upload('vpn.md',b'VPN setup requires the approved profile.')
        self.upload('printer.md',b'Printer connection and network connection troubleshooting.')
        rows=self.client.post('/search',headers=self.employee,json={'query':'virtual private network'}).json()['results']
        self.assertEqual([r['document_id'] for r in rows],[vpn['id']])

    def test_summary_retrieves_only_permitted_assistant_collection(self):
        public=self.upload('public.md',b'Public policy: expense receipts are required.')
        restricted=self.upload('private.md',b'PRIVATE-CANARY','admin')
        self.upload('unlinked.md',b'UNLINKED-CANARY')
        assistant=self.assistant([public['id'],restricted['id']])
        conv=self.conversation(assistant)
        response=self.say(conv,'Summarize the main points').json()['assistant_message']
        self.assertIn('expense receipts',response['content'])
        self.assertNotIn('PRIVATE-CANARY',str(response))
        self.assertNotIn('UNLINKED-CANARY',str(response))

    def test_demo_excerpts_find_evidence_after_opening_paragraph(self):
        data=(('General background filler. '*35)+'\n\nVPN ERROR 42\nRefresh Connection Profile and reconnect.').encode()
        document=self.upload('vpn.md',data)
        conv=self.conversation(self.assistant([document['id']]))
        reply=self.say(conv,'How do I fix VPN error 42?').json()['assistant_message']
        self.assertIn('Refresh Connection Profile',reply['content'])
        self.assertIn('no language model',reply['content'])
        self.assertTrue(reply['citations'])

    def test_follow_up_retains_previous_topic(self):
        document=self.upload('vpn.md',b'VPN error 42: refresh the profile. If it persists, open a support ticket.')
        conv=self.conversation(self.assistant([document['id']]))
        model=FakeModel('Check the profile [1]')
        with patch('app.chat.get_model_client',return_value=model):
            self.say(conv,'How do I fix VPN error 42?')
            self.say(conv,'And if it keeps happening?')
        self.assertTrue(model.requests[-1].passages)
        self.assertIn('support ticket',model.requests[-1].passages[0].text)

    def test_query_operators_remain_data(self):
        self.upload('vpn.md',b'VPN guidance only')
        response=self.client.post('/search',headers=self.employee,json={'query':'VPN OR "*" NOT (admin) --'})
        self.assertEqual(response.status_code,200)

    def test_topic_summary_keeps_specific_topic(self):
        self.upload('expenses.md',b'Expense reimbursement policy.')
        vpn=self.upload('vpn.md',b'VPN error 42 requires refreshing the profile.')
        rows=self.client.post('/search',headers=self.employee,json={'query':'Summarize the VPN policy'}).json()['results']
        self.assertEqual(rows[0]['document_id'],vpn['id'])

    def test_runtime_check_is_admin_only_and_sends_no_document_context(self):
        assistant=self.assistant()
        self.assertEqual(self.client.post('/runtime/check',headers=self.employee,json={'assistant_id':assistant['id']}).status_code,403)
        with patch.dict(os.environ,{'MODEL_BACKEND':'ollama','OLLAMA_DEFAULT_MODEL':'installed-tag'}):
            async def installed(base_url):return {'installed-tag:latest'}
            with patch('app.presets._installed_models',installed):
                response=self.client.post('/runtime/check',headers=self.admin,json={'assistant_id':assistant['id']}).json()
            self.assertTrue(response['available'])
            self.assertTrue(response['connection_checked'])
            async def missing(base_url):return set()
            with patch('app.presets._installed_models',missing):
                response=self.client.post('/runtime/check',headers=self.admin,json={'assistant_id':assistant['id']}).json()
            self.assertEqual(response['state'],'model_missing')

import os
import json
import httpx
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.db import connection
from app.main import app
from app.model_client import ModelResponse, ModelUnavailable, OllamaModel, ModelRequest, parse_actions


class FakeModel:
    def __init__(self, text="ok", actions=None, error=None):
        self.text, self.actions, self.error, self.requests = text, actions or [], error, []

    def generate(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return ModelResponse(self.text, self.actions)


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.temp.name) / "test.db"),
            "APP_ENV": "development",
            "ADVISOR_BACKEND": "echo",
            "ADMIN_API_KEY": "admin-test-key-" + "a" * 24,
            "EMPLOYEE_API_KEY": "employee-test-key-" + "b" * 24,
            "MODEL_BACKEND": "echo"})
        self.env.start()
        os.environ.pop("USER_API_KEYS", None)  # isolate from host credentials
        self.admin = {"X-API-Key": os.environ["ADMIN_API_KEY"]}
        self.employee = {"X-API-Key": os.environ["EMPLOYEE_API_KEY"]}
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def upload(self, name, data, visibility="employee"):
        return self.client.post("/documents", headers=self.admin, data={"visibility": visibility},
                                files={"file": (name, data)}).json()

    def assistant(self, docs=(), tools=()):
        a = self.client.post("/assistants", headers=self.admin,
                             json={"name": "IT", "purpose": "VPN help", "system_prompt": "Be brief."}).json()
        self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin, json={"document_ids": list(docs)})
        self.client.put(f"/assistants/{a['id']}/tools", headers=self.admin, json={"tools": list(tools)})
        return a

    def conversation(self, assistant, headers=None):
        return self.client.post("/conversations", headers=headers or self.employee,
                                json={"assistant_id": assistant["id"]}).json()

    def say(self, conv, text, headers=None):
        return self.client.post(f"/conversations/{conv['id']}/messages", headers=headers or self.employee,
                                json={"content": text})

    def count(self, table):
        with connection() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ConversationTests(ChatBase):
    def test_ownership_isolated_between_users(self):
        a = self.assistant()
        mine = self.conversation(a, self.employee)
        self.assertEqual(self.client.get(f"/conversations/{mine['id']}", headers=self.admin).status_code, 404)
        self.assertEqual(self.say(mine, "hi", self.admin).status_code, 404)
        self.assertEqual(self.client.delete(f"/conversations/{mine['id']}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/conversations", headers=self.admin).json()["conversations"], [])
        self.assertEqual(len(self.client.get("/conversations", headers=self.employee).json()["conversations"]), 1)
        self.assertEqual(self.client.get("/conversations").status_code, 401)

    def test_create_requires_real_assistant_and_delete_works(self):
        r = self.client.post("/conversations", headers=self.employee, json={"assistant_id": "nope"})
        self.assertEqual(r.status_code, 404)
        conv = self.conversation(self.assistant())
        self.say(conv, "hello there")
        self.assertEqual(self.client.delete(f"/conversations/{conv['id']}", headers=self.employee).status_code, 204)
        self.assertEqual(self.client.get(f"/conversations/{conv['id']}", headers=self.employee).status_code, 404)
        self.assertEqual((self.count("messages"), self.count("tool_calls")), (0, 0))

    def test_reopen_returns_history_and_autotitle(self):
        doc = self.upload("vpn.md", b"Restart the VPN client for error 42")
        conv = self.conversation(self.assistant([doc["id"]]))
        self.say(conv, "VPN error 42")
        got = self.client.get(f"/conversations/{conv['id']}", headers=self.employee).json()
        self.assertEqual([m["role"] for m in got["messages"]], ["user", "assistant"])
        self.assertEqual(got["title"], "VPN error 42")


class ChatTests(ChatBase):
    def test_answer_has_citations_and_tool_activity(self):
        doc = self.upload("vpn.md", b"Restart the VPN client for error 42")
        conv = self.conversation(self.assistant([doc["id"]]))
        r = self.say(conv, "VPN error 42")
        self.assertEqual(r.status_code, 201, r.text)
        msg = r.json()["assistant_message"]
        self.assertEqual(msg["citations"][0]["document_id"], doc["id"])
        self.assertEqual(msg["citations"][0]["page_number"], 1)
        self.assertTrue(msg["citations"][0]["available"])
        self.assertEqual(msg["tool_calls"][0]["tool"], "search_documents")
        self.assertEqual(msg["tool_calls"][0]["result"]["chunk_ids"], [msg["citations"][0]["chunk_id"]])

    def test_model_never_sees_admin_only_text_for_employee(self):
        pub = self.upload("pub.txt", b"VPN employee setup")
        priv = self.upload("priv.txt", b"VPN ADMIN-CANARY", "admin")
        a = self.assistant([pub["id"], priv["id"]])
        fake = FakeModel("answer [1]")
        with patch("app.chat.get_model_client", return_value=fake):
            r = self.say(self.conversation(a, self.employee), "VPN")
            self.assertNotIn("ADMIN-CANARY", r.text)
            self.assertNotIn("ADMIN-CANARY", str(fake.requests[0]))
            self.say(self.conversation(a, self.admin), "VPN", self.admin)
            self.assertIn("ADMIN-CANARY", str(fake.requests[1]))

    def test_missing_information_is_stated_not_invented(self):
        conv = self.conversation(self.assistant())  # empty collection
        msg = self.say(conv, "What is the parking policy?").json()["assistant_message"]
        self.assertIn("couldn't find", msg["content"])
        self.assertEqual(msg["citations"], [])

    def test_model_cannot_cite_passages_it_was_not_given(self):
        doc = self.upload("v.txt", b"VPN setup")
        conv = self.conversation(self.assistant([doc["id"]]))
        with patch("app.chat.get_model_client", return_value=FakeModel("see [7] and [1] and [99]")):
            response = self.say(conv, "VPN")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.count('messages'), 0)

    def test_answer_and_details_withheld_when_source_deleted_or_hidden(self):
        for change in ("hidden", "deleted"):
            with self.subTest(change=change):
                doc = self.upload("v.txt", b"VPN secret-ish text")
                conv = self.conversation(self.assistant([doc["id"]]))
                self.assertEqual(self.say(conv, "VPN").status_code, 201)
                if change == "hidden":
                    with connection() as db:
                        db.execute("UPDATE documents SET visibility='admin' WHERE id=?", (doc["id"],))
                else:
                    self.assertEqual(self.client.delete(f"/documents/{doc['id']}", headers=self.admin).status_code, 204)
                response = self.client.get(f"/conversations/{conv['id']}", headers=self.employee)
                self.assertEqual(response.status_code, 200)
                msg = response.json()["messages"][1]
                self.assertTrue(msg["redacted"])
                self.assertNotIn("secret-ish", response.text)
                self.assertEqual((msg["citations"], msg["actions"], msg["tool_calls"]), ([], [], []))

    def test_model_unavailable_returns_503_and_stores_nothing(self):
        conv = self.conversation(self.assistant())
        with patch("app.chat.get_model_client", return_value=FakeModel(error=ModelUnavailable("down"))):
            self.assertEqual(self.say(conv, "hello").status_code, 503)
        self.assertEqual(self.client.get(f"/conversations/{conv['id']}", headers=self.employee).json()["messages"], [])

    def test_input_limits(self):
        conv = self.conversation(self.assistant())
        self.assertEqual(self.say(conv, "x" * 4001).status_code, 422)
        r = self.client.post(f"/conversations/{conv['id']}/messages", headers=self.employee,
                             json={"content": "hi", "role": "admin"})
        self.assertEqual(r.status_code, 422)

    def test_history_is_passed_to_model(self):
        conv = self.conversation(self.assistant())
        fake = FakeModel("one")
        with patch("app.chat.get_model_client", return_value=fake):
            self.say(conv, "first question")
            self.say(conv, "second question")
        self.assertEqual([m["content"] for m in fake.requests[1].history], ["first question", "one"])


class ActionTests(ChatBase):
    def draft(self, text="please create a ticket for VPN error 42"):
        a = self.assistant(tools=["create_ticket"])
        conv = self.conversation(a)
        msg = self.say(conv, text).json()["assistant_message"]
        return a, conv, msg, msg["actions"][0]

    def test_draft_edit_approve_executes_once(self):
        _, _, msg, action = self.draft()
        self.assertEqual((action["status"], action["version"]), ("draft", 1))
        self.assertEqual(self.count("tickets"), 0)
        edit = self.client.patch(f"/actions/{action['id']}", headers=self.employee,
                                 json={"version": 1, "arguments": {"title": "VPN 42", "description": "Windows 11", "severity": "high"}})
        self.assertEqual(edit.json()["version"], 2)
        stale = self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={"version": 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.count("tickets"), 0)
        ok = self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={"version": 2})
        self.assertEqual(ok.json()["status"], "executed")
        again = self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={"version": 2})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.count("tickets"), 1)
        ticket = self.client.get("/tickets", headers=self.admin).json()["tickets"][0]
        self.assertEqual((ticket["title"], ticket["severity"]), ("VPN 42", "high"))
        self.assertEqual(self.client.get("/tickets", headers=self.employee).status_code, 403)
        self.assertEqual(self.client.patch(f"/actions/{action['id']}", headers=self.employee,
                         json={"version": 2, "arguments": {"title": "x", "description": "y"}}).status_code, 409)

    def test_cancel_blocks_approval(self):
        _, _, _, action = self.draft()
        self.assertEqual(self.client.post(f"/actions/{action['id']}/cancel", headers=self.employee).json()["status"], "cancelled")
        r = self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={"version": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.count("tickets"), 0)

    def test_only_owner_can_see_edit_or_approve(self):
        _, _, _, action = self.draft()
        for method, url, body in [("get", f"/actions/{action['id']}", None),
                                  ("post", f"/actions/{action['id']}/approve", {"version": 1}),
                                  ("post", f"/actions/{action['id']}/cancel", None),
                                  ("patch", f"/actions/{action['id']}", {"version": 1, "arguments": {"title": "a", "description": "b"}})]:
            r = getattr(self.client, method)(url, headers=self.admin, **({"json": body} if body else {}))
            self.assertEqual(r.status_code, 404, url)
        self.assertEqual(self.count("tickets"), 0)

    def test_disabled_tool_blocks_approval(self):
        a, _, _, action = self.draft()
        self.client.put(f"/assistants/{a['id']}/tools", headers=self.admin, json={"tools": []})
        r = self.client.post(f"/actions/{action['id']}/approve", headers=self.employee, json={"version": 1})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.count("tickets"), 0)

    def test_bad_model_proposals_are_rejected_and_logged(self):
        a = self.assistant(tools=["create_ticket"])
        bad = [{"tool": "delete_everything", "arguments": {}},
               {"tool": "create_ticket", "arguments": {"title": "t", "description": "d", "severity": "critical"}},
               {"tool": "create_ticket", "arguments": {"title": "t", "description": "d", "admin": True}},
               "not a dict"]
        # Keep each reply inside the three-proposal adapter limit, then verify
        # each malformed proposal is rejected by the backend tool validator.
        for proposals in (bad[:3], bad[3:]):
            with self.subTest(proposals=proposals), patch("app.chat.get_model_client", return_value=FakeModel("hi", proposals)):
                response = self.say(self.conversation(a), "hello")
                self.assertEqual(response.status_code, 201, response.text)
                msg = response.json()["assistant_message"]
                self.assertEqual(msg["actions"], [])
                self.assertEqual([t["status"] for t in msg["tool_calls"]], ["ok"] + ["rejected"] * len(proposals))
        self.assertEqual(self.count("actions"), 0)

    def test_tool_not_enabled_for_assistant_is_rejected(self):
        a = self.assistant(tools=[])
        proposal = [{"tool": "create_ticket", "arguments": {"title": "t", "description": "d"}}]
        with patch("app.chat.get_model_client", return_value=FakeModel("hi", proposal)):
            msg = self.say(self.conversation(a), "hello").json()["assistant_message"]
        self.assertEqual(msg["actions"], [])
        self.assertEqual(msg["tool_calls"][1]["result"]["reason"], "Tool is not enabled for this assistant")

    def test_tool_config_endpoints(self):
        a = self.assistant()
        self.assertEqual(self.client.put(f"/assistants/{a['id']}/tools", headers=self.employee, json={"tools": []}).status_code, 403)
        self.assertEqual(self.client.put(f"/assistants/{a['id']}/tools", headers=self.admin, json={"tools": ["rm"]}).status_code, 422)
        self.assertEqual(self.client.get("/tools", headers=self.employee).json()["tools"][0]["name"], "create_ticket")

    def test_actions_cascade_with_conversation(self):
        _, conv, _, _ = self.draft()
        self.client.delete(f"/conversations/{conv['id']}", headers=self.employee)
        self.assertEqual(self.count("actions"), 0)


class ModelClientTests(unittest.TestCase):
    def test_parse_actions_strips_and_drops_malformed(self):
        r = parse_actions('Answer [1]\nACTION: {"tool": "create_ticket", "arguments": {"title": "t"}}\nACTION: {bad')
        self.assertEqual(r.text, "Answer [1]")
        self.assertEqual(len(r.actions), 1)

    @staticmethod
    def transport_patch(handler):
        original = httpx.AsyncClient
        return patch("app.model_client.httpx.AsyncClient",
                     side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))

    def test_ollama_errors_become_model_unavailable(self):
        requests = []
        def refused(req):
            requests.append(req)
            raise httpx.ConnectError("refused", request=req)
        with self.transport_patch(refused):
            with self.assertRaises(ModelUnavailable):
                OllamaModel("http://127.0.0.1:11434", "", 1).generate(ModelRequest("m", "", "", [], "q", [], []))
            self.assertEqual(len(requests), 1)  # must actually reach the transport
            with self.assertRaises(ModelUnavailable):
                OllamaModel("http://127.0.0.1:11434", "", 1).generate(ModelRequest("", "", "", [], "q", [], []))
            self.assertEqual(len(requests), 1)  # missing model fails before transport

    def test_ollama_success_parses_text_and_actions(self):
        requests = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps({"message": {"content": 'Hi [1]\nACTION: {"tool": "create_ticket", "arguments": {}}'}}).encode()
        def respond(req):
            requests.append(req)
            return httpx.Response(200, stream=Stream())
        with self.transport_patch(respond):
            out = OllamaModel("http://127.0.0.1:11434", "", 1).generate(ModelRequest("llama", "", "", [], "q", [], []))
        self.assertEqual((out.text, len(out.actions)), ("Hi [1]", 1))
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0].content)["model"], "llama")


if __name__ == "__main__":
    unittest.main()

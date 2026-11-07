import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.temp.name) / "test.db"),
            "ADMIN_API_KEY": "admin-test-key-" + "a" * 24,
            "EMPLOYEE_API_KEY": "employee-test-key-" + "b" * 24})
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

    def make_assistant(self):
        return self.client.post("/assistants", headers=self.admin,
                                json={"name": "IT Helper", "purpose": "VPN help", "system_prompt": "SECRET-PROMPT"}).json()

    def test_only_admin_can_configure_and_employees_do_not_see_prompt(self):
        r = self.client.post("/assistants", headers=self.employee, json={"name": "x"})
        self.assertEqual(r.status_code, 403)
        a = self.make_assistant()
        self.assertEqual(a["system_prompt"], "SECRET-PROMPT")
        seen = self.client.get(f"/assistants/{a['id']}", headers=self.employee).json()
        self.assertNotIn("system_prompt", seen)
        self.assertNotIn("SECRET-PROMPT", str(self.client.get("/assistants", headers=self.employee).json()))

    def test_collection_scopes_search_and_hides_admin_docs(self):
        pub = self.upload("pub.txt", b"VPN employee setup")
        other = self.upload("other.txt", b"VPN unrelated cafeteria")
        priv = self.upload("priv.txt", b"VPN ADMIN-CANARY", "admin")
        a = self.make_assistant()
        r = self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin,
                            json={"document_ids": [pub["id"], priv["id"]]})
        self.assertEqual(r.status_code, 200)
        emp = self.client.post(f"/assistants/{a['id']}/search", headers=self.employee, json={"query": "VPN"}).json()
        self.assertEqual([x["document_id"] for x in emp["results"]], [pub["id"]])
        self.assertNotIn("ADMIN-CANARY", str(emp))
        self.assertNotIn(other["id"], str(emp))
        adm = self.client.post(f"/assistants/{a['id']}/search", headers=self.admin, json={"query": "VPN"}).json()
        self.assertEqual(len(adm["results"]), 2)
        listed = self.client.get(f"/assistants/{a['id']}", headers=self.employee).json()
        self.assertEqual(listed["document_ids"], [pub["id"]])

    def test_unknown_document_ids_and_missing_assistant(self):
        a = self.make_assistant()
        r = self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin, json={"document_ids": ["nope"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/assistants/nope", headers=self.admin).status_code, 404)

    def test_patch_and_delete_assistant(self):
        a = self.make_assistant()
        r = self.client.patch(f"/assistants/{a['id']}", headers=self.admin, json={"purpose": "new"}).json()
        self.assertEqual((r["purpose"], r["name"]), ("new", "IT Helper"))
        self.assertEqual(self.client.delete(f"/assistants/{a['id']}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get(f"/assistants/{a['id']}", headers=self.admin).status_code, 404)

    def test_delete_document_removes_chunks_search_rows_and_collection_links(self):
        doc = self.upload("d.txt", b"VPN gone soon")
        a = self.make_assistant()
        self.client.put(f"/assistants/{a['id']}/documents", headers=self.admin, json={"document_ids": [doc["id"]]})
        self.assertEqual(self.client.delete(f"/documents/{doc['id']}", headers=self.employee).status_code, 403)
        self.assertEqual(self.client.delete(f"/documents/{doc['id']}", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.delete(f"/documents/{doc['id']}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.post("/search", headers=self.admin, json={"query": "VPN"}).json()["results"], [])
        self.assertEqual(self.client.get(f"/assistants/{a['id']}", headers=self.admin).json()["document_ids"], [])

    def test_search_prefers_all_terms_and_ignores_filler_words(self):
        self.upload("a.txt", b"restart the client when VPN error 42 appears")
        self.upload("b.txt", b"the cafeteria menu for the week")
        res = self.client.post("/search", headers=self.employee,
                               json={"query": "how do I fix the VPN error"}).json()["results"]
        self.assertTrue(res and res[0]["filename"] == "a.txt")
        self.assertNotIn("b.txt", [r["filename"] for r in res])

    def test_upload_reports_warning_for_blank_pdf_pages(self):
        from tests.test_backend import pdf_bytes
        doc = self.upload("g.pdf", pdf_bytes())
        self.assertEqual(doc["warnings"], [])


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from app.main import app


def pdf_bytes():
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in ("Employee VPN instructions", "VPN error 42 needs a support ticket"):
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(self.temp.name) / "test.db"),
            "APP_ENV": "development",
            "ADVISOR_BACKEND": "echo",
            "MODEL_BACKEND": "echo",
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

    def upload(self, name, data, visibility="employee", headers=None):
        return self.client.post("/documents", headers=headers or self.admin,
                                data={"visibility": visibility}, files={"file": (name, data)})

    def test_requires_identity_and_admin_upload(self):
        self.assertEqual(self.client.get("/documents").status_code, 401)
        self.assertEqual(self.upload("x.txt", b"VPN", headers=self.employee).status_code, 403)

    def test_permission_filter_for_list_search_and_direct_access(self):
        self.upload("public.txt", b"VPN employee setup")
        private = self.upload("private.txt", b"VPN ADMIN-CANARY", "admin").json()
        listing = self.client.get("/documents", headers=self.employee).json()["documents"]
        self.assertEqual(len(listing), 1)
        result = self.client.post("/search", headers=self.employee, json={"query": "VPN"}).json()
        self.assertNotIn("ADMIN-CANARY", str(result))
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.client.get(f"/documents/{private['id']}/chunks", headers=self.employee).status_code, 404)
        admin_result = self.client.post("/search", headers=self.admin, json={"query": "VPN"}).json()
        self.assertEqual(len(admin_result["results"]), 2)

    def test_pdf_preserves_page_citations(self):
        uploaded = self.upload("guide.pdf", pdf_bytes())
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["page_count"], 2)
        result = self.client.post("/search", headers=self.employee, json={"query": "42"}).json()
        self.assertEqual(result["results"][0]["page_number"], 2)
        self.assertEqual(result["results"][0]["document_id"], uploaded.json()["id"])

    def test_rejects_bad_and_empty_files_without_partial_records(self):
        for name, data in [("bad.pdf", b"broken PDF"), ("empty.txt", b"  "), ("bad.txt", b"\xff")]:
            self.assertEqual(self.upload(name, data).status_code, 422)
        self.assertEqual(self.upload("program.exe", b"code").status_code, 415)
        self.assertEqual(self.client.get("/documents", headers=self.admin).json()["documents"], [])

    def test_limits_and_search_syntax(self):
        self.assertEqual(self.upload("big.txt", b"x" * (10 * 1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.upload("x.txt", b"VPN", visibility="public").status_code, 422)
        for query in ["!!!", 'VPN OR " NEAR(*)', "x' OR 1=1 --"]:
            response = self.client.post("/search", headers=self.employee, json={"query": query})
            self.assertEqual(response.status_code, 200, response.text)

    def test_filename_is_metadata_and_chunks_overlap(self):
        doc = self.upload("../../guide.txt", b"vpn " * 1000).json()
        self.assertEqual(doc["filename"], "guide.txt")
        chunks = self.client.get(f"/documents/{doc['id']}/chunks", headers=self.employee).json()["chunks"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["text"][-200:], chunks[1]["text"][:200])


if __name__ == "__main__":
    unittest.main()

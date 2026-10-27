import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app import GoogleDrive, MegaWebDAV, PostgresDatabase, create_app, get_db, hash_password, verify_password


class FakeResponse:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class FakeConnection:
    def __init__(self):
        self.executed = []

    def execute(self, query, parameters):
        self.executed.append((query, parameters))
        return "cursor"

    def commit(self):
        pass

    def close(self):
        pass


class FakeCloud:
    def __init__(self, name, free):
        self.name = name
        self.free = free
        self.files = []
        self.deleted = []

    def connected(self):
        return True

    def list_files(self, parent="/"):
        return [dict(item) for item in self.files]

    def storage(self):
        return {"available": True, "used": 0, "total": self.free, "free": self.free}

    def create_folder(self, name, parent):
        item = {"id": f"{parent}/{name}", "name": name, "is_folder": True, "size": 0, "modified": "", "provider": self.name}
        self.files.append(item)
        return item

    def upload(self, name, parent, content, content_type):
        item = {"id": f"{parent}/{name}", "name": name, "is_folder": False, "size": len(content), "modified": "", "provider": self.name}
        self.files.append(item)
        return item

    def download(self, file_id):
        return b"downloaded"

    def delete(self, file_id):
        self.deleted.append(file_id)

    def rename(self, file_id, name):
        return {"id": file_id, "name": name, "is_folder": False, "size": 0, "modified": "", "provider": self.name}

    def move(self, file_id, parent):
        return {"id": f"{parent}/{file_id}", "name": file_id, "is_folder": False, "size": 0, "modified": "", "provider": self.name}


class CloudVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret",
                "ADMIN_USERNAME": "owner",
                "ADMIN_PASSWORD_HASH": hash_password("correct horse battery staple"),
                "TOKEN_ENCRYPTION_KEY": "",
                "DATABASE_PATH": str(Path(self.temp_dir.name) / "test.sqlite3"),
                "COOKIE_SECURE": False,
                "MEGA_WEBDAV_URL": "http://127.0.0.1:4443/random/",
                "MEGA_FREE_BYTES": "",
            }
        )
        self.google = FakeCloud("google", 100)
        self.mega = FakeCloud("mega", 1000)
        self.app.extensions["google_drive"] = self.google
        self.app.extensions["mega"] = self.mega
        self.client = self.app.test_client()
        self.csrf = self.client.get("/api/session").json["csrf_token"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def login(self):
        response = self.client.post(
            "/api/login",
            json={"username": "owner", "password": "correct horse battery staple"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json["csrf_token"]

    def headers(self):
        return {"X-CSRF-Token": self.csrf}

    def test_password_hash_is_salted_and_verified(self):
        encoded = hash_password("long-password")
        self.assertTrue(verify_password("long-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, hash_password("long-password"))

    def test_private_routes_require_login_and_mutations_require_csrf(self):
        self.assertEqual(self.client.get("/api/notes").status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/api/notes", json={"title": "x"}).status_code, 403)

    def test_session_cookie_uses_oauth_compatible_lax_same_site(self):
        self.login()
        cookie = self.client.get_cookie("session")
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Lax")
        self.assertFalse(cookie.secure)

    def test_postgres_adapter_translates_placeholders(self):
        connection = FakeConnection()
        database = PostgresDatabase(connection)
        self.assertEqual(database.execute("SELECT * FROM notes WHERE id=?", ("n1",)), "cursor")
        self.assertEqual(connection.executed, [("SELECT * FROM notes WHERE id=%s", ("n1",))])

    def test_database_url_selects_postgres_with_ssl_and_initializes_schema(self):
        connection = FakeConnection()
        self.app.config["DATABASE_URL"] = "postgresql://user:password@db.example.test/cloudvault"
        connect = Mock(return_value=connection)
        fake_driver = SimpleNamespace(
            conninfo=SimpleNamespace(
                make_conninfo=lambda url, **kwargs: f"{url} " + " ".join(f"{key}={value}" for key, value in kwargs.items())
            ),
            connect=connect,
        )
        with patch.dict(sys.modules, {"psycopg": fake_driver}):
            with self.app.app_context():
                database = get_db()
                self.assertIsInstance(database, PostgresDatabase)
                self.assertIn("sslmode=require", connect.call_args.args[0])
                self.assertEqual(len(connection.executed), 4)
                database.close()

    def test_render_configuration_can_disable_mega_without_breaking_google(self):
        self.app.config["MEGA_ENABLED"] = False
        self.login()
        response = self.client.get("/api/connections")
        self.assertEqual(response.json["mega"], False)
        self.assertEqual(response.json["mega_supported"], False)
        response = self.client.get("/api/files?provider=mega")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["files"], [])
        upload = self.client.post(
            "/api/files",
            data={"provider": "mega", "file": (io.BytesIO(b"data"), "blocked.txt")},
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 409)

    def test_notes_are_persistent_searchable_editable_and_pinnable(self):
        self.login()
        created = self.client.post(
            "/api/notes",
            json={"title": "Pomysł", "content": "tekst do znalezienia"},
            headers=self.headers(),
        )
        self.assertEqual(created.status_code, 201)
        note = created.json["note"]
        self.assertTrue(note["created_at"])
        self.assertTrue(note["updated_at"])
        updated = self.client.patch(
            f"/api/notes/{note['id']}",
            json={"title": "Projekt", "content": "trwały zapis", "pinned": True},
            headers=self.headers(),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["note"]["pinned"], 1)
        self.assertEqual(self.client.get("/api/notes?q=trwały").json["notes"][0]["title"], "Projekt")
        self.assertEqual(self.client.get("/api/notes?q=nieobecne").json["notes"], [])
        self.assertEqual(
            self.client.delete(f"/api/notes/{note['id']}", headers=self.headers()).status_code,
            200,
        )

    def test_notes_remain_after_new_login(self):
        self.login()
        created = self.client.post(
            "/api/notes",
            json={"title": "Trwała", "content": "zapis bazy"},
            headers=self.headers(),
        )
        self.assertEqual(created.status_code, 201)
        self.client.post("/api/logout", headers=self.headers())
        another_client = self.app.test_client()
        csrf = another_client.get("/api/session").json["csrf_token"]
        logged_in = another_client.post(
            "/api/login",
            json={"username": "owner", "password": "correct horse battery staple"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(logged_in.status_code, 200)
        notes = another_client.get("/api/notes?q=Trwała").json["notes"]
        self.assertEqual(notes[0]["content"], "zapis bazy")

    def test_file_listing_favorites_upload_and_delete_use_provider_adapters(self):
        self.login()
        self.google.files.append({"id": "g1", "name": "drive.txt", "is_folder": False, "size": 4, "modified": "", "provider": "google"})
        self.mega.files.append({"id": "/m1", "name": "mega.txt", "is_folder": False, "size": 4, "modified": "", "provider": "mega"})
        files = self.client.get("/api/files?provider=all").json["files"]
        self.assertEqual({item["provider"] for item in files}, {"google", "mega"})
        self.assertEqual(
            self.client.put("/api/favorites", json={"provider": "mega", "id": "/m1"}, headers=self.headers()).status_code,
            200,
        )
        self.assertTrue(next(item for item in self.client.get("/api/files?provider=mega").json["files"] if item["id"] == "/m1")["favorite"])
        upload = self.client.post(
            "/api/files",
            data={"provider": "auto", "file": (io.BytesIO(b"hello"), "hello.txt")},
            headers=self.headers(),
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(upload.json["file"]["provider"], "mega")
        self.assertEqual(upload.json["file"]["size"], 5)
        deleted = self.client.delete(
            "/api/files/mega",
            json={"id": "/m1"},
            headers=self.headers(),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.mega.deleted, ["/m1"])

    def test_cloud_file_endpoints_support_rename_move_download_and_folders(self):
        self.login()
        created = self.client.post(
            "/api/folders",
            json={"provider": "google", "name": "Dokumenty", "parent": "root"},
            headers=self.headers(),
        )
        self.assertEqual(created.status_code, 201)
        changed = self.client.patch(
            "/api/files/google",
            json={"id": "g1", "name": "nowa-nazwa.txt"},
            headers=self.headers(),
        )
        self.assertEqual(changed.json["file"]["name"], "nowa-nazwa.txt")
        moved = self.client.patch(
            "/api/files/google",
            json={"id": "g1", "parent": "root"},
            headers=self.headers(),
        )
        self.assertEqual(moved.status_code, 200)
        downloaded = self.client.get("/api/files/mega/download?id=%2Ffolder%2Fplik.txt&name=plik.txt")
        self.assertEqual(downloaded.data, b"downloaded")

    def test_megacmd_webdav_configuration_is_local_only(self):
        adapter = MegaWebDAV(self.app)
        self.assertTrue(adapter._url("/folder").startswith("http://127.0.0.1:4443/random/folder"))
        self.app.config["MEGA_WEBDAV_URL"] = "http://127.0.0.1.evil.example:4443/"
        with self.assertRaises(Exception):
            adapter._url("/")
        self.app.config["MEGA_WEBDAV_URL"] = "http://127.0.0.1:4443/random/"
        with self.assertRaises(Exception):
            adapter._url("/../outside")

    def test_google_resumable_upload_encrypts_tokens_and_sends_file_bytes(self):
        self.app.config["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        drive = GoogleDrive(self.app)
        initiation = FakeResponse({"Location": "https://www.googleapis.com/upload/session"})
        completed = FakeResponse(
            {"Content-Type": "application/json"},
            json.dumps({"id": "new-id", "name": "backup.zip", "mimeType": "application/zip", "size": "4"}).encode(),
        )
        with self.app.app_context():
            drive.save_tokens({"access_token": "secret-access", "refresh_token": "secret-refresh"})
            raw = get_db().execute(
                "SELECT value FROM settings WHERE key='google_tokens'"
            ).fetchone()["value"]
            self.assertNotIn(b"secret-access", raw)
            with patch("app.urllib.request.urlopen", side_effect=[initiation, completed]) as open_url:
                result = drive.upload("backup.zip", "root", b"data", "application/zip")
        self.assertEqual(result["id"], "new-id")
        initiation_request = open_url.call_args_list[0].args[0]
        upload_request = open_url.call_args_list[1].args[0]
        self.assertEqual(initiation_request.get_method(), "POST")
        self.assertEqual(initiation_request.get_header("X-upload-content-length"), "4")
        self.assertEqual(upload_request.get_method(), "PUT")
        self.assertEqual(upload_request.data, b"data")

    def test_google_workspace_documents_download_as_exported_files(self):
        self.app.config["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        drive = GoogleDrive(self.app)
        metadata = FakeResponse(
            {"Content-Type": "application/json"},
            json.dumps({"mimeType": "application/vnd.google-apps.document"}).encode(),
        )
        exported = FakeResponse({"Content-Type": "application/pdf"}, b"%PDF-test")
        with self.app.app_context():
            drive.save_tokens({"access_token": "token", "refresh_token": "refresh"})
            with patch("app.urllib.request.urlopen", side_effect=[metadata, exported]) as open_url:
                content, mime_type = drive.download("doc-id")
        self.assertEqual(content, b"%PDF-test")
        self.assertEqual(mime_type, "application/pdf")
        export_request = open_url.call_args_list[1].args[0]
        self.assertIn("/export?mimeType=application%2Fpdf", export_request.full_url)

    def test_megacmd_adapter_parses_propfind_and_uploads_via_webdav(self):
        adapter = MegaWebDAV(self.app)
        xml = b"""<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/random/folder/</d:href><d:propstat><d:prop>
            <d:resourcetype><d:collection/></d:resourcetype><d:getcontentlength>0</d:getcontentlength>
            <d:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</d:getlastmodified>
          </d:prop></d:propstat></d:response>
          <d:response><d:href>/random/folder/note.txt</d:href><d:propstat><d:prop>
            <d:resourcetype/><d:getcontentlength>12</d:getcontentlength>
            <d:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</d:getlastmodified>
          </d:prop></d:propstat></d:response>
        </d:multistatus>"""
        with patch("app.urllib.request.urlopen", side_effect=[FakeResponse(body=xml), FakeResponse()]) as open_url:
            files = adapter.list_files("/folder")
            uploaded = adapter.upload("new.txt", "/folder", b"hello", "text/plain")
        self.assertEqual(files[0]["id"], "/folder/note.txt")
        self.assertEqual(files[0]["size"], 12)
        self.assertEqual(uploaded["id"], "/folder/new.txt")
        self.assertEqual(open_url.call_args_list[0].args[0].get_method(), "PROPFIND")
        self.assertEqual(open_url.call_args_list[1].args[0].get_method(), "PUT")
        self.assertEqual(open_url.call_args_list[1].args[0].data, b"hello")


if __name__ == "__main__":
    unittest.main()

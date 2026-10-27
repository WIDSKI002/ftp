import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, current_app, g, jsonify, redirect, render_template, request, send_file, session

load_dotenv()

GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_FIELDS = "files(id,name,mimeType,size,modifiedTime,parents),nextPageToken"
FOLDER_MIME = "application/vnd.google-apps.folder"


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ProviderError(AppError):
    def __init__(self, message, status=502):
        super().__init__(message, status)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"scrypt$16384$8$1${encode(salt)}${encode(digest)}"


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(actual, decode(expected))
    except (ValueError, TypeError):
        return False


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresDatabase:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        return self.connection.execute(query.replace("?", "%s"), parameters)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


class GoogleDrive:
    def __init__(self, app):
        self.app = app

    def _cipher(self):
        key = self.app.config.get("TOKEN_ENCRYPTION_KEY")
        if not key:
            raise ProviderError("Brak TOKEN_ENCRYPTION_KEY; integracja Google jest wyłączona.", 503)
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            raise ProviderError("Nieprawidłowy TOKEN_ENCRYPTION_KEY.", 503)

    def _token_row(self):
        db = get_db()
        row = db.execute("SELECT value FROM settings WHERE key = 'google_tokens'").fetchone()
        if not row:
            raise ProviderError("Połącz Google Drive w Ustawieniach.", 409)
        try:
            return json.loads(self._cipher().decrypt(row["value"]).decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            raise ProviderError("Nie można odszyfrować tokenu Google; sprawdź TOKEN_ENCRYPTION_KEY.", 503)

    def save_tokens(self, tokens):
        cipher = self._cipher()
        db = get_db()
        db.execute(
            "INSERT INTO settings(key,value) VALUES('google_tokens',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cipher.encrypt(json.dumps(tokens).encode("utf-8")),),
        )
        db.commit()

    def connected(self):
        return bool(get_db().execute("SELECT 1 FROM settings WHERE key='google_tokens'").fetchone())

    def _refresh(self, tokens):
        config = self.app.config
        if not config.get("GOOGLE_CLIENT_ID") or not config.get("GOOGLE_CLIENT_SECRET"):
            raise ProviderError("Brak konfiguracji OAuth Google.", 503)
        data = urllib.parse.urlencode(
            {
                "client_id": config["GOOGLE_CLIENT_ID"],
                "client_secret": config["GOOGLE_CLIENT_SECRET"],
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    "https://oauth2.googleapis.com/token",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ),
                timeout=30,
            ) as response:
                refreshed = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            raise ProviderError("Nie udało się odświeżyć tokenu Google. Połącz konto ponownie.", 502)
        tokens.update(refreshed)
        tokens["refresh_token"] = tokens.get("refresh_token") or refreshed.get("refresh_token")
        self.save_tokens(tokens)
        return tokens

    def _request(
        self, method, path, query=None, body=None, content_type="application/json",
        upload=False, upload_content_type=None, upload_content_length=None
    ):
        tokens = self._token_row()
        url = ("https://www.googleapis.com/upload/drive/v3" if upload else "https://www.googleapis.com/drive/v3") + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        if body is not None:
            headers["Content-Type"] = content_type
        if upload and method == "POST":
            headers["X-Upload-Content-Type"] = upload_content_type or content_type
            headers["X-Upload-Content-Length"] = str(upload_content_length if upload_content_length is not None else len(body or b""))
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                payload = response.read()
                return response.headers, json.loads(payload) if payload and "json" in response.headers.get("Content-Type", "") else payload
        except urllib.error.HTTPError as error:
            if error.code == 401 and tokens.get("refresh_token"):
                tokens = self._refresh(tokens)
                headers["Authorization"] = f"Bearer {tokens['access_token']}"
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(url, data=body, headers=headers, method=method),
                        timeout=120,
                    ) as response:
                        payload = response.read()
                        return response.headers, json.loads(payload) if payload else b""
                except urllib.error.HTTPError as retry_error:
                    raise ProviderError(f"Google Drive zwrócił błąd HTTP {retry_error.code}.", 502)
            raise ProviderError(f"Google Drive zwrócił błąd HTTP {error.code}.", 502)
        except (urllib.error.URLError, TimeoutError):
            raise ProviderError("Nie można połączyć się z Google Drive.", 502)

    def list_files(self, parent="root"):
        query = {
            "q": f"'{parent}' in parents and trashed = false",
            "fields": GOOGLE_FIELDS,
            "pageSize": 1000,
            "orderBy": "folder,name",
        }
        files = []
        while True:
            _, page = self._request("GET", "/files", query)
            files.extend(self._format(item, "google") for item in page.get("files", []))
            if not page.get("nextPageToken"):
                return files
            query["pageToken"] = page["nextPageToken"]

    @staticmethod
    def _format(item, provider):
        return {
            "id": item["id"],
            "name": item["name"],
            "is_folder": item.get("mimeType") == FOLDER_MIME or item.get("is_folder", False),
            "size": int(item["size"]) if item.get("size") is not None else None,
            "modified": item.get("modifiedTime") or "",
            "provider": provider,
        }

    def storage(self):
        _, data = self._request("GET", "/about", {"fields": "storageQuota"})
        quota = data.get("storageQuota", {})
        limit = int(quota["limit"]) if quota.get("limit") else None
        used = int(quota.get("usage") or 0)
        return {"available": True, "used": used, "total": limit, "free": max(0, limit - used) if limit else None}

    def create_folder(self, name, parent):
        body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).encode()
        _, item = self._request("POST", "/files", {"fields": "id,name,mimeType,modifiedTime"}, body)
        return self._format(item, "google")

    def upload(self, name, parent, content, content_type):
        metadata = json.dumps({"name": name, "parents": [parent]}).encode()
        headers, _ = self._request(
            "POST",
            "/files",
            {"uploadType": "resumable", "fields": "id,name,mimeType,size,modifiedTime"},
            metadata,
            "application/json; charset=UTF-8",
            upload=True,
            upload_content_type=content_type,
            upload_content_length=len(content),
        )
        location = headers.get("Location")
        if not location:
            raise ProviderError("Google Drive nie zwrócił adresu do przesyłania pliku.", 502)
        tokens = self._token_row()
        upload_request = lambda access_token: urllib.request.Request(
            location,
            data=content,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": content_type},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(
                upload_request(tokens["access_token"]),
                timeout=300,
            ) as response:
                item = json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code != 401 or not tokens.get("refresh_token"):
                raise ProviderError(f"Google Drive nie przyjął przesyłanego pliku (HTTP {error.code}).", 502)
            tokens = self._refresh(tokens)
            try:
                with urllib.request.urlopen(upload_request(tokens["access_token"]), timeout=300) as response:
                    item = json.loads(response.read())
            except urllib.error.HTTPError as retry_error:
                raise ProviderError(f"Google Drive nie przyjął przesyłanego pliku (HTTP {retry_error.code}).", 502)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                raise ProviderError("Nie udało się dokończyć przesyłania do Google Drive.", 502)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise ProviderError("Nie udało się dokończyć przesyłania do Google Drive.", 502)
        return self._format(item, "google")

    def download(self, file_id):
        encoded_id = urllib.parse.quote(file_id, safe="")
        _, metadata = self._request("GET", f"/files/{encoded_id}", {"fields": "mimeType"})
        mime_type = metadata.get("mimeType", "application/octet-stream")
        if mime_type.startswith("application/vnd.google-apps."):
            exports = {
                "application/vnd.google-apps.document": "application/pdf",
                "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.google-apps.presentation": "application/pdf",
                "application/vnd.google-apps.drawing": "image/png",
            }
            mime_type = exports.get(mime_type)
            if not mime_type:
                raise ProviderError("Ten typ pliku Google nie obsługuje eksportu przez Drive API.", 422)
            _, content = self._request(
                "GET", f"/files/{encoded_id}/export", {"mimeType": mime_type}
            )
            return content, mime_type
        _, content = self._request("GET", f"/files/{encoded_id}", {"alt": "media"})
        return content, mime_type

    def delete(self, file_id):
        self._request("DELETE", f"/files/{urllib.parse.quote(file_id, safe='')}")

    def rename(self, file_id, name):
        _, item = self._request(
            "PATCH", f"/files/{urllib.parse.quote(file_id, safe='')}",
            {"fields": "id,name,mimeType,size,modifiedTime"},
            json.dumps({"name": name}).encode(),
        )
        return self._format(item, "google")

    def move(self, file_id, parent):
        _, current = self._request(
            "GET", f"/files/{urllib.parse.quote(file_id, safe='')}", {"fields": "parents"}
        )
        query = {"addParents": parent, "fields": "id,name,mimeType,size,modifiedTime"}
        if current.get("parents"):
            query["removeParents"] = ",".join(current["parents"])
        _, item = self._request(
            "PATCH", f"/files/{urllib.parse.quote(file_id, safe='')}", query, b"{}"
        )
        return self._format(item, "google")


class MegaWebDAV:
    """MEGA access through MEGAcmd's official local WebDAV server."""

    def __init__(self, app):
        self.app = app

    def _url(self, path):
        base = self.app.config.get("MEGA_WEBDAV_URL", "").rstrip("/") + "/"
        if not base.strip("/"):
            raise ProviderError("Skonfiguruj MEGAcmd i MEGA_WEBDAV_URL.", 409)
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ProviderError("MEGA_WEBDAV_URL musi wskazywać lokalny serwer MEGAcmd.", 503)
        segments = str(path).replace("\\", "/").split("/")
        if any(segment in (".", "..") or "\x00" in segment for segment in segments):
            raise AppError("Nieprawidłowa ścieżka MEGA.", 400)
        relative = "/".join(urllib.parse.quote(part, safe="") for part in path.strip("/").split("/") if part)
        return urllib.parse.urljoin(base, relative)

    def _request(self, method, path, body=None, headers=None):
        req_headers = dict(headers or {})
        req = urllib.request.Request(self._url(path), data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.headers, response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (200, 201, 204, 207):
                raise ProviderError(f"MEGAcmd WebDAV zwrócił błąd HTTP {error.code}.", 502)
            return error.headers, error.read()
        except (urllib.error.URLError, TimeoutError):
            raise ProviderError("Nie można połączyć się z lokalnym WebDAV MEGAcmd.", 502)

    def list_files(self, parent="/"):
        _, xml = self._request(
            "PROPFIND", parent,
            b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
            b"<d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>",
            {"Depth": "1", "Content-Type": "application/xml"},
        )
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            raise ProviderError("MEGAcmd zwrócił nieprawidłową odpowiedź WebDAV.", 502)
        ns = {"d": "DAV:"}
        base_path = urllib.parse.unquote(urllib.parse.urlsplit(self._url("/")).path.rstrip("/"))
        current = urllib.parse.unquote(urllib.parse.urlsplit(self._url(parent)).path.rstrip("/"))
        result = []
        for response in root.findall("d:response", ns):
            href = response.findtext("d:href", default="", namespaces=ns)
            path = urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
            if path == current or (current and not path.startswith(current + "/")):
                continue
            prop = response.find("d:propstat/d:prop", ns)
            if prop is None:
                continue
            is_folder = prop.find("d:resourcetype/d:collection", ns) is not None
            result.append(
                {
                    "id": "/" + path[len(base_path):].strip("/"),
                    "name": urllib.parse.unquote(path.rsplit("/", 1)[-1]),
                    "is_folder": is_folder,
                    "size": int(prop.findtext("d:getcontentlength", default="0", namespaces=ns) or 0),
                    "modified": prop.findtext("d:getlastmodified", default="", namespaces=ns),
                    "provider": "mega",
                }
            )
        return result

    def create_folder(self, name, parent):
        target = self._join(parent, name)
        self._request("MKCOL", target)
        return {"id": target, "name": name, "is_folder": True, "size": 0, "modified": utc_now(), "provider": "mega"}

    @staticmethod
    def _join(parent, name):
        return "/" + "/".join(part.strip("/") for part in (parent, name) if part.strip("/"))

    def upload(self, name, parent, content, content_type):
        path = self._join(parent, name)
        self._request("PUT", path, content, {"Content-Type": content_type})
        return {"id": path, "name": name, "is_folder": False, "size": len(content), "modified": utc_now(), "provider": "mega"}

    def download(self, file_id):
        return self._request("GET", file_id)[1]

    def delete(self, file_id):
        self._request("DELETE", file_id)

    def rename(self, file_id, name):
        parent = file_id.rsplit("/", 1)[0] or "/"
        destination = self._join(parent, name)
        self._request("MOVE", file_id, headers={"Destination": self._url(destination), "Overwrite": "F"})
        return {"id": destination, "name": name, "is_folder": False, "size": 0, "modified": utc_now(), "provider": "mega"}

    def move(self, file_id, parent):
        destination = self._join(parent, file_id.rsplit("/", 1)[-1])
        self._request("MOVE", file_id, headers={"Destination": self._url(destination), "Overwrite": "F"})
        return {"id": destination, "name": destination.rsplit("/", 1)[-1], "is_folder": False, "size": 0, "modified": utc_now(), "provider": "mega"}

    def storage(self):
        try:
            return {
                "available": bool(self.app.config.get("MEGA_WEBDAV_URL")),
                "used": None,
                "total": None,
                "free": int(self.app.config["MEGA_FREE_BYTES"]) if self.app.config.get("MEGA_FREE_BYTES") else None,
            }
        except ValueError:
            raise ProviderError("MEGA_FREE_BYTES musi być liczbą bajtów.", 503)


def get_db():
    if "db" not in g:
        database_url = current_app.config.get("DATABASE_URL", "").strip()
        if database_url:
            import psycopg

            class HybridRow(dict):
                def __getitem__(self, key):
                    if isinstance(key, int):
                        return tuple(self.values())[key]
                    return super().__getitem__(key)

            def hybrid_row(cursor):
                if cursor.description is None:
                    return lambda values: values
                columns = [column.name for column in cursor.description]
                return lambda values: HybridRow(zip(columns, values))

            dsn = psycopg.conninfo.make_conninfo(
                database_url,
                sslmode="require",
                connect_timeout=10,
            )
            connection = psycopg.connect(dsn, row_factory=hybrid_row)
            g.db = PostgresDatabase(connection)
            for statement in (
                """CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                    pinned SMALLINT NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""",
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BYTEA NOT NULL)",
                """CREATE TABLE IF NOT EXISTS favorites (
                    provider TEXT NOT NULL, file_id TEXT NOT NULL, PRIMARY KEY(provider, file_id)
                )""",
                """CREATE TABLE IF NOT EXISTS login_attempts (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    attempted_at BIGINT NOT NULL, remote_addr TEXT NOT NULL
                )""",
            ):
                g.db.execute(statement)
            g.db.commit()
        else:
            db_path = Path(current_app.config["DATABASE_PATH"])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(db_path)
            if os.name != "nt":
                os.chmod(db_path, 0o600)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    provider TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    PRIMARY KEY(provider, file_id)
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempted_at INTEGER NOT NULL,
                    remote_addr TEXT NOT NULL
                );
                """
            )
    return g.db


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", ""),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD_HASH=os.getenv("ADMIN_PASSWORD_HASH", ""),
        TOKEN_ENCRYPTION_KEY=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
        DATABASE_PATH=os.getenv("DATABASE_PATH", "instance/cloudvault.sqlite3"),
        DATABASE_URL=os.getenv("DATABASE_URL", ""),
        COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024,
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        GOOGLE_REDIRECT_URI=os.getenv("GOOGLE_REDIRECT_URI", ""),
        MEGA_WEBDAV_URL=os.getenv("MEGA_WEBDAV_URL", ""),
        MEGA_FREE_BYTES=os.getenv("MEGA_FREE_BYTES", ""),
        MEGA_ENABLED=os.getenv("MEGA_ENABLED", "true").lower() == "true",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    )
    if overrides:
        app.config.update(overrides)
    if not app.config["SECRET_KEY"] and not app.config.get("TESTING"):
        raise RuntimeError("Ustaw silny SECRET_KEY w pliku .env.")
    if not app.config["ADMIN_PASSWORD_HASH"] and not app.config.get("TESTING"):
        raise RuntimeError("Wygeneruj ADMIN_PASSWORD_HASH poleceniem `python app.py hash-password`.")
    if app.config["COOKIE_SECURE"]:
        app.config["SESSION_COOKIE_SECURE"] = True
    else:
        app.config["SESSION_COOKIE_SECURE"] = False

    app.extensions["google_drive"] = GoogleDrive(app)
    app.extensions["mega"] = MegaWebDAV(app)
    app.teardown_appcontext(close_db)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(AppError)
    def handle_app_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Plik przekracza skonfigurowany limit przesyłania."), 413

    def require_auth():
        if not session.get("authenticated"):
            raise AppError("Zaloguj się, aby kontynuować.", 401)

    def require_csrf():
        require_auth()
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, session.get("csrf_token", "")):
            raise AppError("Sesja formularza wygasła. Odśwież stronę.", 403)
        origin = request.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != request.host:
            raise AppError("Nieprawidłowe źródło żądania.", 403)

    def provider(name):
        if name == "google":
            drive = app.extensions["google_drive"]
            if not drive.connected():
                raise AppError("Połącz Google Drive w Ustawieniach.", 409)
            return drive
        if name == "mega":
            if not app.config["MEGA_ENABLED"]:
                raise AppError("MEGA nie jest obsługiwane na tym hostingu. Użyj własnego VPS-a z MEGAcmd.", 409)
            mega = app.extensions["mega"]
            if not app.config.get("MEGA_WEBDAV_URL"):
                raise AppError("Skonfiguruj MEGAcmd i MEGA_WEBDAV_URL.", 409)
            return mega
        raise AppError("Nieznany dysk.", 400)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/session")
    def get_session():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return jsonify(
            authenticated=bool(session.get("authenticated")),
            username=app.config["ADMIN_USERNAME"] if session.get("authenticated") else None,
            csrf_token=session["csrf_token"],
        )

    @app.post("/api/login")
    def login():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf_token"]):
            raise AppError("Odśwież stronę i spróbuj ponownie.", 403)
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", ""))[:200]
        password = str(payload.get("password", ""))[:1024]
        db = get_db()
        cutoff = int(datetime.now(timezone.utc).timestamp()) - 900
        addr = request.remote_addr or "unknown"
        attempts = db.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE remote_addr=? AND attempted_at>?",
            (addr, cutoff),
        ).fetchone()[0]
        db.execute("DELETE FROM login_attempts WHERE attempted_at<=?", (cutoff,))
        db.commit()
        if attempts >= 5:
            raise AppError("Za dużo prób logowania. Odczekaj 15 minut.", 429)
        correct_user = hmac.compare_digest(username, str(app.config["ADMIN_USERNAME"]))
        correct_password = verify_password(password, app.config["ADMIN_PASSWORD_HASH"])
        if not (correct_user and correct_password):
            db.execute("INSERT INTO login_attempts(attempted_at,remote_addr) VALUES(?,?)", (int(datetime.now(timezone.utc).timestamp()), addr))
            db.commit()
            raise AppError("Nieprawidłowy login lub hasło.", 401)
        db.execute("DELETE FROM login_attempts WHERE remote_addr=?", (addr,))
        db.commit()
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        return jsonify(authenticated=True, username=app.config["ADMIN_USERNAME"], csrf_token=session["csrf_token"])

    @app.post("/api/logout")
    def logout():
        require_csrf()
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/notes")
    def notes_list():
        require_auth()
        query = request.args.get("q", "").strip()
        rows = get_db().execute(
            "SELECT id,title,content,pinned,created_at,updated_at FROM notes "
            "WHERE title LIKE ? OR content LIKE ? ORDER BY pinned DESC, updated_at DESC",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        return jsonify(notes=[dict(row) for row in rows])

    @app.post("/api/notes")
    def notes_create():
        require_csrf()
        data = request.get_json(silent=True) or {}
        title = str(data.get("title", "")).strip()[:200] or "Nowa notatka"
        content = str(data.get("content", ""))
        if len(content) > 1_000_000:
            raise AppError("Notatka nie może przekraczać 1 MB.", 413)
        note_id, now = secrets.token_urlsafe(16), utc_now()
        get_db().execute(
            "INSERT INTO notes(id,title,content,created_at,updated_at) VALUES(?,?,?,?,?)",
            (note_id, title, content, now, now),
        )
        get_db().commit()
        return jsonify(note=dict(id=note_id, title=title, content=content, pinned=0, created_at=now, updated_at=now)), 201

    @app.patch("/api/notes/<note_id>")
    def notes_update(note_id):
        require_csrf()
        data = request.get_json(silent=True) or {}
        db = get_db()
        row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            raise AppError("Nie znaleziono notatki.", 404)
        title = str(data.get("title", row["title"])).strip()[:200] or "Bez tytułu"
        content = str(data.get("content", row["content"]))
        if len(content) > 1_000_000:
            raise AppError("Notatka nie może przekraczać 1 MB.", 413)
        pinned = int(bool(data.get("pinned", row["pinned"])))
        now = utc_now()
        db.execute("UPDATE notes SET title=?,content=?,pinned=?,updated_at=? WHERE id=?", (title, content, pinned, now, note_id))
        db.commit()
        return jsonify(note=dict(id=note_id, title=title, content=content, pinned=pinned, created_at=row["created_at"], updated_at=now))

    @app.delete("/api/notes/<note_id>")
    def notes_delete(note_id):
        require_csrf()
        cursor = get_db().execute("DELETE FROM notes WHERE id=?", (note_id,))
        get_db().commit()
        if not cursor.rowcount:
            raise AppError("Nie znaleziono notatki.", 404)
        return jsonify(ok=True)

    @app.get("/api/files")
    def files_list():
        require_auth()
        selected = request.args.get("provider", "all")
        parent = request.args.get("parent", "root" if selected == "google" else "/")
        providers = ("google", "mega") if selected == "all" else (selected,)
        items = []
        for name in providers:
            try:
                items.extend(provider(name).list_files(parent if selected != "all" else ("root" if name == "google" else "/")))
            except AppError as error:
                if error.status == 409:
                    continue
                raise
        ids = {(row["provider"], row["file_id"]) for row in get_db().execute("SELECT provider,file_id FROM favorites")}
        for item in items:
            item["favorite"] = (item["provider"], item["id"]) in ids
        return jsonify(files=items)

    @app.get("/api/storage")
    def storage():
        require_auth()
        result = {}
        for name in ("google", "mega"):
            try:
                result[name] = provider(name).storage()
            except AppError as error:
                if error.status != 409:
                    raise
                result[name] = {"available": False, "used": None, "total": None, "free": None}
        return jsonify(storage=result)

    @app.put("/api/favorites")
    def favorite_add():
        require_csrf()
        data = request.get_json(silent=True) or {}
        if data.get("provider") not in ("google", "mega") or not data.get("id"):
            raise AppError("Nieprawidłowy plik.", 400)
        get_db().execute(
            "INSERT INTO favorites(provider,file_id) VALUES(?,?) ON CONFLICT(provider,file_id) DO NOTHING",
            (data["provider"], data["id"]),
        )
        get_db().commit()
        return jsonify(ok=True)

    @app.delete("/api/favorites")
    def favorite_remove():
        require_csrf()
        data = request.get_json(silent=True) or {}
        get_db().execute("DELETE FROM favorites WHERE provider=? AND file_id=?", (data.get("provider"), data.get("id")))
        get_db().commit()
        return jsonify(ok=True)

    @app.post("/api/folders")
    def create_folder():
        require_csrf()
        data = request.get_json(silent=True) or {}
        name = safe_name(data.get("name"))
        selected = data.get("provider", "google")
        parent = data.get("parent", "root" if selected == "google" else "/")
        return jsonify(folder=provider(selected).create_folder(name, parent)), 201

    @app.post("/api/files")
    def upload_file():
        require_csrf()
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            raise AppError("Wybierz plik do przesłania.", 400)
        name = safe_name(uploaded.filename)
        content = uploaded.read()
        selected = request.form.get("provider", "auto")
        parent = request.form.get("parent", "root")
        if selected == "auto":
            choices = []
            for candidate in ("google", "mega"):
                try:
                    space = provider(candidate).storage()
                except AppError:
                    continue
                if space.get("free") is not None and space["free"] >= len(content):
                    choices.append((space["free"], candidate))
            if not choices:
                raise AppError("Nie można wiarygodnie ustalić wolnego miejsca. Wybierz dysk ręcznie lub skonfiguruj limit MEGA.", 422)
            selected = max(choices)[1]
        chosen_parent = parent if selected == request.form.get("provider") else ("root" if selected == "google" else "/")
        uploaded_file = provider(selected).upload(name, chosen_parent, content, uploaded.mimetype or "application/octet-stream")
        return jsonify(file=uploaded_file), 201

    @app.get("/api/files/<provider_name>/download")
    def download_file(provider_name):
        require_auth()
        name = request.args.get("name", "plik")
        file_id = request.args.get("id", "")
        if not file_id:
            raise AppError("Brak identyfikatora pliku.", 400)
        downloaded = provider(provider_name).download(file_id)
        content, mime_type = downloaded if isinstance(downloaded, tuple) else (downloaded, "application/octet-stream")
        if mime_type == "application/pdf" and not name.lower().endswith(".pdf"):
            name += ".pdf"
        elif mime_type == "image/png" and not name.lower().endswith(".png"):
            name += ".png"
        elif mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" and not name.lower().endswith(".xlsx"):
            name += ".xlsx"
        return send_file(io.BytesIO(content), mimetype=mime_type, as_attachment=True, download_name=name)

    @app.delete("/api/files/<provider_name>")
    def delete_file(provider_name):
        require_csrf()
        data = request.get_json(silent=True) or {}
        file_id = str(data.get("id", ""))
        if not file_id:
            raise AppError("Brak identyfikatora pliku.", 400)
        provider(provider_name).delete(file_id)
        return jsonify(ok=True)

    @app.patch("/api/files/<provider_name>")
    def change_file(provider_name):
        require_csrf()
        data = request.get_json(silent=True) or {}
        file_id = str(data.get("id", ""))
        if not file_id:
            raise AppError("Brak identyfikatora pliku.", 400)
        cloud = provider(provider_name)
        if "name" in data:
            return jsonify(file=cloud.rename(file_id, safe_name(data["name"])))
        if "parent" in data:
            return jsonify(file=cloud.move(file_id, str(data["parent"])))
        raise AppError("Podaj nową nazwę lub folder docelowy.", 400)

    @app.get("/oauth/google")
    def google_oauth_start():
        require_auth()
        if not app.config.get("GOOGLE_CLIENT_ID") or not app.config.get("GOOGLE_REDIRECT_URI"):
            raise AppError("Ustaw GOOGLE_CLIENT_ID i GOOGLE_REDIRECT_URI w .env.", 503)
        state = secrets.token_urlsafe(32)
        session["google_oauth_state"] = state
        query = urllib.parse.urlencode(
            {
                "client_id": app.config["GOOGLE_CLIENT_ID"],
                "redirect_uri": app.config["GOOGLE_REDIRECT_URI"],
                "response_type": "code",
                "scope": GOOGLE_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + query)

    @app.get("/oauth/google/callback")
    def google_oauth_callback():
        require_auth()
        state = request.args.get("state", "")
        if not state or not hmac.compare_digest(state, session.pop("google_oauth_state", "")):
            raise AppError("Nieprawidłowy stan OAuth Google. Spróbuj ponownie.", 403)
        if request.args.get("error"):
            raise AppError("Autoryzacja Google została anulowana.", 400)
        config = app.config
        body = urllib.parse.urlencode(
            {
                "code": request.args.get("code", ""),
                "client_id": config["GOOGLE_CLIENT_ID"],
                "client_secret": config["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": config["GOOGLE_REDIRECT_URI"],
                "grant_type": "authorization_code",
            }
        ).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request("https://oauth2.googleapis.com/token", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}),
                timeout=30,
            ) as response:
                tokens = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            raise ProviderError("Nie udało się zakończyć autoryzacji Google.", 502)
        if not tokens.get("refresh_token"):
            existing = app.extensions["google_drive"]._token_row() if app.extensions["google_drive"].connected() else {}
            tokens["refresh_token"] = existing.get("refresh_token")
        if not tokens.get("refresh_token"):
            raise ProviderError("Google nie zwrócił tokenu odświeżania. Cofnij dostęp aplikacji i połącz ponownie.", 502)
        app.extensions["google_drive"].save_tokens(tokens)
        return redirect("/?connected=google")

    @app.get("/api/connections")
    def connections():
        require_auth()
        return jsonify(
            google=app.extensions["google_drive"].connected(),
            mega_supported=app.config["MEGA_ENABLED"],
            mega=bool(app.config["MEGA_ENABLED"] and app.config.get("MEGA_WEBDAV_URL")),
        )

    return app


def safe_name(value):
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise AppError("Nazwa pliku lub folderu jest nieprawidłowa.", 400)
    return name[:255]


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hash-password":
        password = sys.argv[2] if len(sys.argv) > 2 else __import__("getpass").getpass("Hasło administratora: ")
        if len(password) < 12:
            raise SystemExit("Hasło musi mieć co najmniej 12 znaków.")
        print(hash_password(password))
        return
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()

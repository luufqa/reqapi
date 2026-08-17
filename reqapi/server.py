from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .http_client import RequestCancellation, RequestCancelled, execute_request
from .security import SecurityError, hash_password, is_ip_allowed, random_token, verify_password
from .storage import Storage


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_DB_PATH = ROOT / "data" / "reqapi.sqlite3"
DEFAULT_SECRET_KEY_PATH = ROOT / "data" / "secret.key"
ADMIN_USERNAME = "admin"
SESSION_COOKIE = "reqapi_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_JSON_BODY_BYTES = 32 * 1024 * 1024


class PayloadTooLarge(ValueError):
    pass


class RateLimitError(PermissionError):
    pass


class AuthenticationError(PermissionError):
    pass


class AppState:
    def __init__(self, db_path: Path, secret_key_path: Path):
        self.storage = Storage(db_path)
        self.secret_key_path = secret_key_path
        self._lock = threading.Lock()
        self._executions: dict[str, RequestCancellation] = {}
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def rate_limit(self, key: str, limit: int, window_seconds: int):
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - window_seconds:
                attempts.popleft()
            if len(attempts) >= limit:
                raise RateLimitError("Too many attempts. Please wait and try again.")
            attempts.append(now)

    def start_execution(self, execution_id: str) -> RequestCancellation:
        cancellation = RequestCancellation()
        with self._lock:
            previous = self._executions.pop(execution_id, None)
            self._executions[execution_id] = cancellation
        if previous:
            previous.cancel()
        return cancellation

    def finish_execution(self, execution_id: str, cancellation: RequestCancellation):
        with self._lock:
            if self._executions.get(execution_id) is cancellation:
                self._executions.pop(execution_id, None)

    def cancel_execution(self, execution_id: str) -> bool:
        with self._lock:
            cancellation = self._executions.pop(execution_id, None)
        if cancellation:
            cancellation.cancel()
            return True
        return False


class ReqApiHandler(BaseHTTPRequestHandler):
    server_version = "REQAPI/0.1"

    @property
    def app(self) -> AppState:
        return self.server.app_state

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str):
        if not is_ip_allowed(self.client_address[0]):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Client IP is not allowed."})
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                self.serve_static("index.html")
                return
            if path.startswith("/static/"):
                self.serve_static(path.replace("/static/", "", 1))
                return
            if path.startswith("/api/"):
                self.route_api(method, path, parse_qs(parsed.query))
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except SecurityError as exc:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except AuthenticationError as exc:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)}, no_store=True)
        except PayloadTooLarge as exc:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)}, no_store=True)
        except RateLimitError as exc:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)}, no_store=True)
        except RequestCancelled:
            self.send_json(HTTPStatus.CONFLICT, {"error": "Request was cancelled."}, no_store=True)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def route_api(self, method: str, path: str, query):
        if path == "/api/me" and method == "GET":
            user = self.current_user(required=False)
            self.send_json(
                HTTPStatus.OK,
                {
                    "auth_enabled": True,
                    "setup_required": self.app.storage.setup_required(),
                    "user": user,
                },
                no_store=True,
            )
            return

        if path == "/api/setup" and method == "POST":
            self.app.rate_limit(f"setup:{self.client_address[0]}", 5, 300)
            if not self.app.storage.setup_required():
                self.send_json(HTTPStatus.CONFLICT, {"error": "Admin password is already set."})
                return
            data = self.read_json()
            password = self.require_text(data, "password", min_len=self.password_min_len(ADMIN_USERNAME))
            user = self.app.storage.configure_single_admin(hash_password(password))
            self.login_user(user["id"])
            return

        if path == "/api/login" and method == "POST":
            self.app.rate_limit(f"login:{self.client_address[0]}", 10, 60)
            data = self.read_json()
            username = self.normalize_username(data.get("username"))
            lookup_username = ADMIN_USERNAME if username.lower() == ADMIN_USERNAME else username
            auth = self.app.storage.get_user_auth(lookup_username)
            if not auth:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "User does not exist. Set a password to create this workspace.",
                        "registration_required": True,
                        "username": lookup_username,
                    },
                    no_store=True,
                )
                return
            if not auth or not verify_password(str(data.get("password") or ""), auth["password_hash"]):
                self.send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "Invalid username or password."},
                    no_store=True,
                )
                return
            self.login_user(auth["id"])
            return

        if path == "/api/register" and method == "POST":
            self.app.rate_limit(f"register:{self.client_address[0]}", 5, 300)
            data = self.read_json()
            username = self.normalize_username(data.get("username"))
            if username.lower() == ADMIN_USERNAME:
                raise PermissionError("Admin account is managed by deployment setup.")
            if self.app.storage.get_user_auth(username):
                self.send_json(HTTPStatus.CONFLICT, {"error": "User already exists."}, no_store=True)
                return
            password = self.require_text(data, "password", min_len=self.password_min_len(username))
            user = self.app.storage.create_user(username, hash_password(password), "user")
            self.login_user(user["id"])
            return

        if path == "/api/reset-password" and method == "POST":
            raise PermissionError("Password reset is available only to admin in account settings.")
            return

        if path == "/api/logout" and method == "POST":
            token = self.session_token()
            if token:
                self.app.storage.delete_session(token)
            self.send_json(HTTPStatus.OK, {"ok": True}, cookies=[self.expired_cookie()], no_store=True)
            return

        user = self.current_user(required=True)
        actor_id = user["id"]
        is_admin = self.is_admin(user)

        if path == "/api/admin/users" and method == "GET":
            if not is_admin:
                raise PermissionError("Administrator access is required to manage user accounts.")
            users = [
                item
                for item in self.app.storage.list_users()
                if str(item.get("username", "")).lower() != ADMIN_USERNAME
            ]
            self.send_json(HTTPStatus.OK, {"users": users}, no_store=True)
            return

        if path.startswith("/api/admin/users/"):
            if not is_admin:
                raise PermissionError("Administrator access is required to manage user accounts.")
            parts = path.split("/")
            if len(parts) == 5 and method == "DELETE":
                user_id = int(parts[4])
                if user_id == int(user["id"]):
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "You cannot delete your own account."},
                        no_store=True,
                    )
                    return
                deleted = self.app.storage.delete_user_by_id(user_id, include_admin=False)
                if not deleted:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "User not found."},
                        no_store=True,
                    )
                    return
                self.send_json(HTTPStatus.OK, {"user": deleted}, no_store=True)
                return
            if len(parts) == 6 and parts[5] == "password" and method == "PUT":
                user_id = int(parts[4])
                data = self.read_json()
                password = self.require_text(data, "password", min_len=self.password_min_len("user"))
                updated = self.app.storage.update_user_password_by_id(
                    user_id,
                    hash_password(password),
                    include_admin=False,
                )
                if not updated:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "User not found."}, no_store=True)
                    return
                self.send_json(HTTPStatus.OK, {"user": updated}, no_store=True)
                return
            if len(parts) == 6 and parts[5] == "role" and method == "PUT":
                user_id = int(parts[4])
                data = self.read_json()
                role = self.require_text(data, "role")
                updated = self.app.storage.update_user_role_by_id(user_id, role)
                if not updated:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "User not found."}, no_store=True)
                    return
                self.send_json(HTTPStatus.OK, {"user": updated}, no_store=True)
                return

        if path == "/api/delete-requests" and method == "GET":
            self.send_json(
                HTTPStatus.OK,
                {"delete_requests": self.app.storage.list_delete_requests()},
                no_store=True,
            )
            return

        if path == "/api/delete-requests" and method == "POST":
            data = self.read_json()
            created = self.app.storage.create_delete_request(
                self.require_text(data, "target_type"),
                int(data.get("target_id") or 0),
                actor_id,
            )
            self.send_json(HTTPStatus.CREATED, {"delete_request": created}, no_store=True)
            return

        if path.startswith("/api/delete-requests/"):
            if not is_admin:
                raise PermissionError("Administrator access is required to process deletion requests.")
            parts = path.split("/")
            delete_request_id = int(parts[3])
            if len(parts) == 5 and parts[4] == "approve" and method == "POST":
                deleted = self.app.storage.approve_delete_request(delete_request_id)
                self.send_json(HTTPStatus.OK, {"deleted": deleted}, no_store=True)
                return
            if len(parts) == 4 and method == "DELETE":
                self.app.storage.dismiss_delete_request(delete_request_id)
                self.send_json(HTTPStatus.OK, {"ok": True}, no_store=True)
                return

        if path == "/api/catalog-state" and method == "GET":
            self.send_json(HTTPStatus.OK, self.app.storage.catalog_state(), no_store=True)
            return

        if path == "/api/collections" and method == "GET":
            self.send_json(HTTPStatus.OK, {"collections": self.app.storage.list_collections()})
            return

        if path == "/api/collections/reorder" and method == "PUT":
            ordered_ids = [
                int(item)
                for item in self.read_json().get("ordered_ids", [])
                if str(item).strip().isdigit()
            ]
            self.send_json(
                HTTPStatus.OK,
                {"collections": self.app.storage.reorder_collections(ordered_ids)},
            )
            return

        if path == "/api/collections" and method == "POST":
            created = self.app.storage.create_collection(self.read_json(), actor_id)
            self.send_json(HTTPStatus.CREATED, {"collection": created})
            return

        if path == "/api/tab-sets" and method == "GET":
            self.send_json(HTTPStatus.OK, {"tab_sets": self.app.storage.list_tab_sets(actor_id)})
            return

        if path == "/api/workspace" and method == "GET":
            self.send_json(HTTPStatus.OK, self.app.storage.get_user_workspace(actor_id), no_store=True)
            return

        if path == "/api/workspace" and method == "PUT":
            data = self.read_json()
            workspace = self.app.storage.save_user_workspace(
                actor_id,
                data.get("open_tabs", []),
                str(data.get("active_tab_key") or ""),
            )
            self.send_json(HTTPStatus.OK, workspace, no_store=True)
            return

        if path == "/api/onboarding" and method == "PUT":
            data = self.read_json()
            workspace = self.app.storage.set_onboarding_seen(actor_id, bool(data.get("seen", True)))
            self.send_json(
                HTTPStatus.OK,
                {"onboarding_seen": workspace["onboarding_seen"]},
                no_store=True,
            )
            return

        if path == "/api/tab-sets" and method == "POST":
            data = self.read_json()
            name = self.require_text(data, "name")
            request_ids = self.parse_request_ids(data)
            if len(request_ids) > 20:
                raise ValueError("Tab set can include at most 20 requests.")
            created = self.app.storage.create_tab_set(name, request_ids, actor_id)
            self.send_json(HTTPStatus.CREATED, {"tab_set": created})
            return

        if path.startswith("/api/tab-sets/"):
            parts = path.split("/")
            if len(parts) >= 4:
                tab_set_id = int(parts[3])
                if len(parts) == 4 and method == "PUT":
                    data = self.read_json()
                    updated = self.app.storage.update_tab_set(
                        tab_set_id, self.require_text(data, "name"), actor_id
                    )
                    if not updated:
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Tab set not found."})
                        return
                    self.send_json(HTTPStatus.OK, {"tab_set": updated})
                    return
                if len(parts) == 4 and method == "DELETE":
                    if not self.app.storage.get_tab_set(tab_set_id, actor_id):
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Tab set not found."})
                        return
                    self.app.storage.delete_tab_set(tab_set_id, actor_id)
                    self.send_json(HTTPStatus.OK, {"ok": True})
                    return
                if len(parts) == 5 and parts[4] == "requests" and method == "GET":
                    if not self.app.storage.get_tab_set(tab_set_id, actor_id):
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Tab set not found."})
                        return
                    self.send_json(
                        HTTPStatus.OK,
                        {"requests": self.app.storage.get_tab_set_requests(tab_set_id, actor_id)},
                    )
                    return
                if len(parts) == 5 and parts[4] == "requests" and method == "PUT":
                    request_ids = self.parse_request_ids(self.read_json())
                    if len(request_ids) > 20:
                        raise ValueError("Tab set can include at most 20 requests.")
                    if not self.app.storage.get_tab_set(tab_set_id, actor_id):
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Tab set not found."})
                        return
                    self.app.storage.replace_tab_set_items(tab_set_id, request_ids, actor_id)
                    self.send_json(HTTPStatus.OK, {"ok": True})
                    return

        if path.startswith("/api/collections/"):
            parts = path.split("/")
            if len(parts) >= 4:
                collection_id = int(parts[3])
                if len(parts) == 4 and method == "PUT":
                    updated = self.app.storage.update_collection(
                        collection_id, self.read_json(), actor_id
                    )
                    self.send_json(HTTPStatus.OK, {"collection": updated})
                    return
                if len(parts) == 4 and method == "DELETE":
                    if not is_admin:
                        raise PermissionError(
                            "Administrator access is required to delete collections."
                        )
                    self.app.storage.delete_collection(collection_id)
                    self.send_json(HTTPStatus.OK, {"ok": True})
                    return
                if len(parts) == 5 and parts[4] == "requests" and method == "GET":
                    self.send_json(
                        HTTPStatus.OK,
                        {"requests": self.app.storage.list_requests(collection_id)},
                    )
                    return
                if (
                    len(parts) == 6
                    and parts[4] == "requests"
                    and parts[5] == "reorder"
                    and method == "PUT"
                ):
                    ordered_ids = [
                        int(item)
                        for item in self.read_json().get("ordered_ids", [])
                        if str(item).strip().isdigit()
                    ]
                    self.send_json(
                        HTTPStatus.OK,
                        {"requests": self.app.storage.reorder_requests(collection_id, ordered_ids)},
                    )
                    return

        if path == "/api/requests" and method == "POST":
            data = self.read_json()
            data["use_bearer_token"] = False
            created = self.app.storage.create_request(data, actor_id)
            self.send_json(HTTPStatus.CREATED, {"request": created})
            return

        if path.startswith("/api/requests/"):
            request_id = int(path.split("/")[3])
            if method == "GET":
                request = self.app.storage.get_request(request_id)
                if not request:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Request not found."})
                    return
                self.send_json(HTTPStatus.OK, {"request": request})
                return
            if method == "PUT":
                existing = self.app.storage.get_request(request_id)
                if not existing:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Request not found."})
                    return
                data = self.read_json()
                data["use_bearer_token"] = False
                updated = self.app.storage.update_request(request_id, data, actor_id)
                self.send_json(HTTPStatus.OK, {"request": updated})
                return
            if method == "DELETE":
                if not is_admin:
                    raise PermissionError(
                        "Administrator access is required to delete requests."
                    )
                self.app.storage.delete_request(request_id)
                self.send_json(HTTPStatus.OK, {"ok": True})
                return

        if path == "/api/env" and method == "GET":
            self.send_json(HTTPStatus.OK, {"env": self.app.storage.get_env_vars()})
            return

        if path == "/api/env" and method == "PUT":
            updated = self.app.storage.replace_env_vars(self.read_json().get("env", {}), actor_id)
            self.send_json(HTTPStatus.OK, {"env": updated})
            return

        if path == "/api/file-history" and method == "GET":
            self.send_json(HTTPStatus.OK, {"items": self.app.storage.list_file_history()})
            return

        if path == "/api/file-history" and method == "POST":
            data = self.read_json()
            entry = self.app.storage.add_file_history_entry(
                data.get("file_id") or data.get("id"),
                data.get("name"),
                data.get("mime_type") or data.get("mimeType"),
                actor_id,
            )
            self.send_json(HTTPStatus.OK, {"item": entry})
            return

        if path == "/api/file-history" and method == "DELETE":
            self.app.storage.clear_file_history()
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        if path.startswith("/api/file-history/") and method == "DELETE":
            entry_id = path.removeprefix("/api/file-history/").strip("/")
            if not entry_id.isdigit():
                raise ValueError("A numeric file history id is required.")
            self.app.storage.delete_file_history_entry(int(entry_id))
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/execute" and method == "POST":
            data = self.read_json()
            execution_id = str(data.get("execution_id") or random_token()).strip()
            request = data.get("request")
            if data.get("request_id"):
                request = self.app.storage.get_request(int(data["request_id"]))
            if not request:
                raise ValueError("Request payload or request_id is required.")
            request["use_bearer_token"] = False
            bearer_token = str(request.get("auth_token") or "").strip() or None
            cancellation = self.app.start_execution(execution_id)
            try:
                result = execute_request(
                    request,
                    self.app.storage.get_env_vars_with_computed(),
                    bearer_token,
                    cancellation=cancellation,
                )
            finally:
                self.app.finish_execution(execution_id, cancellation)
            self.send_json(HTTPStatus.OK, {"result": result}, no_store=True)
            return

        if path.startswith("/api/executions/") and path.endswith("/cancel") and method == "POST":
            execution_id = path.removeprefix("/api/executions/").removesuffix("/cancel").strip("/")
            if not execution_id:
                raise ValueError("execution_id is required.")
            self.send_json(
                HTTPStatus.OK,
                {"cancelled": self.app.cancel_execution(execution_id)},
                no_store=True,
            )
            return

        if path == "/api/runs" and method == "GET":
            self.app.storage.clear_runs()
            self.send_json(HTTPStatus.OK, {"runs": []}, no_store=True)
            return

        if path == "/api/export" and method == "GET":
            if not is_admin:
                raise PermissionError("Administrator access is required to export collections.")
            self.send_json(HTTPStatus.OK, self.app.storage.export_data(), no_store=True)
            return

        if path == "/api/import" and method == "POST":
            if not is_admin:
                raise PermissionError("Administrator access is required to import collections.")
            imported = self.app.storage.import_data(self.read_json(), actor_id)
            self.send_json(HTTPStatus.OK, {"imported": imported})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint."})

    def login_user(self, user_id: int):
        token = random_token()
        self.app.storage.create_session(token, user_id, int(time.time()) + SESSION_SECONDS)
        user = self.app.storage.get_user(user_id)
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_SECONDS}{self.secure_cookie_suffix()}"
        )
        self.send_json(HTTPStatus.OK, {"user": user}, cookies=[cookie], no_store=True)

    def current_user(self, required: bool):
        token = self.session_token()
        user = self.app.storage.get_session_user(token) if token else None
        if user:
            return user
        if required:
            raise AuthenticationError("Authentication is required.")
        return None

    def is_admin(self, user: dict) -> bool:
        return str(user.get("role", "")).lower() == "admin"

    def password_min_len(self, username: str) -> int:
        return 12 if str(username or "").lower() == ADMIN_USERNAME else 6

    def normalize_username(self, value) -> str:
        username = str(value or "").strip()
        if len(username) < 2:
            raise ValueError("username must be at least 2 characters.")
        if len(username) > 64:
            raise ValueError("username must be at most 64 characters.")
        if any(ch.isspace() for ch in username):
            raise ValueError("username must not contain spaces.")
        return username

    def session_token(self) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie(cookie_header)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length.")
        if length > MAX_JSON_BODY_BYTES:
            raise PayloadTooLarge(
                f"Request payload exceeds the {MAX_JSON_BODY_BYTES // (1024 * 1024)} MB limit."
            )
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def require_text(self, data: dict, key: str, min_len: int = 1) -> str:
        value = str(data.get(key, "")).strip()
        if len(value) < min_len:
            raise ValueError(f"{key} must be at least {min_len} characters.")
        return value

    def parse_request_ids(self, data: dict) -> list[int]:
        request_ids = []
        for item in data.get("request_ids", []):
            try:
                request_id = int(item)
            except (TypeError, ValueError):
                continue
            if request_id > 0 and request_id not in request_ids:
                request_ids.append(request_id)
        return request_ids

    def serve_static(self, name: str):
        safe = Path(name).name if "/" not in name else Path(name)
        target = (STATIC_DIR / safe).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Static file not found."})
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, payload, cookies=None, no_store=False):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if no_store else "private, max-age=0")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def expired_cookie(self):
        return (
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age=0{self.secure_cookie_suffix()}"
        )

    def secure_cookie_suffix(self) -> str:
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        configured = os.environ.get("REQAPI_SECURE_COOKIES", "").lower()
        return "; Secure" if forwarded_proto == "https" or configured in {"1", "true", "yes"} else ""

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="REQAPI local-network API client")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--secret-key", default=str(DEFAULT_SECRET_KEY_PATH))
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), ReqApiHandler)
    server.app_state = AppState(Path(args.db), Path(args.secret_key))
    print(f"REQAPI is running on http://{args.host}:{args.port}")
    print("Allowed clients: localhost and 192.168.0.0/16; target DNS domains are unrestricted")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

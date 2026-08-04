from __future__ import annotations

import base64
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .security import load_fernet, token_hash


SECRET_PREFIX = "enc:v1:"


def now_ts() -> int:
    return int(time.time())


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def loads(value: str | None, default=None):
    if value in (None, ""):
        return [] if default is None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return [] if default is None else default


def auth_from_header(value: Any) -> dict[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    lower = raw.lower()
    if lower.startswith("bearer "):
        token = raw[7:].strip()
        return {"auth_type": "bearer", "auth_token": token} if token else {}
    if lower.startswith("basic "):
        try:
            decoded = base64.b64decode(raw[6:].strip(), validate=True).decode("utf-8")
        except Exception:
            return {}
        username, separator, password = decoded.partition(":")
        if separator:
            return {
                "auth_type": "basic",
                "basic_auth_username": username,
                "basic_auth_password": password,
            }
    return {}


def strip_authorization_headers(headers: Any) -> list[dict[str, Any]]:
    if isinstance(headers, dict):
        source_headers = [
            {"key": str(key), "value": str(value), "enabled": True}
            for key, value in headers.items()
        ]
    elif isinstance(headers, list):
        source_headers = [dict(item) for item in headers if isinstance(item, dict)]
    else:
        source_headers = []

    return [
        header
        for header in source_headers
        if str(header.get("key") or header.get("name") or "").strip().casefold()
        != "authorization"
    ]


def normalize_request_auth(data: dict[str, Any]) -> dict[str, Any]:
    headers = data.get("headers")
    if isinstance(headers, dict):
        source_headers = [
            {"key": str(key), "value": str(value), "enabled": True}
            for key, value in headers.items()
        ]
    elif isinstance(headers, list):
        source_headers = [dict(item) for item in headers if isinstance(item, dict)]
    else:
        source_headers = []

    authorization_value = None
    cleaned_headers = []
    for header in source_headers:
        key = str(header.get("key") or "")
        if key.strip().lower() == "authorization":
            if authorization_value is None and header.get("enabled", True) is not False:
                authorization_value = header.get("value") or ""
            continue
        cleaned_headers.append(header)

    auth_type = str(data.get("auth_type") or "bearer").strip().lower()
    if auth_type not in {"bearer", "basic"}:
        auth_type = "bearer"

    auth_token = str(data.get("auth_token") or "").strip()
    basic_username = str(data.get("basic_auth_username") or "")
    basic_password = str(data.get("basic_auth_password") or "")
    if not auth_token and not basic_username and not basic_password and authorization_value:
        parsed = auth_from_header(authorization_value)
        auth_type = parsed.get("auth_type", auth_type)
        auth_token = parsed.get("auth_token", auth_token)
        basic_username = parsed.get("basic_auth_username", basic_username)
        basic_password = parsed.get("basic_auth_password", basic_password)

    return {
        "headers": cleaned_headers,
        "auth_type": auth_type,
        "auth_token": auth_token,
        "basic_auth_username": basic_username,
        "basic_auth_password": basic_password,
    }


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_key_path = self.db_path.parent / ".secret-key"
        self.fernet = load_fernet(self.secret_key_path)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    method TEXT NOT NULL,
                    url TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '[]',
                    headers_json TEXT NOT NULL DEFAULT '[]',
                    cookies_json TEXT NOT NULL DEFAULT '[]',
                    body_type TEXT NOT NULL DEFAULT 'none',
                    body_text TEXT NOT NULL DEFAULT '',
                    form_json TEXT NOT NULL DEFAULT '[]',
                    pre_request_script TEXT NOT NULL DEFAULT '',
                    post_response_script TEXT NOT NULL DEFAULT '',
                    use_bearer_token INTEGER NOT NULL DEFAULT 0,
                    auth_token TEXT NOT NULL DEFAULT '',
                    auth_type TEXT NOT NULL DEFAULT 'bearer',
                    basic_auth_username TEXT NOT NULL DEFAULT '',
                    basic_auth_password TEXT NOT NULL DEFAULT '',
                    skip_tls_verification INTEGER NOT NULL DEFAULT 1,
                    position INTEGER,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS env_vars (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    method TEXT NOT NULL,
                    url TEXT NOT NULL,
                    request_headers_json TEXT NOT NULL DEFAULT '{}',
                    request_body TEXT,
                    status INTEGER,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    response_headers_json TEXT NOT NULL DEFAULT '{}',
                    response_body TEXT,
                    response_size INTEGER NOT NULL DEFAULT 0,
                    response_truncated INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    saved_response INTEGER NOT NULL DEFAULT 0,
                    run_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tab_sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tab_set_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tab_set_id INTEGER NOT NULL REFERENCES tab_sets(id) ON DELETE CASCADE,
                    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    UNIQUE(tab_set_id, request_id)
                );

                CREATE TABLE IF NOT EXISTS user_workspaces (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    open_tabs_json TEXT NOT NULL DEFAULT '[]',
                    active_tab_key TEXT NOT NULL DEFAULT '',
                    onboarding_seen INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delete_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_type TEXT NOT NULL CHECK(target_type IN ('collection', 'request')),
                    target_id INTEGER NOT NULL,
                    target_name TEXT NOT NULL,
                    requester_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    UNIQUE(target_type, target_id)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self.ensure_request_columns(conn)
            self.ensure_encrypted_request_credentials(conn)
            self.ensure_tls_default_migration(conn)
            self.ensure_collection_columns(conn)
            self.ensure_tab_set_schema(conn)
            self.ensure_user_workspace_columns(conn)
            conn.execute("DELETE FROM runs")

    def clear_runs(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runs")

    def ensure_request_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        if "skip_tls_verification" not in columns:
            conn.execute(
                    "ALTER TABLE requests ADD COLUMN skip_tls_verification INTEGER NOT NULL DEFAULT 1"
            )
        if "auth_token" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN auth_token TEXT NOT NULL DEFAULT ''")
        if "auth_type" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'bearer'")
        if "basic_auth_username" not in columns:
            conn.execute(
                "ALTER TABLE requests ADD COLUMN basic_auth_username TEXT NOT NULL DEFAULT ''"
            )
        if "basic_auth_password" not in columns:
            conn.execute(
                "ALTER TABLE requests ADD COLUMN basic_auth_password TEXT NOT NULL DEFAULT ''"
            )
        if "pre_request_script" not in columns:
            conn.execute(
                "ALTER TABLE requests ADD COLUMN pre_request_script TEXT NOT NULL DEFAULT ''"
            )
        if "post_response_script" not in columns:
            conn.execute(
                "ALTER TABLE requests ADD COLUMN post_response_script TEXT NOT NULL DEFAULT ''"
            )
        if "position" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN position INTEGER")
            collection_rows = conn.execute(
                "SELECT id FROM collections ORDER BY id"
            ).fetchall()
            for collection in collection_rows:
                request_rows = conn.execute(
                    """
                    SELECT id
                    FROM requests
                    WHERE collection_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (collection["id"],),
                ).fetchall()
                conn.executemany(
                    "UPDATE requests SET position = ? WHERE id = ?",
                    [(index, row["id"]) for index, row in enumerate(request_rows)],
                )
        conn.execute(
            """
            UPDATE requests
            SET position = id
            WHERE position IS NULL
            """
        )

    def encrypt_credential(self, value: Any) -> str:
        plaintext = str(value or "")
        if not plaintext or plaintext.startswith(SECRET_PREFIX):
            return plaintext
        encrypted = self.fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{SECRET_PREFIX}{encrypted}"

    def decrypt_credential(self, value: Any) -> str:
        stored = str(value or "")
        if not stored:
            return ""
        if not stored.startswith(SECRET_PREFIX):
            return stored
        try:
            return self.fernet.decrypt(
                stored[len(SECRET_PREFIX) :].encode("ascii")
            ).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(
                "Stored request credentials cannot be decrypted. "
                "Restore the matching data/.secret-key or REQAPI_SECRET_KEY."
            ) from exc

    def ensure_encrypted_request_credentials(self, conn: sqlite3.Connection) -> None:
        columns = ("auth_token", "basic_auth_username", "basic_auth_password")
        rows = conn.execute(
            f"SELECT id, {', '.join(columns)} FROM requests"
        ).fetchall()
        for row in rows:
            encrypted = tuple(self.encrypt_credential(row[column]) for column in columns)
            current = tuple(str(row[column] or "") for column in columns)
            if encrypted != current:
                conn.execute(
                    """
                    UPDATE requests
                    SET auth_token = ?, basic_auth_username = ?, basic_auth_password = ?
                    WHERE id = ?
                    """,
                    (*encrypted, row["id"]),
                )

    def ensure_tls_default_migration(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        migrated = conn.execute(
            """
            SELECT value
            FROM app_settings
            WHERE key = 'skip_tls_verification_enabled_by_default_v3'
            """
        ).fetchone()
        if migrated:
            return
        conn.execute("UPDATE requests SET skip_tls_verification = 1")
        conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ('skip_tls_verification_enabled_by_default_v3', '1')
            """
        )

    def ensure_collection_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(collections)")}
        if "position" not in columns:
            conn.execute("ALTER TABLE collections ADD COLUMN position INTEGER")
            rows = conn.execute(
                "SELECT id FROM collections ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            conn.executemany(
                "UPDATE collections SET position = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(rows)],
            )
        conn.execute(
            """
            UPDATE collections
            SET position = id
            WHERE position IS NULL
            """
        )

    def ensure_tab_set_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tab_sets'"
        ).fetchone()
        table_sql = str(row["sql"] or "") if row else ""
        if "name TEXT NOT NULL UNIQUE" in table_sql:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """
                CREATE TABLE tab_sets_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tab_sets_new (id, name, created_by, updated_by, created_at, updated_at)
                SELECT id, name, created_by, updated_by, created_at, updated_at
                FROM tab_sets
                """
            )
            conn.execute("DROP TABLE tab_sets")
            conn.execute("ALTER TABLE tab_sets_new RENAME TO tab_sets")
            conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tab_sets_user_name
            ON tab_sets(created_by, name)
            """
        )

    def ensure_user_workspace_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_workspaces)")}
        if "onboarding_seen" not in columns:
            conn.execute(
                "ALTER TABLE user_workspaces ADD COLUMN onboarding_seen INTEGER NOT NULL DEFAULT 0"
            )

    def setup_required(self) -> bool:
        with self.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'admin'"
            ).fetchone()[0]
            return count == 0

    def configure_single_admin(self, password_hash: str) -> dict[str, Any]:
        ts = now_ts()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE username = 'admin'"
            ).fetchone()
            if existing:
                user_id = existing["id"]
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, role = 'admin', updated_at = ?
                    WHERE id = ?
                    """,
                    (password_hash, ts, user_id),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, created_at, updated_at)
                    VALUES ('admin', ?, 'admin', ?, ?)
                    """,
                    (password_hash, ts, ts),
                )
                user_id = cur.lastrowid
        return self.get_user(user_id)

    def create_user(self, username: str, password_hash: str, role: str) -> dict[str, Any]:
        ts = now_ts()
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, password_hash, role, ts, ts),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("User already exists.") from exc
            user_id = cur.lastrowid
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return row_to_dict(row)

    def get_user_auth(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            return row_to_dict(row)

    def update_user_password(self, username: str, password_hash: str) -> dict[str, Any] | None:
        ts = now_ts()
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (password_hash, ts, row["id"]),
            )
            user_id = row["id"]
        return self.get_user(user_id)

    def update_user_password_by_id(
        self, user_id: int, password_hash: str, *, include_admin: bool = False
    ) -> dict[str, Any] | None:
        ts = now_ts()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            if not include_admin and str(row["username"]).lower() == "admin":
                return None
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (password_hash, ts, row["id"]),
            )
        return self.get_user(user_id)

    def update_user_role_by_id(self, user_id: int, role: str) -> dict[str, Any] | None:
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"admin", "user"}:
            raise ValueError("Role must be admin or user.")

        ts = now_ts()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            if str(row["username"]).lower() == "admin":
                raise ValueError("The primary admin account role cannot be changed.")
            conn.execute(
                """
                UPDATE users
                SET role = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_role, ts, row["id"]),
            )
        return self.get_user(user_id)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, created_at, updated_at FROM users ORDER BY username"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_user_by_id(
        self, user_id: int, *, include_admin: bool = False
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, role, created_at, updated_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            user = dict(row)
            if not include_admin and str(user["username"]).lower() == "admin":
                return None
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return user

    def get_user_workspace(self, user_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT open_tabs_json, active_tab_key, onboarding_seen
                FROM user_workspaces
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            items = loads(row["open_tabs_json"], default=[]) if row else []
            active_tab_key = row["active_tab_key"] if row else ""
            onboarding_seen = bool(row["onboarding_seen"]) if row else False
            request_ids = []
            for item in items:
                try:
                    request_id = int(item.get("request_id"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if request_id > 0 and request_id not in request_ids:
                    request_ids.append(request_id)
            requests_by_id: dict[int, dict[str, Any]] = {}
            if request_ids:
                placeholders = ",".join("?" for _ in request_ids)
                rows = conn.execute(
                    f"SELECT * FROM requests WHERE id IN ({placeholders})",
                    request_ids,
                ).fetchall()
                requests_by_id = {
                    row["id"]: self.decode_request(dict(row))
                    for row in rows
                }
            open_tabs = []
            for item in items:
                try:
                    request_id = int(item.get("request_id"))
                except (AttributeError, TypeError, ValueError):
                    continue
                request = requests_by_id.get(request_id)
                if not request:
                    continue
                tab_key = str(item.get("tab_key") or f"request-{request_id}")[:120]
                open_tabs.append({**request, "tabKey": tab_key})
            return {
                "open_tabs": open_tabs,
                "active_tab_key": active_tab_key,
                "onboarding_seen": onboarding_seen,
            }

    def save_user_workspace(
        self, user_id: int, open_tabs: list[dict[str, Any]], active_tab_key: str
    ) -> dict[str, Any]:
        cleaned = []
        request_ids = []
        for item in open_tabs:
            try:
                request_id = int(item.get("request_id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if request_id <= 0:
                continue
            tab_key = str(item.get("tab_key") or f"request-{request_id}")[:120]
            cleaned.append({"request_id": request_id, "tab_key": tab_key})
            if request_id not in request_ids:
                request_ids.append(request_id)
        existing_ids: set[int] = set()
        with self.connect() as conn:
            if request_ids:
                placeholders = ",".join("?" for _ in request_ids)
                rows = conn.execute(
                    f"SELECT id FROM requests WHERE id IN ({placeholders})",
                    request_ids,
                ).fetchall()
                existing_ids = {row["id"] for row in rows}
            cleaned = [item for item in cleaned if item["request_id"] in existing_ids]
            active = str(active_tab_key or "")
            if active and active not in {item["tab_key"] for item in cleaned}:
                active = cleaned[0]["tab_key"] if cleaned else ""
            conn.execute(
                """
                INSERT INTO user_workspaces (user_id, open_tabs_json, active_tab_key, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    open_tabs_json = excluded.open_tabs_json,
                    active_tab_key = excluded.active_tab_key,
                    updated_at = excluded.updated_at
                """,
                (user_id, dumps(cleaned), active, now_ts()),
            )
        return self.get_user_workspace(user_id)

    def set_onboarding_seen(self, user_id: int, seen: bool) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT open_tabs_json, active_tab_key FROM user_workspaces WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE user_workspaces
                    SET onboarding_seen = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (1 if seen else 0, now_ts(), user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_workspaces (
                        user_id, open_tabs_json, active_tab_key, onboarding_seen, updated_at
                    )
                    VALUES (?, '[]', '', ?, ?)
                    """,
                    (user_id, 1 if seen else 0, now_ts()),
                )
        return self.get_user_workspace(user_id)

    def create_session(self, raw_token: str, user_id: int, expires_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash(raw_token), user_id, expires_at, now_ts()),
            )

    def get_session_user(self, raw_token: str) -> dict[str, Any] | None:
        hashed = token_hash(raw_token)
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_ts(),))
            row = conn.execute(
                """
                SELECT users.id, users.username, users.role, users.created_at, users.updated_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at >= ?
                """,
                (hashed, now_ts()),
            ).fetchone()
            return row_to_dict(row)

    def delete_session(self, raw_token: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(raw_token),))

    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def list_collections(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT collections.*,
                       COUNT(requests.id) AS request_count
                FROM collections
                LEFT JOIN requests ON requests.collection_id = collections.id
                GROUP BY collections.id
                ORDER BY collections.position ASC, collections.id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def create_collection(self, data: dict[str, Any], user_id: int) -> dict[str, Any]:
        ts = now_ts()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO collections (
                    name, description, created_by, updated_by, created_at, updated_at, position
                )
                VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(position) + 1 FROM collections), 0))
                """,
                (
                    data.get("name") or "Untitled collection",
                    data.get("description") or "",
                    user_id,
                    user_id,
                    ts,
                    ts,
                ),
            )
            collection_id = cur.lastrowid
        return self.get_collection(collection_id)

    def get_collection(self, collection_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            return row_to_dict(row)

    def update_collection(self, collection_id: int, data: dict[str, Any], user_id: int):
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE collections
                SET name = ?, description = ?, updated_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data.get("name") or "Untitled collection",
                    data.get("description") or "",
                    user_id,
                    now_ts(),
                    collection_id,
                ),
            )
        return self.get_collection(collection_id)

    def delete_collection(self, collection_id: int) -> None:
        with self.connect() as conn:
            request_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM requests WHERE collection_id = ?", (collection_id,)
                ).fetchall()
            ]
            if request_ids:
                placeholders = ",".join("?" for _ in request_ids)
                conn.execute(
                    f"DELETE FROM delete_requests WHERE target_type = 'request' "
                    f"AND target_id IN ({placeholders})",
                    request_ids,
                )
            conn.execute(
                "DELETE FROM delete_requests WHERE target_type = 'collection' AND target_id = ?",
                (collection_id,),
            )
            conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def reorder_collections(self, ordered_ids: list[int]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            existing_ids = {
                row["id"] for row in conn.execute("SELECT id FROM collections").fetchall()
            }
            cleaned = []
            for collection_id in ordered_ids:
                if collection_id in existing_ids and collection_id not in cleaned:
                    cleaned.append(collection_id)
            missing = [collection_id for collection_id in existing_ids if collection_id not in cleaned]
            for index, collection_id in enumerate(cleaned + sorted(missing)):
                conn.execute(
                    "UPDATE collections SET position = ? WHERE id = ?",
                    (index, collection_id),
                )
        return self.list_collections()

    def list_tab_sets(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT tab_sets.*,
                       COUNT(tab_set_items.id) AS request_count
                FROM tab_sets
                LEFT JOIN tab_set_items ON tab_set_items.tab_set_id = tab_sets.id
                WHERE tab_sets.created_by = ?
                GROUP BY tab_sets.id
                ORDER BY tab_sets.name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_tab_set(self, tab_set_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM tab_sets WHERE id = ?", (tab_set_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tab_sets WHERE id = ? AND created_by = ?",
                    (tab_set_id, user_id),
                ).fetchone()
            return row_to_dict(row)

    def create_tab_set(self, name: str, request_ids: list[int], user_id: int) -> dict[str, Any]:
        ts = now_ts()
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO tab_sets (name, created_by, updated_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, user_id, user_id, ts, ts),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Tab set name already exists.") from exc
            tab_set_id = cur.lastrowid
            self.replace_tab_set_items_in_conn(conn, tab_set_id, request_ids)
        return self.get_tab_set(tab_set_id, user_id)

    def update_tab_set(self, tab_set_id: int, name: str, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    UPDATE tab_sets
                    SET name = ?, updated_by = ?, updated_at = ?
                    WHERE id = ? AND created_by = ?
                    """,
                    (name, user_id, now_ts(), tab_set_id, user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Tab set name already exists.") from exc
        return self.get_tab_set(tab_set_id, user_id)

    def delete_tab_set(self, tab_set_id: int, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM tab_sets WHERE id = ? AND created_by = ?", (tab_set_id, user_id)
            )

    def get_tab_set_requests(self, tab_set_id: int, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT requests.*
                FROM tab_set_items
                JOIN tab_sets ON tab_sets.id = tab_set_items.tab_set_id
                JOIN requests ON requests.id = tab_set_items.request_id
                WHERE tab_set_items.tab_set_id = ?
                  AND tab_sets.created_by = ?
                ORDER BY tab_set_items.position ASC, tab_set_items.id ASC
                """,
                (tab_set_id, user_id),
            ).fetchall()
            return [self.decode_request(dict(row)) for row in rows]

    def replace_tab_set_items(self, tab_set_id: int, request_ids: list[int], user_id: int) -> bool:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT id FROM tab_sets WHERE id = ? AND created_by = ?",
                (tab_set_id, user_id),
            ).fetchone()
            if not exists:
                return False
            self.replace_tab_set_items_in_conn(conn, tab_set_id, request_ids)
            conn.execute(
                "UPDATE tab_sets SET updated_by = ?, updated_at = ? WHERE id = ?",
                (user_id, now_ts(), tab_set_id),
            )
            return True

    def replace_tab_set_items_in_conn(
        self, conn: sqlite3.Connection, tab_set_id: int, request_ids: list[int]
    ) -> None:
        cleaned: list[int] = []
        for request_id in request_ids:
            if request_id not in cleaned:
                cleaned.append(request_id)
        existing_ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM requests WHERE id IN ({})".format(
                    ",".join("?" for _ in cleaned) or "NULL"
                ),
                cleaned,
            ).fetchall()
        }
        conn.execute("DELETE FROM tab_set_items WHERE tab_set_id = ?", (tab_set_id,))
        conn.executemany(
            """
            INSERT INTO tab_set_items (tab_set_id, request_id, position)
            VALUES (?, ?, ?)
            """,
            [
                (tab_set_id, request_id, index)
                for index, request_id in enumerate(cleaned)
                if request_id in existing_ids
            ],
        )

    def list_requests(self, collection_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE collection_id = ? ORDER BY position ASC, id ASC",
                (collection_id,),
            ).fetchall()
            return [self.decode_request(dict(row)) for row in rows]

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            return self.decode_request(dict(row)) if row else None

    def create_request(self, data: dict[str, Any], user_id: int) -> dict[str, Any]:
        ts = now_ts()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO requests (
                    collection_id, name, method, url, params_json, headers_json,
                    cookies_json, body_type, body_text, form_json, use_bearer_token,
                    auth_token, auth_type, basic_auth_username, basic_auth_password,
                    skip_tls_verification, pre_request_script, post_response_script,
                    position, created_by, updated_by, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(position) + 1 FROM requests WHERE collection_id = ?), 0),
                    ?, ?, ?, ?)
                """,
                self.request_values(data, user_id, created_at=None, updated_at=ts)
                + (int(data.get("collection_id")), user_id, user_id, ts, ts),
            )
            request_id = cur.lastrowid
        return self.get_request(request_id)

    def update_request(self, request_id: int, data: dict[str, Any], user_id: int):
        ts = now_ts()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE requests
                SET collection_id = ?, name = ?, method = ?, url = ?, params_json = ?,
                    headers_json = ?, cookies_json = ?, body_type = ?, body_text = ?,
                    form_json = ?, use_bearer_token = ?, auth_token = ?, auth_type = ?,
                    basic_auth_username = ?, basic_auth_password = ?, skip_tls_verification = ?,
                    pre_request_script = ?, post_response_script = ?,
                    updated_by = ?, updated_at = ?
                WHERE id = ?
                """,
                self.request_values(data, user_id, created_at=None, updated_at=ts)[:18]
                + (user_id, ts, request_id),
            )
        return self.get_request(request_id)

    def delete_request(self, request_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM delete_requests WHERE target_type = 'request' AND target_id = ?",
                (request_id,),
            )
            conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))

    def list_delete_requests(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.target_type, d.target_id, d.target_name,
                       d.requester_user_id, d.created_at,
                       u.username AS requester_username
                FROM delete_requests d
                JOIN users u ON u.id = d.requester_user_id
                ORDER BY d.created_at DESC, d.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_delete_request(
        self, target_type: str, target_id: int, requester_user_id: int
    ) -> dict[str, Any]:
        target_type = str(target_type).strip().lower()
        if target_type not in {"collection", "request"}:
            raise ValueError("Target type must be collection or request.")
        table = "collections" if target_type == "collection" else "requests"
        with self.connect() as conn:
            target = conn.execute(
                f"SELECT id, name FROM {table} WHERE id = ?", (target_id,)
            ).fetchone()
            if not target:
                raise ValueError(f"{target_type.title()} not found.")
            conn.execute(
                """
                INSERT INTO delete_requests
                    (target_type, target_id, target_name, requester_user_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_type, target_id) DO NOTHING
                """,
                (target_type, target_id, target["name"], requester_user_id, now_ts()),
            )
            row = conn.execute(
                """
                SELECT d.id, d.target_type, d.target_id, d.target_name,
                       d.requester_user_id, d.created_at,
                       u.username AS requester_username
                FROM delete_requests d
                JOIN users u ON u.id = d.requester_user_id
                WHERE d.target_type = ? AND d.target_id = ?
                """,
                (target_type, target_id),
            ).fetchone()
        return dict(row)

    def dismiss_delete_request(self, delete_request_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM delete_requests WHERE id = ?", (delete_request_id,)
            )
            if not cursor.rowcount:
                raise ValueError("Delete request not found.")

    def approve_delete_request(self, delete_request_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            item = conn.execute(
                "SELECT * FROM delete_requests WHERE id = ?", (delete_request_id,)
            ).fetchone()
            if not item:
                raise ValueError("Delete request not found.")
            result = dict(item)
            if item["target_type"] == "collection":
                request_ids = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM requests WHERE collection_id = ?",
                        (item["target_id"],),
                    ).fetchall()
                ]
                if request_ids:
                    placeholders = ",".join("?" for _ in request_ids)
                    conn.execute(
                        f"DELETE FROM delete_requests WHERE target_type = 'request' "
                        f"AND target_id IN ({placeholders})",
                        request_ids,
                    )
                conn.execute(
                    "DELETE FROM delete_requests "
                    "WHERE target_type = 'collection' AND target_id = ?",
                    (item["target_id"],),
                )
                conn.execute(
                    "DELETE FROM collections WHERE id = ?", (item["target_id"],)
                )
            else:
                conn.execute(
                    "DELETE FROM delete_requests "
                    "WHERE target_type = 'request' AND target_id = ?",
                    (item["target_id"],),
                )
                conn.execute("DELETE FROM requests WHERE id = ?", (item["target_id"],))
        return result

    def catalog_state(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as conn:
            collections = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, updated_at FROM collections ORDER BY id"
                ).fetchall()
            ]
            requests = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, collection_id, updated_at FROM requests ORDER BY id"
                ).fetchall()
            ]
        return {"collections": collections, "requests": requests}

    def reorder_requests(self, collection_id: int, ordered_ids: list[int]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            existing_rows = conn.execute(
                "SELECT id FROM requests WHERE collection_id = ? ORDER BY position ASC, id ASC",
                (collection_id,),
            ).fetchall()
            existing_ids = [row["id"] for row in existing_rows]
            cleaned = [request_id for request_id in ordered_ids if request_id in existing_ids]
            cleaned += [request_id for request_id in existing_ids if request_id not in cleaned]
            conn.executemany(
                "UPDATE requests SET position = ?, updated_at = ? WHERE id = ? AND collection_id = ?",
                [
                    (index, now_ts(), request_id, collection_id)
                    for index, request_id in enumerate(cleaned)
                ],
            )
        return self.list_requests(collection_id)

    def request_values(self, data, user_id, created_at, updated_at):
        auth = normalize_request_auth(data)
        use_bearer = 1 if data.get("use_bearer_token") else 0
        skip_tls = 1 if data.get("skip_tls_verification", True) is True else 0
        auth_type = auth["auth_type"]
        if auth_type not in {"bearer", "basic"}:
            auth_type = "bearer"
        legacy_form = data.get("form") if isinstance(data.get("form"), list) else []
        body_type = data.get("body_type") or "none"
        form_payload = {
            "form_data": data.get("form_data")
            if isinstance(data.get("form_data"), list)
            else legacy_form if body_type == "form-data" else [],
            "urlencoded": data.get("urlencoded")
            if isinstance(data.get("urlencoded"), list)
            else legacy_form if body_type == "form" else [],
            "binary": data.get("binary") if isinstance(data.get("binary"), dict) else {},
            "graphql": data.get("graphql") if isinstance(data.get("graphql"), dict) else {},
        }
        values = (
            int(data.get("collection_id")),
            data.get("name") or "Untitled request",
            (data.get("method") or "GET").upper(),
            data.get("url") or "http://localhost:8000/",
            dumps(data.get("params")),
            dumps(auth["headers"]),
            dumps(data.get("cookies")),
            body_type,
            data.get("body_text") or "",
            dumps(form_payload),
            use_bearer,
            self.encrypt_credential(auth["auth_token"]),
            auth_type,
            self.encrypt_credential(auth["basic_auth_username"]),
            self.encrypt_credential(auth["basic_auth_password"]),
            skip_tls,
            data.get("pre_request_script") or "",
            data.get("post_response_script") or "",
        )
        if created_at is None:
            return values
        return values + (user_id, user_id, created_at, updated_at)

    def decode_request(self, row: dict[str, Any]) -> dict[str, Any]:
        row["params"] = loads(row.pop("params_json", None))
        row["headers"] = loads(row.pop("headers_json", None))
        row["cookies"] = loads(row.pop("cookies_json", None))
        stored_form = loads(row.pop("form_json", None))
        if isinstance(stored_form, dict):
            row["form_data"] = stored_form.get("form_data") or []
            row["urlencoded"] = stored_form.get("urlencoded") or []
            row["binary"] = stored_form.get("binary") or {}
            row["graphql"] = stored_form.get("graphql") or {}
        else:
            legacy_form = stored_form if isinstance(stored_form, list) else []
            row["form_data"] = legacy_form if row.get("body_type") == "form-data" else []
            row["urlencoded"] = legacy_form if row.get("body_type") == "form" else []
            row["binary"] = {}
            row["graphql"] = {}
        row["form"] = row["form_data"] if row.get("body_type") == "form-data" else row["urlencoded"]
        row["auth_token"] = self.decrypt_credential(row.get("auth_token"))
        row["basic_auth_username"] = self.decrypt_credential(
            row.get("basic_auth_username")
        )
        row["basic_auth_password"] = self.decrypt_credential(
            row.get("basic_auth_password")
        )
        auth = normalize_request_auth(row)
        row["headers"] = auth["headers"]
        row["use_bearer_token"] = bool(row.get("use_bearer_token"))
        row["auth_type"] = auth["auth_type"]
        if row["auth_type"] not in {"bearer", "basic"}:
            row["auth_type"] = "bearer"
        row["auth_token"] = auth["auth_token"]
        row["basic_auth_username"] = auth["basic_auth_username"]
        row["basic_auth_password"] = auth["basic_auth_password"]
        row["pre_request_script"] = row.get("pre_request_script") or ""
        row["post_response_script"] = row.get("post_response_script") or ""
        row["skip_tls_verification"] = bool(row.get("skip_tls_verification"))
        row["position"] = int(row.get("position") or 0)
        return row

    def get_env_vars(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM env_vars ORDER BY key").fetchall()
            return {row["key"]: row["value"] for row in rows}

    def replace_env_vars(self, values: dict[str, str], user_id: int) -> dict[str, str]:
        cleaned = {
            str(key).strip(): str(value)
            for key, value in values.items()
            if str(key).strip()
        }
        with self.connect() as conn:
            conn.execute("DELETE FROM env_vars")
            conn.executemany(
                "INSERT INTO env_vars (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
                [(key, value, user_id, now_ts()) for key, value in cleaned.items()],
            )
        return self.get_env_vars()

    def export_data(self) -> dict[str, Any]:
        collections = self.list_collections()
        for collection in collections:
            collection["requests"] = self.list_requests(collection["id"])
            for request in collection["requests"]:
                request.pop("created_by", None)
                request.pop("updated_by", None)
                request.pop("created_at", None)
                request.pop("updated_at", None)
                request["use_bearer_token"] = False
                request["auth_token"] = ""
                request["basic_auth_username"] = ""
                request["basic_auth_password"] = ""
                request["headers"] = strip_authorization_headers(request.get("headers"))
        return {
            "format": "reqapi.collection.v1",
            "collections": collections,
            "env": {},
        }

    def import_data(self, payload: dict[str, Any], user_id: int) -> dict[str, Any]:
        imported = {"collections": 0, "requests": 0}
        for collection in payload.get("collections", []):
            created = self.create_collection(
                {
                    "name": collection.get("name") or "Imported collection",
                    "description": collection.get("description") or "",
                },
                user_id,
            )
            imported["collections"] += 1
            for request in collection.get("requests", []):
                safe_request = dict(request)
                safe_request["collection_id"] = created["id"]
                safe_request["use_bearer_token"] = False
                safe_request["auth_token"] = ""
                safe_request["basic_auth_username"] = ""
                safe_request["basic_auth_password"] = ""
                safe_request["headers"] = strip_authorization_headers(
                    safe_request.get("headers")
                )
                self.create_request(safe_request, user_id)
                imported["requests"] += 1
        return imported

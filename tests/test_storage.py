import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reqapi.storage import Storage


class StorageWorkspaceTests(unittest.TestCase):
    def test_new_requests_skip_tls_verification_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "tls"}, admin["id"])

            default_request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Default TLS",
                    "method": "GET",
                    "url": "https://localhost/",
                },
                admin["id"],
            )
            verified_request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Verify TLS",
                    "method": "GET",
                    "url": "https://localhost/",
                    "skip_tls_verification": False,
                },
                admin["id"],
            )

            self.assertTrue(storage.get_request(default_request["id"])["skip_tls_verification"])
            self.assertFalse(storage.get_request(verified_request["id"])["skip_tls_verification"])

    def test_existing_requests_enable_skip_tls_verification_once_on_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reqapi.sqlite3"
            storage = Storage(db_path)
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "tls"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Existing request",
                    "method": "GET",
                    "url": "https://localhost/",
                    "skip_tls_verification": False,
                },
                admin["id"],
            )

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    DELETE FROM app_settings
                    WHERE key = 'skip_tls_verification_enabled_by_default_v3'
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO app_settings (key, value)
                    VALUES ('tls_verification_enabled_v2', '1')
                    """
                )

            migrated = Storage(db_path)
            self.assertTrue(migrated.get_request(request["id"])["skip_tls_verification"])

            migrated.update_request(
                request["id"],
                {
                    **migrated.get_request(request["id"]),
                    "skip_tls_verification": False,
                },
                admin["id"],
            )
            reopened = Storage(db_path)
            self.assertFalse(reopened.get_request(request["id"])["skip_tls_verification"])

    def test_form_data_file_is_saved_with_its_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "uploads"}, admin["id"])
            file_bytes = b"%PDF-1.4\nreqapi-test\n"
            encoded = base64.b64encode(file_bytes).decode("ascii")

            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Upload PDF",
                    "method": "POST",
                    "url": "https://api.example.test/api/filestorage/file/temp",
                    "body_type": "form-data",
                    "form": [
                        {
                            "key": "data",
                            "type": "file",
                            "file_name": "test.pdf",
                            "file_type": "application/pdf",
                            "file_size": len(file_bytes),
                            "file_base64": encoded,
                            "enabled": True,
                        },
                        {
                            "key": "mimeType",
                            "type": "text",
                            "value": "application/pdf",
                            "enabled": True,
                        },
                    ],
                },
                admin["id"],
            )

            loaded = storage.get_request(request["id"])
            self.assertEqual(loaded["form"][0]["file_name"], "test.pdf")
            self.assertEqual(loaded["form"][0]["file_base64"], encoded)
            self.assertEqual(base64.b64decode(loaded["form"][0]["file_base64"]), file_bytes)

    def test_body_modes_are_persisted_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "bodies"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Independent bodies",
                    "method": "POST",
                    "url": "http://localhost:8000/",
                    "body_type": "form",
                    "form_data": [{"key": "file", "type": "file", "file_name": "a.pdf"}],
                    "urlencoded": [{"key": "name", "value": "a.pdf", "type": "text"}],
                    "binary": {"file_name": "payload.bin", "file_base64": "YQ=="},
                    "graphql": {"query": "query { ping }", "variables": "{}"},
                },
                admin["id"],
            )

            loaded = storage.get_request(request["id"])
            self.assertEqual(loaded["form_data"][0]["file_name"], "a.pdf")
            self.assertEqual(loaded["urlencoded"][0]["value"], "a.pdf")
            self.assertEqual(loaded["binary"]["file_name"], "payload.bin")
            self.assertEqual(loaded["graphql"]["query"], "query { ping }")

    def test_imported_authorization_header_becomes_request_auth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "shared"}, admin["id"])

            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Imported",
                    "method": "GET",
                    "url": "http://localhost:8000/api",
                    "headers": [
                        {
                            "key": "Authorization",
                            "value": "Bearer imported-token",
                            "enabled": True,
                        },
                        {"key": "X-Test", "value": "ok", "enabled": True},
                    ],
                },
                admin["id"],
            )

            self.assertEqual(request["auth_type"], "bearer")
            self.assertEqual(request["auth_token"], "imported-token")
            self.assertEqual([item["key"] for item in request["headers"]], ["X-Test"])

    def test_workspace_and_tab_sets_are_per_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            user = storage.create_user("alex", "hash-alex", "user")
            collection = storage.create_collection({"name": "shared"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Shared request",
                    "method": "GET",
                    "url": "http://localhost:8000/api",
                },
                admin["id"],
            )

            storage.save_user_workspace(
                admin["id"],
                [{"request_id": request["id"], "tab_key": "request-admin"}],
                "request-admin",
            )
            storage.create_tab_set("Admin set", [request["id"]], admin["id"])

            user_workspace = storage.get_user_workspace(user["id"])
            self.assertEqual(user_workspace["open_tabs"], [])
            self.assertEqual(user_workspace["active_tab_key"], "")
            self.assertEqual(storage.list_tab_sets(user["id"]), [])

            storage.save_user_workspace(
                user["id"],
                [{"request_id": request["id"], "tab_key": "request-user"}],
                "request-user",
            )
            storage.create_tab_set("Alex set", [request["id"]], user["id"])

            admin_workspace = storage.get_user_workspace(admin["id"])
            user_workspace = storage.get_user_workspace(user["id"])
            self.assertEqual(admin_workspace["active_tab_key"], "request-admin")
            self.assertEqual(user_workspace["active_tab_key"], "request-user")
            self.assertEqual([item["name"] for item in storage.list_tab_sets(admin["id"])], ["Admin set"])
            self.assertEqual([item["name"] for item in storage.list_tab_sets(user["id"])], ["Alex set"])

    def test_requests_can_be_reordered_inside_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "shared"}, admin["id"])
            first = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "First",
                    "method": "GET",
                    "url": "http://localhost:8000/first",
                },
                admin["id"],
            )
            second = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Second",
                    "method": "GET",
                    "url": "http://localhost:8000/second",
                },
                admin["id"],
            )

            self.assertEqual(
                [item["name"] for item in storage.list_requests(collection["id"])],
                ["First", "Second"],
            )

            storage.reorder_requests(collection["id"], [second["id"], first["id"]])

            self.assertEqual(
                [item["name"] for item in storage.list_requests(collection["id"])],
                ["Second", "First"],
            )

    def test_delete_request_queue_records_requester_and_can_be_dismissed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            user = storage.create_user("alex", "hash-alex", "user")
            collection = storage.create_collection({"name": "shared"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Obsolete request",
                    "method": "GET",
                    "url": "http://localhost:8000/obsolete",
                },
                user["id"],
            )

            queued = storage.create_delete_request("request", request["id"], user["id"])

            self.assertEqual(queued["target_type"], "request")
            self.assertEqual(queued["target_id"], request["id"])
            self.assertEqual(queued["target_name"], "Obsolete request")
            self.assertEqual(queued["requester_username"], "alex")
            self.assertEqual(storage.list_delete_requests(), [queued])

            storage.dismiss_delete_request(queued["id"])

            self.assertEqual(storage.list_delete_requests(), [])
            self.assertEqual(storage.get_request(request["id"])["name"], "Obsolete request")

    def test_approving_request_deletion_removes_request_and_queue_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "shared"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Delete me",
                    "method": "DELETE",
                    "url": "http://localhost:8000/item",
                },
                admin["id"],
            )
            queued = storage.create_delete_request("request", request["id"], admin["id"])

            approved = storage.approve_delete_request(queued["id"])

            self.assertEqual(approved["target_id"], request["id"])
            self.assertIsNone(storage.get_request(request["id"]))
            self.assertEqual(storage.list_delete_requests(), [])

    def test_approving_collection_deletion_removes_children_and_related_queue_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            user = storage.create_user("alex", "hash-alex", "user")
            collection = storage.create_collection({"name": "obsolete"}, user["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Child request",
                    "method": "GET",
                    "url": "http://localhost:8000/child",
                },
                user["id"],
            )
            storage.create_delete_request("request", request["id"], user["id"])
            collection_queue = storage.create_delete_request(
                "collection", collection["id"], user["id"]
            )

            storage.approve_delete_request(collection_queue["id"])

            self.assertIsNone(storage.get_collection(collection["id"]))
            self.assertIsNone(storage.get_request(request["id"]))
            self.assertEqual(storage.list_delete_requests(), [])

    def test_duplicate_deletion_request_keeps_original_requester(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            first_user = storage.create_user("alex", "hash-alex", "user")
            second_user = storage.create_user("evgeny", "hash-evgeny", "user")
            collection = storage.create_collection({"name": "shared"}, first_user["id"])

            first = storage.create_delete_request(
                "collection", collection["id"], first_user["id"]
            )
            duplicate = storage.create_delete_request(
                "collection", collection["id"], second_user["id"]
            )

            self.assertEqual(duplicate["id"], first["id"])
            self.assertEqual(duplicate["requester_username"], "alex")
            self.assertEqual(len(storage.list_delete_requests()), 1)

    def test_deletion_request_rejects_unknown_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            user = storage.create_user("alex", "hash-alex", "user")

            with self.assertRaisesRegex(ValueError, "Request not found"):
                storage.create_delete_request("request", 99999, user["id"])

            with self.assertRaisesRegex(
                ValueError, "Target type must be collection or request"
            ):
                storage.create_delete_request("workspace", 1, user["id"])

    def test_direct_request_deletion_removes_pending_queue_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "shared"}, admin["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Delete directly",
                    "method": "GET",
                    "url": "http://localhost:8000/item",
                },
                admin["id"],
            )
            storage.create_delete_request("request", request["id"], admin["id"])

            storage.delete_request(request["id"])

            self.assertIsNone(storage.get_request(request["id"]))
            self.assertEqual(storage.list_delete_requests(), [])

    def test_request_credentials_are_encrypted_at_rest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reqapi.sqlite3"
            storage = Storage(db_path)
            user = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "secure"}, user["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Protected request",
                    "method": "GET",
                    "url": "https://example.test/private",
                    "auth_type": "basic",
                    "auth_token": "bearer-secret",
                    "basic_auth_username": "service-user",
                    "basic_auth_password": "service-password",
                },
                user["id"],
            )

            with sqlite3.connect(db_path) as conn:
                stored = conn.execute(
                    """
                    SELECT auth_token, basic_auth_username, basic_auth_password
                    FROM requests WHERE id = ?
                    """,
                    (request["id"],),
                ).fetchone()

            self.assertTrue(all(value.startswith("enc:v1:") for value in stored))
            self.assertNotIn(b"bearer-secret", db_path.read_bytes())
            loaded = storage.get_request(request["id"])
            self.assertEqual(loaded["auth_token"], "bearer-secret")
            self.assertEqual(loaded["basic_auth_username"], "service-user")
            self.assertEqual(loaded["basic_auth_password"], "service-password")

    def test_plaintext_request_credentials_are_migrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "reqapi.sqlite3"
            storage = Storage(db_path)
            user = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "legacy"}, user["id"])
            request = storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Legacy request",
                    "method": "GET",
                    "url": "https://example.test",
                },
                user["id"],
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE requests
                    SET auth_token = ?, basic_auth_username = ?, basic_auth_password = ?
                    WHERE id = ?
                    """,
                    ("legacy-token", "legacy-user", "legacy-password", request["id"]),
                )

            migrated = Storage(db_path)
            loaded = migrated.get_request(request["id"])
            self.assertEqual(loaded["auth_token"], "legacy-token")
            with sqlite3.connect(db_path) as conn:
                stored = conn.execute(
                    "SELECT auth_token FROM requests WHERE id = ?",
                    (request["id"],),
                ).fetchone()[0]
            self.assertTrue(stored.startswith("enc:v1:"))

    def test_export_excludes_credentials_authorization_headers_and_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            user = storage.create_user("admin", "hash-admin", "admin")
            collection = storage.create_collection({"name": "export"}, user["id"])
            storage.replace_env_vars({"baseUrl": "https://secret.example"}, user["id"])
            storage.create_request(
                {
                    "collection_id": collection["id"],
                    "name": "Secret request",
                    "method": "GET",
                    "url": "https://example.test",
                    "use_bearer_token": True,
                    "auth_token": "secret-token",
                    "basic_auth_username": "secret-user",
                    "basic_auth_password": "secret-password",
                    "headers": [
                        {"key": "Authorization", "value": "Bearer header-secret"},
                        {"key": "Accept", "value": "application/json"},
                    ],
                },
                user["id"],
            )

            exported = storage.export_data()
            serialized = json.dumps(exported)
            request = exported["collections"][0]["requests"][0]

            self.assertEqual(exported["env"], {})
            self.assertFalse(request["use_bearer_token"])
            self.assertEqual(request["auth_token"], "")
            self.assertEqual(request["basic_auth_username"], "")
            self.assertEqual(request["basic_auth_password"], "")
            self.assertEqual(
                request["headers"],
                [{"key": "Accept", "value": "application/json"}],
            )
            for secret in (
                "secret-token",
                "secret-user",
                "secret-password",
                "header-secret",
                "secret.example",
            ):
                self.assertNotIn(secret, serialized)

    def test_user_role_can_be_promoted_and_demoted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            user = storage.create_user("alex", "hash-alex", "user")

            promoted = storage.update_user_role_by_id(user["id"], "admin")
            self.assertEqual(promoted["role"], "admin")
            self.assertEqual(storage.get_user(user["id"])["role"], "admin")

            demoted = storage.update_user_role_by_id(user["id"], "user")
            self.assertEqual(demoted["role"], "user")
            self.assertEqual(storage.get_user(user["id"])["role"], "user")

    def test_primary_admin_role_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            admin = storage.create_user("admin", "hash-admin", "admin")

            with self.assertRaisesRegex(
                ValueError,
                "primary admin account role cannot be changed",
            ):
                storage.update_user_role_by_id(admin["id"], "user")

            self.assertEqual(storage.get_user(admin["id"])["role"], "admin")

    def test_import_discards_credentials_authorization_headers_and_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(Path(tmpdir) / "reqapi.sqlite3")
            user = storage.create_user("admin", "hash-admin", "admin")
            payload = {
                "collections": [
                    {
                        "name": "Imported",
                        "requests": [
                            {
                                "name": "Unsafe",
                                "method": "GET",
                                "url": "https://example.test",
                                "use_bearer_token": True,
                                "auth_token": "imported-token",
                                "basic_auth_username": "imported-user",
                                "basic_auth_password": "imported-password",
                                "headers": [
                                    {"key": "authorization", "value": "Bearer imported-header"},
                                    {"key": "Accept", "value": "application/json"},
                                ],
                            }
                        ],
                    }
                ],
                "env": {"baseUrl": "https://imported-secret.example"},
            }

            storage.import_data(payload, user["id"])
            request = storage.list_requests(storage.list_collections()[0]["id"])[0]

            self.assertEqual(storage.get_env_vars(), {})
            self.assertFalse(request["use_bearer_token"])
            self.assertEqual(request["auth_token"], "")
            self.assertEqual(request["basic_auth_username"], "")
            self.assertEqual(request["basic_auth_password"], "")
            self.assertEqual(request["headers"], [{"key": "Accept", "value": "application/json"}])


if __name__ == "__main__":
    unittest.main()

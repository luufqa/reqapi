import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from reqapi.http_client import build_body, build_headers, build_url, execute_request
from reqapi.security import SecurityError


class HttpClientTests(unittest.TestCase):
    def test_build_url_uses_enabled_params_and_preserves_repeated_keys(self):
        url = build_url(
            "https://example.test/items?existing=1",
            [
                {"enabled": True, "key": "tag", "value": "one"},
                {"enabled": True, "key": "tag", "value": "two"},
                {"enabled": False, "key": "hidden", "value": "no"},
                {"enabled": True, "key": "search", "value": "api test"},
            ],
            {},
        )

        self.assertEqual(
            url,
            "https://example.test/items?existing=1&tag=one&tag=two&search=api+test",
        )

    def test_authorization_header_is_not_sent_from_regular_headers(self):
        headers = build_headers(
            {
                "headers": [
                    {"key": "Authorization", "value": "Bearer hidden-token", "enabled": True},
                    {"key": "X-Trace", "value": "ok", "enabled": True},
                ],
                "auth_type": "bearer",
                "auth_token": "",
            },
            {},
            None,
        )

        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["X-Trace"], "ok")

    def test_authorization_header_is_sent_only_from_request_auth(self):
        headers = build_headers(
            {
                "headers": [
                    {"key": "Authorization", "value": "Bearer hidden-token", "enabled": True},
                ],
                "auth_type": "bearer",
                "auth_token": "saved-token",
            },
            {},
            None,
        )

        self.assertEqual(headers["Authorization"], "Bearer saved-token")

    def test_skip_tls_verification_rejects_non_allowlisted_targets(self):
        with self.assertRaises(SecurityError):
            execute_request(
                {
                    "method": "GET",
                    "url": "https://192.168.1.10/api",
                    "skip_tls_verification": True,
                },
                {},
            )

    def test_form_data_includes_text_and_binary_file_parts(self):
        headers = {}
        pdf_bytes = b"%PDF-1.4\nreqapi-test\n%%EOF"
        body = build_body(
            {
                "body_type": "form-data",
                "form": [
                    {
                        "key": "data",
                        "type": "file",
                        "file_name": "test.pdf",
                        "file_type": "application/pdf",
                        "file_size": len(pdf_bytes),
                        "file_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                        "enabled": True,
                    },
                    {"key": "mimeType", "value": "application/pdf", "type": "text", "enabled": True},
                    {"key": "name", "value": "test.pdf", "type": "text", "enabled": True},
                ],
            },
            headers,
            {},
        )

        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="data"; filename="test.pdf"', body)
        self.assertIn(b"Content-Type: application/pdf", body)
        self.assertIn(pdf_bytes, body)
        self.assertIn(b'name="mimeType"', body)
        self.assertIn(b"application/pdf", body)

    def test_form_data_requires_selected_file(self):
        with self.assertRaisesRegex(ValueError, "No file is selected"):
            build_body(
                {
                    "body_type": "form-data",
                    "form": [{"key": "data", "type": "file", "enabled": True}],
                },
                {},
                {},
            )

    def test_urlencoded_uses_its_own_text_fields(self):
        headers = {}
        body = build_body(
            {
                "body_type": "form",
                "form_data": [
                    {
                        "key": "data",
                        "type": "file",
                        "file_name": "ignored.pdf",
                        "file_base64": base64.b64encode(b"ignored").decode("ascii"),
                        "enabled": True,
                    }
                ],
                "urlencoded": [
                    {"key": "name", "value": "test.pdf", "enabled": True},
                    {"key": "mimeType", "value": "application/pdf", "enabled": True},
                ],
            },
            headers,
            {},
        )

        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(body, b"name=test.pdf&mimeType=application%2Fpdf")
        self.assertNotIn(b"ignored", body)

    def test_binary_body_contains_selected_file(self):
        headers = {}
        content = b"binary-content"
        body = build_body(
            {
                "body_type": "binary",
                "binary": {
                    "file_name": "payload.bin",
                    "file_type": "application/octet-stream",
                    "file_base64": base64.b64encode(content).decode("ascii"),
                },
            },
            headers,
            {},
        )

        self.assertEqual(body, content)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")

    def test_graphql_body_contains_query_and_variables(self):
        headers = {}
        body = build_body(
            {
                "body_type": "graphql",
                "graphql": {
                    "query": "query User($id: ID!) { user(id: $id) { name } }",
                    "variables": '{"id": "42"}',
                },
            },
            headers,
            {},
        )

        decoded = __import__("json").loads(body)
        self.assertEqual(decoded["variables"], {"id": "42"})
        self.assertIn("query User", decoded["query"])
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_graphql_variables_must_be_a_json_object(self):
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            build_body(
                {
                    "body_type": "graphql",
                    "graphql": {"query": "query { ping }", "variables": "[]"},
                },
                {},
                {},
            )

    def test_graphql_request_is_sent_as_json(self):
        received = {}

        class GraphqlHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received["content_type"] = self.headers.get("Content-Type")
                received["body"] = json.loads(self.rfile.read(length))
                response = json.dumps({"data": {"ping": "pong"}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), GraphqlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = execute_request(
                {
                    "method": "POST",
                    "url": f"http://127.0.0.1:{server.server_port}/graphql",
                    "body_type": "graphql",
                    "graphql": {
                        "query": "query Ping { ping }",
                        "variables": '{"enabled": true}',
                    },
                },
                {},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["response_body_base64"], "")
        self.assertEqual(received["content_type"], "application/json")
        self.assertEqual(received["body"]["query"], "query Ping { ping }")
        self.assertEqual(received["body"]["variables"], {"enabled": True})

    def test_binary_file_response_includes_base64_body(self):
        pdf_bytes = b"%PDF-1.4\nbinary-response\n%%EOF"

        class FileHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="report.pdf"')
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), FileHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = execute_request(
                {
                    "method": "GET",
                    "url": f"http://127.0.0.1:{server.server_port}/report",
                },
                {},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["status"], 200)
        self.assertEqual(base64.b64decode(result["response_body_base64"]), pdf_bytes)


if __name__ == "__main__":
    unittest.main()

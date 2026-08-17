from __future__ import annotations

import http.client
import base64
import binascii
import json
import ssl
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .security import (
    SecurityError,
    can_skip_tls_verification,
    is_blocked_header,
    redact_headers,
    render_template,
    validate_target_url,
)


MAX_RESPONSE_DISPLAY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20
METHODS_WITHOUT_BODY = {"HEAD"}
MAX_FORM_FILE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024
FILE_RESPONSE_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
}


class RequestCancelled(Exception):
    pass


class RequestCancellation:
    def __init__(self):
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._connection = None

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            connection = self._connection
        if connection:
            try:
                connection.close()
            except OSError:
                pass

    def attach(self, connection):
        with self._lock:
            self._connection = connection
        self.check()

    def detach(self, connection):
        with self._lock:
            if self._connection is connection:
                self._connection = None

    def check(self):
        if self._cancelled.is_set():
            raise RequestCancelled("Request cancelled.")


class PinnedHTTPSConnection(http.client.HTTPConnection):
    default_port = 443

    def __init__(self, connect_host, port, timeout, context, tls_server_hostname):
        super().__init__(connect_host, port=port, timeout=timeout)
        self._context = context
        self._tls_server_hostname = tls_server_hostname

    def connect(self):
        super().connect()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tls_server_hostname
        )


def normalize_pairs(value):
    if isinstance(value, dict):
        return [
            {"key": str(key), "value": str(val), "enabled": True}
            for key, val in value.items()
        ]
    if isinstance(value, list):
        pairs = []
        for item in value:
            if isinstance(item, dict):
                pairs.append(
                    {
                        "key": str(item.get("key", "")),
                        "value": str(item.get("value", "")),
                        "enabled": item.get("enabled", True) is not False,
                    }
                )
        return pairs
    return []


def normalize_form_items(value):
    if isinstance(value, dict):
        value = [{"key": key, "value": val} for key, val in value.items()]
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            file_size = max(0, int(item.get("file_size") or 0))
        except (TypeError, ValueError):
            file_size = 0
        items.append(
            {
                "key": str(item.get("key", "")),
                "value": str(item.get("value", "")),
                "enabled": item.get("enabled", True) is not False,
                "type": "file" if item.get("type") == "file" else "text",
                "file_name": str(item.get("file_name", "")),
                "file_type": str(item.get("file_type", "application/octet-stream")),
                "file_size": file_size,
                "file_base64": str(item.get("file_base64", "")),
            }
        )
    return items


def multipart_token(value: str) -> str:
    return str(value).replace("\r", "").replace("\n", "").replace('"', r'\"')


def decode_form_file(item) -> bytes:
    if not item["file_name"] or not item["file_base64"]:
        raise ValueError(f'No file is selected for form-data field "{item["key"]}".')
    try:
        content = base64.b64decode(item["file_base64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f'Invalid file data for form-data field "{item["key"]}".') from exc
    if len(content) > MAX_FORM_FILE_BYTES:
        raise ValueError(f'File in form-data field "{item["key"]}" is too large.')
    return content


def build_url(raw_url: str, params, variables: dict[str, str]) -> str:
    rendered_url = render_template(raw_url, variables)
    parsed = urlsplit(rendered_url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for pair in normalize_pairs(render_template(params, variables)):
        if pair["enabled"] and pair["key"]:
            query_pairs.append((pair["key"], pair["value"]))
    query = urlencode(query_pairs)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def response_header(headers: dict[str, str | list[str]], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value[0] if isinstance(value, list) else str(value)
    return ""


def response_content_type(headers: dict[str, str | list[str]]) -> str:
    return response_header(headers, "Content-Type").split(";", 1)[0].strip().lower()


def looks_like_file_response(headers: dict[str, str | list[str]]) -> bool:
    disposition = response_header(headers, "Content-Disposition").lower()
    if "attachment" in disposition or "filename=" in disposition:
        return True
    content_type = response_content_type(headers)
    return (
        content_type in FILE_RESPONSE_TYPES
        or content_type.startswith("image/")
        or content_type.startswith("audio/")
        or content_type.startswith("video/")
    )


def build_auth_header(payload, variables, bearer_token: str | None = None) -> str | None:
    auth_type = str(payload.get("auth_type") or "bearer").strip().lower()
    if auth_type == "basic":
        username = str(render_template(payload.get("basic_auth_username") or "", variables))
        password = str(render_template(payload.get("basic_auth_password") or "", variables))
        if not username and not password:
            return None
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    token = str(render_template(bearer_token or payload.get("auth_token") or "", variables)).strip()
    if token:
        return token if token.lower().startswith("bearer ") else f"Bearer {token}"
    return None


def build_headers(payload, variables, bearer_token: str | None):
    result: dict[str, str] = {}
    for pair in normalize_pairs(render_template(payload.get("headers"), variables)):
        key = pair["key"].strip()
        if not pair["enabled"] or not key or is_blocked_header(key):
            continue
        if key.lower() == "authorization":
            continue
        result[key] = pair["value"]

    cookie_parts = []
    for pair in normalize_pairs(render_template(payload.get("cookies"), variables)):
        if pair["enabled"] and pair["key"]:
            cookie_parts.append(f"{pair['key']}={pair['value']}")
    if cookie_parts:
        existing = result.get("Cookie")
        result["Cookie"] = "; ".join([part for part in [existing, "; ".join(cookie_parts)] if part])

    auth_header = build_auth_header(payload, variables, bearer_token)
    if auth_header:
        result["Authorization"] = auth_header
    return result


def build_body(payload, headers, variables):
    body_type = payload.get("body_type") or "none"
    body_text = render_template(payload.get("body_text") or "", variables)
    legacy_form = payload.get("form")
    form_data = payload.get("form_data", legacy_form if body_type == "form-data" else [])
    urlencoded = payload.get("urlencoded", legacy_form if body_type == "form" else [])

    if body_type == "none":
        return None
    if body_type == "json":
        headers.setdefault("Content-Type", "application/json")
        return body_text.encode("utf-8")
    if body_type == "raw":
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        return body_text.encode("utf-8")
    if body_type == "form":
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        fields = [
            (item["key"], item["value"])
            for item in normalize_form_items(render_template(urlencoded, variables))
            if item["enabled"] and item["key"]
        ]
        return urlencode(fields).encode("utf-8")
    if body_type == "form-data":
        boundary = f"----reqapi-{uuid.uuid4().hex}"
        headers.setdefault("Content-Type", f"multipart/form-data; boundary={boundary}")
        chunks: list[bytes] = []
        for item in normalize_form_items(render_template(form_data, variables)):
            if not item["enabled"] or not item["key"]:
                continue
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            field_name = multipart_token(item["key"])
            if item["type"] == "file":
                file_name = multipart_token(item["file_name"])
                content_type = multipart_token(item["file_type"] or "application/octet-stream")
                chunks.append(
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'.encode(
                        "utf-8"
                    )
                )
                chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
                chunks.append(decode_form_file(item))
            else:
                chunks.append(
                    f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode(
                        "utf-8"
                    )
                )
                chunks.append(str(item["value"]).encode("utf-8"))
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks)
    if body_type == "binary":
        binary = payload.get("binary") if isinstance(payload.get("binary"), dict) else {}
        content = decode_form_file(
            {
                "key": "binary",
                "file_name": binary.get("file_name", ""),
                "file_base64": binary.get("file_base64", ""),
            }
        )
        headers.setdefault("Content-Type", str(binary.get("file_type") or "application/octet-stream"))
        return content
    if body_type == "graphql":
        graphql = payload.get("graphql") if isinstance(payload.get("graphql"), dict) else {}
        query = str(render_template(graphql.get("query") or "", variables))
        raw_variables = str(render_template(graphql.get("variables") or "{}", variables)).strip() or "{}"
        try:
            graphql_variables = json.loads(raw_variables)
        except json.JSONDecodeError as exc:
            raise ValueError("GraphQL variables must be valid JSON.") from exc
        if not isinstance(graphql_variables, dict):
            raise ValueError("GraphQL variables must be a JSON object.")
        headers.setdefault("Content-Type", "application/json")
        return json.dumps({"query": query, "variables": graphql_variables}).encode("utf-8")
    raise ValueError(f"Unsupported body type: {body_type}")


def execute_request(
    payload,
    variables: dict[str, str],
    bearer_token: str | None = None,
    cancellation: RequestCancellation | None = None,
):
    method = (payload.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError("Unsupported HTTP method.")

    final_url = build_url(payload.get("url") or "", payload.get("params"), variables)
    target = validate_target_url(final_url)
    skip_tls_verification = bool(payload.get("skip_tls_verification"))
    if skip_tls_verification and not can_skip_tls_verification(target):
        raise SecurityError(
            "Skip TLS verification is allowed only for explicitly allowlisted HTTPS domains."
        )
    parsed = urlsplit(final_url)
    headers = build_headers(payload, variables, bearer_token)
    body = None if method in METHODS_WITHOUT_BODY else build_body(payload, headers, variables)
    if body and len(body) > MAX_REQUEST_BODY_BYTES:
        raise ValueError(
            f"Request body exceeds the {MAX_REQUEST_BODY_BYTES // (1024 * 1024)} MB limit."
        )
    headers["Host"] = target.host_header
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    start = time.perf_counter()
    conn = None
    try:
        if target.scheme == "https":
            context = ssl.create_default_context()
            if skip_tls_verification:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            conn = PinnedHTTPSConnection(
                target.connect_host,
                target.port,
                timeout=REQUEST_TIMEOUT_SECONDS,
                context=context,
                tls_server_hostname=target.tls_server_hostname or target.hostname,
            )
        else:
            conn = http.client.HTTPConnection(
                target.connect_host, target.port, timeout=REQUEST_TIMEOUT_SECONDS
            )

        if cancellation:
            cancellation.attach(conn)
        conn.request(method, path, body=body, headers=headers)
        if cancellation:
            cancellation.check()
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if cancellation:
            cancellation.check()
        truncated = len(raw) > MAX_RESPONSE_BYTES
        raw = raw[:MAX_RESPONSE_BYTES]
        duration_ms = int((time.perf_counter() - start) * 1000)
        response_header_items = response.getheaders()
        response_headers: dict[str, str | list[str]] = {}
        for key, value in response_header_items:
            existing = response_headers.get(key)
            if existing is None:
                response_headers[key] = value
            elif isinstance(existing, list):
                existing.append(value)
            else:
                response_headers[key] = [existing, value]
        display_raw = raw[:MAX_RESPONSE_DISPLAY_BYTES]
        charset = response_header(response_headers, "Content-Type")
        encoding = "utf-8"
        if "charset=" in charset:
            encoding = charset.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        text = display_raw.decode(encoding, errors="replace")
        response_body_base64 = ""
        if looks_like_file_response(response_headers) and not truncated:
            response_body_base64 = base64.b64encode(raw).decode("ascii")
        return {
            "ok": True,
            "status": response.status,
            "reason": response.reason,
            "duration_ms": duration_ms,
            "url": final_url,
            "request_headers": redact_headers(headers),
            "request_body": body.decode("utf-8", errors="replace") if body else None,
            "response_headers": response_headers,
            "response_header_items": [
                {"key": key, "value": value} for key, value in response_header_items
            ],
            "response_body": text,
            "response_body_base64": response_body_base64,
            "response_size": len(raw),
            "response_truncated": truncated,
            "tls_verification_skipped": skip_tls_verification,
        }
    except SecurityError:
        raise
    except RequestCancelled:
        raise
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        return {
            "ok": False,
            "status": None,
            "reason": "",
            "duration_ms": duration_ms,
            "url": final_url,
            "request_headers": redact_headers(headers),
            "request_body": body.decode("utf-8", errors="replace") if body else None,
            "response_headers": {},
            "response_header_items": [],
            "response_body": "",
            "response_body_base64": "",
            "response_size": 0,
            "response_truncated": False,
            "tls_verification_skipped": skip_tls_verification,
            "error": str(exc),
        }
    finally:
        if cancellation and conn:
            cancellation.detach(conn)
        if conn:
            conn.close()


def pretty_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except Exception:
        return text

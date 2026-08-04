from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PASSWORD_ITERATIONS = 600_000
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 256 * 1024 * 1024
ALLOWED_CLIENT_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
)
ALLOWED_TARGET_NETWORKS = ALLOWED_CLIENT_NETWORKS
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
BLOCKED_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "upgrade",
}
VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


class SecurityError(ValueError):
    pass


@dataclass(frozen=True)
class TargetURL:
    original_url: str
    scheme: str
    hostname: str
    connect_host: str
    port: int
    host_header: str
    tls_server_hostname: str | None = None
    resolved_ips: tuple[str, ...] = ()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password must not be empty")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        parts = encoded.split("$")
        algorithm = parts[0]
        if algorithm == "scrypt" and len(parts) == 6:
            _, n, r, p, salt_b64, digest_b64 = parts
            salt = _unb64(salt_b64)
            expected = _unb64(digest_b64)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
                maxmem=SCRYPT_MAXMEM,
            )
            return hmac.compare_digest(actual, expected)
        if algorithm != "pbkdf2_sha256" or len(parts) != 4:
            return False
        _, iterations, salt_b64, digest_b64 = parts
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_ip_allowed(ip_value: str, networks=ALLOWED_CLIENT_NETWORKS) -> bool:
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def host_header_for(parsed, hostname: str, port: int) -> str:
    default_port = 443 if parsed.scheme == "https" else 80
    return hostname if port == default_port else f"{hostname}:{port}"


def resolve_domain(hostname: str, port: int, resolver=socket.getaddrinfo) -> tuple[str, ...]:
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SecurityError(f"Could not resolve domain {hostname}.") from exc

    resolved: list[str] = []
    for record in records:
        sockaddr = record[4]
        ip_value = sockaddr[0]
        if ip_value not in resolved:
            resolved.append(ip_value)

    if not resolved:
        raise SecurityError(f"Could not resolve domain {hostname}.")

    return tuple(resolved)


def can_skip_tls_verification(target: TargetURL) -> bool:
    return target.scheme == "https" and not is_ip_allowed(target.hostname)


def validate_target_url(url: str, resolver=socket.getaddrinfo) -> TargetURL:
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise SecurityError("Only http and https URLs are allowed.")
    if not parsed.hostname:
        raise SecurityError("URL must include a hostname.")
    if parsed.username or parsed.password:
        raise SecurityError("Credentials in URLs are not allowed.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SecurityError("Invalid port in URL.") from exc
    if port < 1 or port > 65535:
        raise SecurityError("Invalid port in URL.")

    hostname = parsed.hostname.strip().lower()
    host_header = host_header_for(parsed, hostname, port)
    if hostname == "localhost":
        return TargetURL(
            url,
            parsed.scheme,
            hostname,
            "127.0.0.1",
            port,
            host_header,
            tls_server_hostname=hostname if parsed.scheme == "https" else None,
            resolved_ips=("127.0.0.1",),
        )

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        resolved_ips = resolve_domain(hostname, port, resolver=resolver)
        return TargetURL(
            url,
            parsed.scheme,
            hostname,
            resolved_ips[0],
            port,
            host_header,
            tls_server_hostname=hostname if parsed.scheme == "https" else None,
            resolved_ips=resolved_ips,
        )

    if not any(ip in network for network in ALLOWED_TARGET_NETWORKS):
        raise SecurityError("Target host is outside the allowed local network.")
    return TargetURL(
        url,
        parsed.scheme,
        hostname,
        str(ip),
        port,
        host_header,
        tls_server_hostname=hostname if parsed.scheme == "https" else None,
        resolved_ips=(str(ip),),
    )


def crypto_available() -> bool:
    try:
        from cryptography.fernet import Fernet  # noqa: F401

        return True
    except Exception:
        return False


def load_fernet(secret_key_path: Path):
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:
        raise RuntimeError(
            "cryptography is required for Bearer token encryption. "
            "Install dependencies with: python3 -m pip install -r requirements.txt"
        ) from exc

    env_key = os.environ.get("REQAPI_SECRET_KEY")
    if env_key:
        return Fernet(env_key.encode("utf-8"))

    secret_key_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_key_path.exists():
        key = secret_key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        secret_key_path.write_bytes(key)
        try:
            secret_key_path.chmod(0o600)
        except OSError:
            pass
    return Fernet(key)


def encrypt_secret(plaintext: str, secret_key_path: Path) -> str:
    fernet = load_fernet(secret_key_path)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, secret_key_path: Path) -> str:
    fernet = load_fernet(secret_key_path)
    return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def random_token() -> str:
    return secrets.token_urlsafe(48)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADER_NAMES:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def is_blocked_header(name: str) -> bool:
    return name.strip().lower() in BLOCKED_REQUEST_HEADERS


def render_template(value, variables: dict[str, str]):
    if isinstance(value, str):
        return VAR_PATTERN.sub(lambda match: str(variables.get(match.group(1), "")), value)
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render_template(val, variables) for key, val in value.items()}
    return value

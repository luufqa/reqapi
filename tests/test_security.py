import unittest

from reqapi.security import (
    SecurityError,
    can_skip_tls_verification,
    hash_password,
    is_ip_allowed,
    render_template,
    validate_target_url,
    verify_password,
)


class SecurityTests(unittest.TestCase):
    def test_password_hash_verifies(self):
        encoded = hash_password("very-strong-password")
        self.assertTrue(verify_password("very-strong-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))

    def test_client_ip_allowlist(self):
        self.assertTrue(is_ip_allowed("127.0.0.1"))
        self.assertTrue(is_ip_allowed("192.168.10.20"))
        self.assertFalse(is_ip_allowed("10.0.0.1"))
        self.assertFalse(is_ip_allowed("8.8.8.8"))

    def test_target_allowlist_accepts_localhost_and_192_168(self):
        self.assertEqual(validate_target_url("http://localhost:8000").connect_host, "127.0.0.1")
        self.assertEqual(
            validate_target_url("http://192.168.1.10/api").connect_host,
            "192.168.1.10",
        )

    def test_target_rules_accept_dns_names_and_reject_external_ip_literals(self):
        def external_resolver(host, port, type=None):
            return [(None, None, None, "", ("93.184.216.34", port))]

        target = validate_target_url("https://example.com", resolver=external_resolver)
        self.assertEqual(target.hostname, "example.com")
        self.assertEqual(target.connect_host, "93.184.216.34")
        with self.assertRaises(SecurityError):
            validate_target_url("http://10.0.0.5")
        with self.assertRaises(SecurityError):
            validate_target_url("http://8.8.8.8")

    def test_allowlisted_domain_accepts_local_dns_result(self):
        def local_resolver(host, port, type=None):
            return [(None, None, None, "", ("192.168.12.34", port))]

        target = validate_target_url(
            "https://api.example.test/api/employees",
            resolver=local_resolver,
        )
        self.assertEqual(target.connect_host, "192.168.12.34")
        self.assertEqual(target.host_header, "api.example.test")
        self.assertEqual(target.tls_server_hostname, "api.example.test")

    def test_allowlisted_domain_accepts_external_dns_result(self):
        def external_resolver(host, port, type=None):
            return [(None, None, None, "", ("203.0.113.24", port))]

        target = validate_target_url(
            "https://api.example.test/api/employees",
            resolver=external_resolver,
        )
        self.assertEqual(target.connect_host, "203.0.113.24")
        self.assertEqual(target.host_header, "api.example.test")

    def test_example_subdomain_is_allowed(self):
        def external_resolver(host, port, type=None):
            return [(None, None, None, "", ("203.0.113.24", port))]

        target = validate_target_url(
            "https://regions.example.test/api",
            resolver=external_resolver,
        )
        self.assertEqual(target.connect_host, "203.0.113.24")
        self.assertEqual(target.host_header, "regions.example.test")
        self.assertTrue(can_skip_tls_verification(target))

    def test_public_demo_domain_is_allowed(self):
        def external_resolver(host, port, type=None):
            return [(None, None, None, "", ("172.64.155.209", port))]

        target = validate_target_url(
            "https://jsonplaceholder.typicode.com/users",
            resolver=external_resolver,
        )
        self.assertEqual(target.hostname, "jsonplaceholder.typicode.com")
        self.assertEqual(target.connect_host, "172.64.155.209")
        self.assertTrue(can_skip_tls_verification(target))

    def test_skip_tls_verification_only_for_allowlisted_https_domains(self):
        def external_resolver(host, port, type=None):
            return [(None, None, None, "", ("203.0.113.24", port))]

        allowed = validate_target_url(
            "https://api.example.test/api/employees",
            resolver=external_resolver,
        )
        local = validate_target_url("https://192.168.1.10/api")
        plain_http = validate_target_url(
            "http://api.example.test/api/employees",
            resolver=external_resolver,
        )
        self.assertTrue(can_skip_tls_verification(allowed))
        self.assertFalse(can_skip_tls_verification(local))
        self.assertFalse(can_skip_tls_verification(plain_http))

    def test_template_rendering(self):
        rendered = render_template(
            {"url": "{{baseUrl}}/users/{{ id }}", "items": ["{{baseUrl}}"]},
            {"baseUrl": "http://localhost:8000", "id": "42"},
        )
        self.assertEqual(rendered["url"], "http://localhost:8000/users/42")
        self.assertEqual(rendered["items"], ["http://localhost:8000"])


if __name__ == "__main__":
    unittest.main()

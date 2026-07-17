"""Offline unit tests for security boundaries (no network access required)."""

from __future__ import annotations

import ipaddress
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SECURITY_PATH = Path(__file__).resolve().parents[1] / "src" / "security.py"
SECURITY_SPEC = importlib.util.spec_from_file_location("agent_builder_security", SECURITY_PATH)
assert SECURITY_SPEC and SECURITY_SPEC.loader
security = importlib.util.module_from_spec(SECURITY_SPEC)
sys.modules[SECURITY_SPEC.name] = security
SECURITY_SPEC.loader.exec_module(security)

from agent_builder_security import (  # noqa: E402
    APIAuthenticationError,
    RequestBodyLimitMiddleware,
    SecurityValidationError,
    authenticate_api_headers,
    parse_cors_origins,
    resolve_contained_path,
    redact_arguments,
    redact_mapping,
    sanitise_filename,
    validate_archive_member_name,
    validate_outbound_url,
    validate_package_specs,
)


TOKEN = "t" * 48


class AuthenticationTests(unittest.TestCase):
    def test_unconfigured_api_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(APIAuthenticationError) as context:
                authenticate_api_headers({})
        self.assertEqual(context.exception.status_code, 503)

    def test_bearer_and_api_key_are_supported(self):
        authenticate_api_headers({"authorization": f"Bearer {TOKEN}"}, TOKEN)
        authenticate_api_headers({"x-api-key": TOKEN}, TOKEN)

    def test_wrong_or_conflicting_credentials_fail(self):
        with self.assertRaises(APIAuthenticationError) as context:
            authenticate_api_headers({"authorization": f"Bearer {'x' * 48}"}, TOKEN)
        self.assertEqual(context.exception.status_code, 401)

        with self.assertRaises(APIAuthenticationError):
            authenticate_api_headers(
                {"authorization": f"Bearer {TOKEN}", "x-api-key": "x" * 48},
                TOKEN,
            )


class CORSTests(unittest.TestCase):
    def test_defaults_are_local_and_explicit(self):
        origins = parse_cors_origins("")
        self.assertIn("http://127.0.0.1:20815", origins)
        self.assertNotIn("*", origins)

    def test_wildcard_is_rejected(self):
        with self.assertRaises(SecurityValidationError):
            parse_cors_origins("*")


class SSRFTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_and_private_literals_are_rejected(self):
        for url in ("http://127.0.0.1:11434", "http://10.0.0.8", "http://[::1]"):
            with self.subTest(url=url), self.assertRaises(SecurityValidationError):
                await validate_outbound_url(url, allowlist="")

    async def test_private_host_requires_explicit_allowlist(self):
        accepted = await validate_outbound_url(
            "http://127.0.0.1:11434/v1",
            allowlist="127.0.0.1:11434",
        )
        self.assertEqual(accepted, "http://127.0.0.1:11434/v1")

    async def test_metadata_address_cannot_be_allowlisted(self):
        with self.assertRaises(SecurityValidationError):
            await validate_outbound_url(
                "http://169.254.169.254/latest/meta-data",
                allowlist="169.254.169.254/32",
            )

    async def test_dns_result_must_be_global(self):
        with patch.object(
            security, "_resolve_host_sync", return_value={ipaddress.ip_address("10.1.2.3")}
        ):
            with self.assertRaises(SecurityValidationError):
                await validate_outbound_url(
                    "https://example.test", allowlist="example.test:443"
                )

        with patch.object(
            security, "_resolve_host_sync", return_value={ipaddress.ip_address("93.184.216.34")}
        ):
            self.assertEqual(
                await validate_outbound_url(
                    "https://example.test", allowlist="example.test:443"
                ),
                "https://example.test",
            )

    async def test_untrusted_public_dns_name_is_rejected_before_resolution(self):
        with patch.object(security, "_resolve_host_sync") as resolver:
            with self.assertRaises(SecurityValidationError):
                await validate_outbound_url("https://attacker.example", allowlist="")
        resolver.assert_not_called()

    async def test_wildcard_dns_allowlist_is_rejected(self):
        with patch.object(security, "_resolve_host_sync") as resolver:
            with self.assertRaises(SecurityValidationError):
                await validate_outbound_url(
                    "https://api.example.test",
                    allowlist="*.example.test",
                )
        resolver.assert_not_called()

    async def test_non_http_and_url_credentials_are_rejected(self):
        credential_url = "http://" + "user:pass" + "@example.com"
        for url in ("file:///etc/passwd", credential_url):
            with self.subTest(url=url), self.assertRaises(SecurityValidationError):
                await validate_outbound_url(url, allowlist="")


class PathTests(unittest.TestCase):
    def test_traversal_absolute_paths_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (root / "escape.txt").symlink_to(outside)

            self.assertEqual(resolve_contained_path(root, "safe.txt"), root / "safe.txt")
            for candidate in ("../outside.txt", str(outside), "escape.txt"):
                with self.subTest(candidate=candidate), self.assertRaises(SecurityValidationError):
                    resolve_contained_path(root, candidate)

    def test_filename_is_reduced_to_safe_basename(self):
        self.assertEqual(sanitise_filename("../../report.pdf"), "report.pdf")
        self.assertEqual(sanitise_filename(r"..\report.pdf"), "report.pdf")
        with self.assertRaises(SecurityValidationError):
            sanitise_filename("..")

    def test_zip_member_traversal_variants_are_rejected(self):
        self.assertEqual(validate_archive_member_name("skill/SKILL.md").as_posix(), "skill/SKILL.md")
        for candidate in ("../SKILL.md", "/etc/passwd", r"..\evil.py", "C:/evil.py"):
            with self.subTest(candidate=candidate), self.assertRaises(SecurityValidationError):
                validate_archive_member_name(candidate)


class PackageValidationTests(unittest.TestCase):
    def test_versioned_package_specs_are_allowed(self):
        self.assertEqual(
            validate_package_specs(["requests==2.32.0", "httpx[http2]>=0.27"]),
            ["requests==2.32.0", "httpx[http2]>=0.27"],
        )

    def test_pip_options_urls_and_paths_are_rejected(self):
        for package in ("--target=/tmp/x", "https://example.test/pkg.whl", "../pkg", "name @ https://x"):
            with self.subTest(package=package), self.assertRaises(SecurityValidationError):
                validate_package_specs([package])


class RedactionTests(unittest.TestCase):
    def test_secret_mappings_and_arguments_are_masked(self):
        self.assertEqual(
            redact_mapping({"API_KEY": "secret", "PATH": "/bin"}),
            {"API_KEY": "***", "PATH": "/bin"},
        )
        self.assertEqual(
            redact_arguments(["--token", "secret", "--api-key=value", "safe"]),
            ["--token", "***", "--api-key=***", "safe"],
        )


class RequestLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_is_limited_before_endpoint(self):
        endpoint_called = False
        messages = [
            {"type": "http.request", "body": b"x" * (40 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x" * (30 * 1024), "more_body": False},
        ]
        sent = []

        async def endpoint(scope, receive, send):
            nonlocal endpoint_called
            endpoint_called = True
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(endpoint)
        await middleware(
            {"type": "http", "path": "/api/log", "headers": []},
            receive,
            send,
        )

        self.assertTrue(endpoint_called)
        self.assertEqual(sent[0]["status"], 413)


if __name__ == "__main__":
    unittest.main()

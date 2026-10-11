"""Empirical stress tests for OpenTheRank HTTP client, fast-fail latency, redirects, and socket aborts."""
from __future__ import annotations

import http.client
import http.server
import socket
import threading
import time
import unittest
import urllib.error
from typing import Optional

from openrank.client import (
    HttpClient,
    HttpError,
    HttpFastFailError,
    NetworkError,
    NoRedirectHandler,
)


class EphemeralHttpServer:
    """Helper ephemeral HTTP server for live deterministic socket tests."""

    def __init__(self, handler_cls) -> None:
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get_url(self, path: str = "") -> str:
        if path and not path.startswith("/"):
            path = "/" + path
        return f"http://127.0.0.1:{self.port}{path}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestHttpFastFailBenchmark(unittest.TestCase):
    """Stress tests for deterministic 4xx fast-fail latency (<0.5s) and typing."""

    @classmethod
    def setUpClass(cls) -> None:
        class FastFailHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/status/"):
                    code = int(self.path.split("/status/")[1])
                    self.send_response(code)
                    if code == 429:
                        self.send_header("Retry-After", "120")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(f'{{"code": {code}}}'.encode("utf-8"))
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def log_message(self, *args):
                pass

        cls.server = EphemeralHttpServer(FastFailHandler)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_4xx_fast_fail_latency_and_typing(self) -> None:
        """Assert all 4xx status codes fast-fail in <0.5s and raise HttpFastFailError."""
        client = HttpClient(timeout=5.0)
        status_codes = [400, 401, 403, 404, 405, 409, 410, 422, 429]

        for code in status_codes:
            url = self.server.get_url(f"/status/{code}")
            start_time = time.perf_counter()
            with self.assertRaises(HttpFastFailError) as ctx:
                client.get(url)
            elapsed = time.perf_counter() - start_time

            # Benchmark requirement: deterministic fast-fail < 0.5s
            self.assertLess(
                elapsed,
                0.5,
                f"Status {code} took {elapsed:.4f}s, exceeding 0.5s threshold",
            )
            self.assertEqual(ctx.exception.status_code, code)
            self.assertEqual(ctx.exception.url, url)
            self.assertIsInstance(ctx.exception, HttpError)

    def test_429_retry_after_no_sleep(self) -> None:
        """Assert 429 Too Many Requests fails immediately without sleeping on Retry-After."""
        client = HttpClient(timeout=5.0)
        url = self.server.get_url("/status/429")

        start = time.perf_counter()
        with self.assertRaises(HttpFastFailError) as ctx:
            client.get(url)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers.get("Retry-After"), "120")

    def test_5xx_server_errors_raise_httperror_not_fastfail(self) -> None:
        """Assert 5xx status codes raise HttpError, but NOT HttpFastFailError."""
        client = HttpClient(timeout=5.0)
        for code in [500, 502, 503, 504]:
            url = self.server.get_url(f"/status/{code}")
            with self.assertRaises(HttpError) as ctx:
                client.get(url)
            self.assertNotIsInstance(ctx.exception, HttpFastFailError)
            self.assertEqual(ctx.exception.status_code, code)

    def test_fast_fail_in_fetch_with_redirect_check(self) -> None:
        """Assert fetch_with_redirect_check fast-fails on 4xx codes."""
        client = HttpClient(timeout=5.0)
        url = self.server.get_url("/status/404")
        start = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            client.fetch_with_redirect_check(url, expected_country="us")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_fast_fail_in_check_redirect(self) -> None:
        """Assert check_redirect fast-fails on 4xx codes."""
        client = HttpClient(timeout=5.0)
        url = self.server.get_url("/status/403")
        start = time.perf_counter()
        with self.assertRaises(HttpFastFailError):
            client.check_redirect(url)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


class TestRedirectTrackingAndSpoofing(unittest.TestCase):
    """Stress tests for redirect handling, chained redirects, and anti-spoofing."""

    @classmethod
    def setUpClass(cls) -> None:
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                p = self.path
                if p == "/spoof-us":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/spoof-relative-us":
                    self.send_response(302)
                    self.send_header("Location", "/us/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/spoof-other-country":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/gb/app/chatgpt/id6448311069")
                    self.end_headers()
                elif p == "/valid-ph-absolute":
                    self.send_response(301)
                    self.send_header("Location", "https://apps.apple.com/ph/app/chatgpt-official/id6448311069")
                    self.end_headers()
                elif p == "/valid-ph-relative":
                    self.send_response(302)
                    self.send_header("Location", "/ph/app/chatgpt-official/id6448311069")
                    self.end_headers()
                elif p == "/chain-1":
                    self.send_response(301)
                    self.send_header("Location", "/chain-2")
                    self.end_headers()
                elif p == "/chain-2":
                    self.send_response(302)
                    self.send_header("Location", "/chain-3")
                    self.end_headers()
                elif p == "/chain-3":
                    self.send_response(307)
                    self.send_header("Location", "/chain-final")
                    self.end_headers()
                elif p == "/chain-final":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Final Chain Reached")
                elif p == "/chain-spoof-1":
                    # Intermediate normalization within /xy/
                    self.send_response(301)
                    self.send_header("Location", "/chain-spoof-2/xy/normalized")
                    self.end_headers()
                elif p == "/chain-spoof-2/xy/normalized":
                    # Terminating redirect to /us/
                    self.send_response(302)
                    self.send_header("Location", "https://apps.apple.com/us/app/target")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Direct 200 OK")

            def log_message(self, *args):
                pass

        cls.server = EphemeralHttpServer(RedirectHandler)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_cross_country_spoof_detected(self) -> None:
        """Assert redirect from /xy/ to /us/ is detected as is_spoofed=True."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/spoof-us"),
            expected_country="xy",
        )
        self.assertEqual(code, 301)
        self.assertEqual(final_url, "https://apps.apple.com/us/app/chatgpt/id6448311069")
        self.assertTrue(is_spoofed)

    def test_cross_country_spoof_to_other_country_detected(self) -> None:
        """Assert redirect from /xy/ to /gb/ is detected as is_spoofed=True (country lost)."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/spoof-other-country"),
            expected_country="xy",
        )
        self.assertEqual(code, 301)
        self.assertTrue(is_spoofed)

    def test_relative_spoof_location_detected(self) -> None:
        """Assert relative Location /us/app/... is detected as is_spoofed=True."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/spoof-relative-us"),
            expected_country="xy",
        )
        self.assertEqual(code, 302)
        self.assertTrue(is_spoofed)

    def test_valid_in_country_redirect_not_spoofed(self) -> None:
        """Assert in-country redirect within /ph/ is is_spoofed=False."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/valid-ph-absolute"),
            expected_country="ph",
        )
        self.assertEqual(code, 301)
        self.assertFalse(is_spoofed)

    def test_valid_relative_in_country_redirect_not_spoofed(self) -> None:
        """Assert in-country relative redirect /ph/app/... is is_spoofed=False."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/valid-ph-relative"),
            expected_country="ph",
        )
        self.assertEqual(code, 302)
        self.assertFalse(is_spoofed)

    def test_us_expected_country_not_spoofed_when_redirected_to_us(self) -> None:
        """Assert when expected_country='us', redirecting to /us/ is NOT spoofed."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/spoof-us"),
            expected_country="us",
        )
        self.assertFalse(is_spoofed)

    def test_case_insensitive_and_whitespace_expected_country(self) -> None:
        """Assert expected_country='  PH  ' handles normalization cleanly."""
        client = HttpClient()
        code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
            self.server.get_url("/valid-ph-absolute"),
            expected_country="  PH  ",
        )
        self.assertFalse(is_spoofed)

    def test_chained_redirects_full_traversal(self) -> None:
        """Assert client.get(follow_redirects=True) traverses 301 -> 302 -> 307 -> 200."""
        client = HttpClient()
        status, headers, body = client.get(
            self.server.get_url("/chain-1"),
            follow_redirects=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"Final Chain Reached")

    def test_no_redirect_mode_stops_at_first_hop(self) -> None:
        """Assert client.get(follow_redirects=False) halts at initial 301."""
        client = HttpClient()
        status, headers, body = client.get(
            self.server.get_url("/chain-1"),
            follow_redirects=False,
        )
        self.assertEqual(status, 301)
        self.assertEqual(headers.get("Location"), "/chain-2")


class TestSocketAbortAndNetworkErrors(unittest.TestCase):
    """Stress tests for network failures, socket timeouts, and socket disconnect aborts."""

    def test_connection_refused_raises_network_error(self) -> None:
        """Assert connecting to an unopened port raises NetworkError."""
        # Find unused closed port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            unused_port = s.getsockname()[1]

        client = HttpClient(timeout=1.0)
        with self.assertRaises(NetworkError) as ctx:
            client.get(f"http://127.0.0.1:{unused_port}/closed")
        self.assertIn("Connection refused", str(ctx.exception))

    def test_socket_timeout_raises_network_error(self) -> None:
        """Assert server hanging longer than client timeout raises NetworkError."""
        class HangingHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(1.0)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        hang_server = EphemeralHttpServer(HangingHandler)
        try:
            client = HttpClient(timeout=0.2)
            with self.assertRaises(NetworkError) as ctx:
                client.get(hang_server.get_url("/hang"))
            self.assertIn("timed out", str(ctx.exception).lower())
        finally:
            hang_server.shutdown()

    def test_abrupt_socket_close_before_response_raises_network_error(self) -> None:
        """Assert server closing connection without sending HTTP response raises NetworkError."""
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.bind(("127.0.0.1", 0))
        port = raw_sock.getsockname()[1]
        raw_sock.listen(1)

        def close_immediately():
            conn, _ = raw_sock.accept()
            conn.recv(1024)
            conn.close()

        threading.Thread(target=close_immediately, daemon=True).start()

        client = HttpClient(timeout=1.0)
        try:
            with self.assertRaises(NetworkError) as ctx:
                client.get(f"http://127.0.0.1:{port}/abort")
            self.assertIn("connection", str(ctx.exception).lower())
        finally:
            raw_sock.close()

    def test_dns_resolution_failure_raises_network_error(self) -> None:
        """Assert invalid unresolvable hostname raises NetworkError."""
        client = HttpClient(timeout=1.0)
        with self.assertRaises(NetworkError):
            client.get("http://nonexistent-openrank-domain-12345.invalid/test")


class TestEmpiricalChallengerFindings(unittest.TestCase):
    """Specific challenge tests demonstrating edge case gaps found during adversarial analysis."""

    def test_gap_1_mid_stream_eof_incomplete_read_leakage(self) -> None:
        """CHALLENGE 1: Mid-stream socket abort leaks http.client.IncompleteRead instead of NetworkError.

        When a server sends 200 OK headers with Content-Length: 1000 but aborts after 10 bytes,
        resp.read() raises http.client.IncompleteRead, which does not inherit from OSError,
        leaking an unwrapped internal exception instead of NetworkError.
        """
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.bind(("127.0.0.1", 0))
        port = raw_sock.getsockname()[1]
        raw_sock.listen(1)

        def send_truncated():
            conn, _ = raw_sock.accept()
            conn.recv(1024)
            # Send 200 OK with Content-Length 1000, then send only 10 bytes and close
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n1234567890")
            conn.close()

        threading.Thread(target=send_truncated, daemon=True).start()

        client = HttpClient(timeout=1.0)
        try:
            # Under current implementation, IncompleteRead leaks uncaught as http.client.IncompleteRead
            with self.assertRaises(Exception) as ctx:
                client.get(f"http://127.0.0.1:{port}/truncated")
            # We verify the leakage empirically:
            is_wrapped = isinstance(ctx.exception, NetworkError)
            is_leaked = isinstance(ctx.exception, http.client.IncompleteRead)
            self.assertTrue(
                is_leaked or is_wrapped,
                f"Unexpected exception type: {type(ctx.exception)}",
            )
            # Document finding: currently is_leaked is True, is_wrapped is False
            print(f"\n[EMPIRICAL FINDING 1] IncompleteRead wrapped as NetworkError? {is_wrapped} (Leaked: {is_leaked})")
        finally:
            raw_sock.close()

    def test_gap_2_corrupt_bad_status_line_leakage(self) -> None:
        """CHALLENGE 2: Server sending non-HTTP garbage leaks http.client.BadStatusLine instead of NetworkError.

        http.client.BadStatusLine inherits from http.client.HTTPException, not OSError or URLError,
        causing an unwrapped low-level exception leak.
        """
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.bind(("127.0.0.1", 0))
        port = raw_sock.getsockname()[1]
        raw_sock.listen(1)

        def send_garbage():
            conn, _ = raw_sock.accept()
            conn.recv(1024)
            conn.sendall(b"NOT_HTTP_PROTOCOL_GARBAGE\r\n\r\n")
            conn.close()

        threading.Thread(target=send_garbage, daemon=True).start()

        client = HttpClient(timeout=1.0)
        try:
            with self.assertRaises(Exception) as ctx:
                client.get(f"http://127.0.0.1:{port}/garbage")
            is_wrapped = isinstance(ctx.exception, NetworkError)
            is_leaked = isinstance(ctx.exception, http.client.BadStatusLine)
            self.assertTrue(
                is_leaked or is_wrapped,
                f"Unexpected exception type: {type(ctx.exception)}",
            )
            print(f"\n[EMPIRICAL FINDING 2] BadStatusLine wrapped as NetworkError? {is_wrapped} (Leaked: {is_leaked})")
        finally:
            raw_sock.close()

    def test_gap_3_relative_location_header_not_resolved_to_absolute_url(self) -> None:
        """CHALLENGE 3: Relative Location header is not resolved via urljoin, producing unusable final_url.

        When a server returns Location: /us/app/chatgpt, fetch_with_redirect_check returns
        final_url = '/us/app/chatgpt' instead of resolving it against the base URL.
        """
        class RelativeRedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "/us/app/chatgpt")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = EphemeralHttpServer(RelativeRedirectHandler)
        try:
            client = HttpClient()
            target_url = server.get_url("/start")
            code, final_url, body, is_spoofed = client.fetch_with_redirect_check(
                target_url,
                expected_country="xy",
            )
            # Spoof detection succeeds
            self.assertTrue(is_spoofed)
            # Check whether final_url is absolute
            is_absolute = final_url.startswith("http://") or final_url.startswith("https://")
            print(f"\n[EMPIRICAL FINDING 3] Relative Location resolved to absolute URL? {is_absolute} (final_url='{final_url}')")
        finally:
            server.shutdown()

    def test_gap_4_module_level_convenience_functions_missing(self) -> None:
        """CHALLENGE 4: Handover claims fetch_with_redirect_check is exported at module level, but it is not.

        m1_worker_2/handoff.md lists fetch_with_redirect_check under openrank/client.py exports,
        and PROJECT.md interface contract lists fetch_bytes and fetch_with_redirect_check under openrank.client.
        """
        import openrank.client
        has_fetch_with_redirect_check = hasattr(openrank.client, "fetch_with_redirect_check")
        has_fetch_bytes = hasattr(openrank.client, "fetch_bytes")
        print(f"\n[EMPIRICAL FINDING 4] openrank.client has fetch_with_redirect_check? {has_fetch_with_redirect_check}")
        print(f"[EMPIRICAL FINDING 4] openrank.client has fetch_bytes? {has_fetch_bytes}")
        # Both currently False: only present as methods on HttpClient class


if __name__ == "__main__":
    unittest.main()

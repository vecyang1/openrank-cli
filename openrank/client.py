"""Hardened standard library HTTP client with redirect tracking and deterministic 4xx fast-fail."""
from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple


class HttpError(Exception):
    """Base exception for HTTP protocol errors."""

    def __init__(
        self,
        status_code: int,
        message: str,
        url: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.url = url
        self.headers = headers or {}


class HttpFastFailError(HttpError):
    """Deterministic fast-fail error raised immediately on 4xx status codes without retry."""
    pass


class NetworkError(Exception):
    """Raised on socket timeouts, DNS resolution failures, or connection errors."""
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler that intercepts HTTP redirects without following them."""

    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    def http_error_303(self, req, fp, code, msg, headers):
        return fp

    def http_error_307(self, req, fp, code, msg, headers):
        return fp

    def http_error_308(self, req, fp, code, msg, headers):
        return fp


def _safe_close(err: Any) -> None:
    """Safely close response or error without raising on mocked or uninitialized fp (Python 3.9 compat)."""
    try:
        if hasattr(err, "close"):
            err.close()
    except Exception:
        pass


class HttpClient:
    """Production-grade stdlib HTTP client with deterministic fast-fail and redirect protection."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36 (openrank-cli/0.1.0)"
    )

    DEFAULT_HEADERS = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(
        self,
        timeout: float = 10.0,
        user_agent: Optional[str] = None,
    ) -> None:
        self.timeout = float(timeout)
        self.headers = dict(self.DEFAULT_HEADERS)
        if user_agent:
            self.headers["User-Agent"] = user_agent

        self._default_opener = urllib.request.build_opener()
        self._no_redirect_opener = urllib.request.build_opener(NoRedirectHandler)

    def _prepare_request(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> urllib.request.Request:
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        return urllib.request.Request(url, headers=merged_headers, method="GET")

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        follow_redirects: bool = True,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Fetch URL and return (status_code, headers_dict, body_bytes).

        Raises HttpFastFailError on 4xx immediately.
        Raises HttpError on 5xx.
        Raises NetworkError on connection/timeout issues.
        """
        req = self._prepare_request(url, headers)
        opener = self._default_opener if follow_redirects else self._no_redirect_opener

        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status_code = getattr(resp, "status", getattr(resp, "code", 200))
                resp_headers = {k: v for k, v in resp.headers.items()}
                body = resp.read()
                return status_code, resp_headers, body
        except urllib.error.HTTPError as e:
            headers_dict = {k: v for k, v in e.headers.items()} if e.headers else {}
            code = e.code
            _safe_close(e)
            if 400 <= code < 500:
                raise HttpFastFailError(
                    status_code=code,
                    message=f"HTTP client error {code} for URL: {url}",
                    url=url,
                    headers=headers_dict,
                ) from e
            raise HttpError(
                status_code=code,
                message=f"HTTP server error {code} for URL: {url}",
                url=url,
                headers=headers_dict,
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise NetworkError(f"Connection failure accessing {url}: {e}") from e

    def fetch_bytes(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> bytes:
        """Convenience method returning raw bytes from URL."""
        _, _, body = self.get(url, headers=headers, follow_redirects=True)
        return body

    def check_redirect(self, url: str) -> Tuple[int, Optional[str]]:
        """Probe URL redirects without following to detect destination changes."""
        req = self._prepare_request(url)
        try:
            with self._no_redirect_opener.open(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", getattr(resp, "code", 200))
                location = resp.headers.get("Location")
                return code, location
        except urllib.error.HTTPError as e:
            code = e.code
            _safe_close(e)
            if 400 <= code < 500:
                raise HttpFastFailError(
                    status_code=code,
                    message=f"HTTP client error {code} for URL: {url}",
                    url=url,
                ) from e
            raise HttpError(
                status_code=code,
                message=f"HTTP server error {code} for URL: {url}",
                url=url,
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise NetworkError(f"Network probe failed for {url}: {e}") from e

    def fetch_with_redirect_check(
        self,
        url: str,
        expected_country: Optional[str] = None,
    ) -> Tuple[int, str, bytes, bool]:
        """Fetch URL while intercepting redirects to detect CDN fallback spoofing.

        Returns (status_code, final_url, body_bytes, is_spoofed).
        """
        req = self._prepare_request(url)
        try:
            with self._no_redirect_opener.open(req, timeout=self.timeout) as resp:
                status_code = getattr(resp, "status", getattr(resp, "code", 200))
                location = resp.headers.get("Location")
                body = resp.read()

                final_url = urllib.parse.urljoin(url, location) if location else url
                is_spoofed = False

                if location and expected_country:
                    exp = expected_country.strip().lower()
                    parsed = urllib.parse.urlsplit(final_url)
                    path_segments = [p for p in parsed.path.split("/") if p]
                    storefront_cc = path_segments[0].lower() if path_segments else ""
                    if storefront_cc != exp:
                        is_spoofed = True

                return status_code, final_url, body, is_spoofed
        except urllib.error.HTTPError as e:
            headers_dict = {k: v for k, v in e.headers.items()} if e.headers else {}
            code = e.code
            _safe_close(e)
            if 400 <= code < 500:
                raise HttpFastFailError(
                    status_code=code,
                    message=f"HTTP client error {code} for URL: {url}",
                    url=url,
                    headers=headers_dict,
                ) from e
            raise HttpError(
                status_code=code,
                message=f"HTTP server error {code} for URL: {url}",
                url=url,
                headers=headers_dict,
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise NetworkError(f"Network error accessing {url}: {e}") from e


_default_client = HttpClient()


def fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    """Module-level convenience function returning raw bytes from URL."""
    return _default_client.fetch_bytes(url, headers=headers)


def fetch_with_redirect_check(
    url: str,
    expected_country: Optional[str] = None,
) -> Tuple[int, str, bytes, bool]:
    """Module-level convenience function probing URL for redirect and CDN spoofing."""
    return _default_client.fetch_with_redirect_check(url, expected_country=expected_country)


__all__ = [
    "HttpClient",
    "HttpError",
    "HttpFastFailError",
    "NetworkError",
    "NoRedirectHandler",
    "fetch_bytes",
    "fetch_with_redirect_check",
]

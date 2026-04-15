"""Minimal Calibre Web upload client.

Implements just enough of the Calibre Web
(https://github.com/janeczku/calibre-web) HTTP surface to log in and
upload a book:

1. ``GET /login`` → scrape ``csrf_token`` hidden input, remember cookies
2. ``POST /login`` (urlencoded) with ``username``/``password``/``csrf_token``
3. ``GET /`` → scrape a session-bound ``csrf_token`` from the upload form
4. ``POST /upload`` (multipart) with ``btn-upload`` file field + ``csrf_token``

Built on the stdlib only (``urllib``, ``http.cookiejar``,
``html.parser``) to stay consistent with the rest of this project.
"""

from __future__ import annotations

import json
import mimetypes
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = "ade-dedrm"


class CalibreWebError(Exception):
    pass


class _CsrfFinder(HTMLParser):
    """Extract the first ``<input name="csrf_token" value="...">`` token."""

    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.token is not None or tag.lower() != "input":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if attr_map.get("name") == "csrf_token" and attr_map.get("value"):
            self.token = attr_map["value"]


def _extract_csrf(html: str) -> str:
    finder = _CsrfFinder()
    finder.feed(html)
    if not finder.token:
        raise CalibreWebError("could not find csrf_token in HTML response")
    return finder.token


def _build_ssl_ctx(verify_tls: bool) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _encode_multipart(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}".encode("ascii"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode("ascii")
        )
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(f"--{boundary}".encode("ascii"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"'
        ).encode("utf-8")
    )
    parts.append(f"Content-Type: {mime}".encode("ascii"))
    parts.append(b"")
    parts.append(file_path.read_bytes())
    parts.append(f"--{boundary}--".encode("ascii"))
    parts.append(b"")
    body = crlf.join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


class CalibreWebClient:
    """Session-aware Calibre Web client backed by ``urllib`` + ``CookieJar``."""

    def __init__(self, base_url: str, verify_tls: bool = True) -> None:
        if "://" not in base_url:
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_build_ssl_ctx(verify_tls)),
            urllib.request.HTTPCookieProcessor(self._jar),
            _NoRedirect(),
        )
        self._opener.addheaders = [("User-Agent", USER_AGENT)]
        self._logged_in = False

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(
            self._url(path), data=data, method=method, headers=headers or {}
        )
        try:
            resp = self._opener.open(req)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            return exc.code, dict(exc.headers or {}), body
        except urllib.error.URLError as exc:
            raise CalibreWebError(
                f"network error talking to {self.base_url}: {exc.reason}"
            ) from exc
        body = resp.read()
        return resp.getcode(), dict(resp.headers or {}), body

    def login(self, username: str, password: str) -> None:
        status, _headers, body = self._request("/login")
        if status != 200:
            raise CalibreWebError(
                f"GET /login returned HTTP {status} (is the URL correct?)"
            )
        csrf = _extract_csrf(body.decode("utf-8", "replace"))

        form = urllib.parse.urlencode(
            {
                "username": username,
                "password": password,
                "csrf_token": csrf,
                "next": "/",
            }
        ).encode("ascii")
        status, headers, _body = self._request(
            "/login",
            method="POST",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status == 429:
            raise CalibreWebError(
                "login rate-limited by Calibre Web (try again later)"
            )
        if status != 302:
            raise CalibreWebError(
                "login failed: check username/password "
                f"(Calibre Web returned HTTP {status})"
            )
        location = headers.get("Location", "")
        if "/login" in location:
            raise CalibreWebError("login failed: invalid credentials")
        self._logged_in = True

    def upload(self, path: Path) -> dict:
        if not self._logged_in:
            raise CalibreWebError("upload() called before login()")
        if not path.is_file():
            raise CalibreWebError(f"file not found: {path}")

        status, _headers, body = self._request("/")
        if status != 200:
            raise CalibreWebError(
                f"GET / returned HTTP {status} after login (session expired?)"
            )
        csrf = _extract_csrf(body.decode("utf-8", "replace"))

        body_bytes, content_type = _encode_multipart(
            fields={"csrf_token": csrf},
            file_field="btn-upload",
            file_path=path,
        )
        status, headers, resp_body = self._request(
            "/upload",
            method="POST",
            data=body_bytes,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body_bytes)),
                "X-CSRFToken": csrf,
            },
        )
        if status == 302 and "/login" in headers.get("Location", ""):
            raise CalibreWebError("upload failed: session expired, please retry")
        if status == 403:
            raise CalibreWebError(
                "upload failed: user lacks the 'upload' role in Calibre Web"
            )
        if status == 400:
            raise CalibreWebError(
                "upload failed: CSRF token rejected (HTTP 400)"
            )
        if status != 200:
            snippet = resp_body[:300].decode("utf-8", "replace")
            raise CalibreWebError(
                f"upload failed: HTTP {status}: {snippet}"
            )

        try:
            payload = json.loads(resp_body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise CalibreWebError(
                f"upload succeeded but response was not JSON: {exc}"
            ) from exc

        location = ""
        book_id: int | None = None
        if isinstance(payload, list) and payload:
            first = payload[0] if isinstance(payload[0], dict) else {}
            location = str(first.get("location") or "")
        elif isinstance(payload, dict):
            location = str(payload.get("location") or "")

        if location:
            tail = location.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                book_id = int(tail)
        return {"book_id": book_id, "location": location}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable auto-redirects so we can inspect 302 Location headers."""

    def http_error_302(self, req, fp, code, msg, headers):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302


__all__ = ["CalibreWebClient", "CalibreWebError"]

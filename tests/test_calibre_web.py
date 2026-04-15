"""Tests for CalibreWebClient against a local fake HTTP server."""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from ade_dedrm.calibre_web import CalibreWebClient, CalibreWebError


LOGIN_HTML = (
    '<html><body><form method="post" action="/login">'
    '<input type="hidden" name="csrf_token" value="LOGIN-TOKEN">'
    "</form></body></html>"
)

INDEX_HTML = (
    '<html><body><form method="post" action="/upload">'
    '<input type="hidden" name="csrf_token" value="UPLOAD-TOKEN">'
    '<input type="file" name="btn-upload">'
    "</form></body></html>"
)


class _FakeCalibreHandler(BaseHTTPRequestHandler):
    behavior: dict = {}  # type: ignore[assignment]

    def log_message(self, *args, **kwargs) -> None:  # silence test output
        pass

    # --- helpers ---
    def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _session_from_cookie(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        match = re.search(r"session=([^;]+)", raw)
        return match.group(1) if match else None

    # --- routes ---
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/login":
            self._send(
                200,
                LOGIN_HTML.encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            )
            return
        if self.path == "/":
            if not self._session_from_cookie():
                self._send(302, headers={"Location": "/login"})
                return
            self._send(
                200,
                INDEX_HTML.encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            )
            return
        self._send(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""

        if self.path == "/login":
            form = dict(
                kv.split("=", 1) for kv in body.decode("ascii").split("&") if "=" in kv
            )
            if form.get("csrf_token") != "LOGIN-TOKEN":
                self._send(400, b"bad csrf")
                return
            if self.behavior.get("bad_password"):
                self._send(
                    200,
                    LOGIN_HTML.encode("utf-8"),
                    {"Content-Type": "text/html; charset=utf-8"},
                )
                return
            if self.behavior.get("rate_limited"):
                self._send(429, b"slow down")
                return
            session_id = uuid.uuid4().hex
            self._send(
                302,
                headers={
                    "Location": "/",
                    "Set-Cookie": f"session={session_id}; Path=/; HttpOnly",
                },
            )
            return

        if self.path == "/upload":
            if not self._session_from_cookie():
                self._send(302, headers={"Location": "/login"})
                return
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._send(400, b"not multipart")
                return
            match = re.search(r"boundary=([^;]+)", ctype)
            assert match, "missing boundary"
            boundary = match.group(1).strip()
            text = body.decode("utf-8", "replace")

            if f'name="csrf_token"' not in text or "UPLOAD-TOKEN" not in text:
                self._send(400, b"missing csrf")
                return
            if 'name="btn-upload"' not in text:
                self._send(400, b"missing file field")
                return
            if self.behavior.get("forbidden"):
                self._send(403, b"forbidden")
                return

            # record for assertions
            self.behavior.setdefault("uploads", []).append(
                {"boundary": boundary, "len": len(body)}
            )
            payload = json.dumps({"location": "/admin/book/42"}).encode("utf-8")
            self._send(
                200, payload, {"Content-Type": "application/json"}
            )
            return

        self._send(404)


@pytest.fixture
def fake_server() -> Iterator[tuple[str, dict]]:
    _FakeCalibreHandler.behavior = {}
    server = HTTPServer(("127.0.0.1", 0), _FakeCalibreHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", _FakeCalibreHandler.behavior
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _sample_epub(tmp_path: Path) -> Path:
    path = tmp_path / "sample.epub"
    path.write_bytes(b"PK\x03\x04fake-epub-bytes")
    return path


def test_login_and_upload_success(fake_server, tmp_path: Path) -> None:
    url, behavior = fake_server
    client = CalibreWebClient(url)
    client.login("alice", "hunter2")
    result = client.upload(_sample_epub(tmp_path))
    assert result == {"book_id": 42, "location": "/admin/book/42"}
    assert len(behavior["uploads"]) == 1


def test_login_bad_password(fake_server, tmp_path: Path) -> None:
    url, behavior = fake_server
    behavior["bad_password"] = True
    client = CalibreWebClient(url)
    with pytest.raises(CalibreWebError, match="login failed"):
        client.login("alice", "wrong")


def test_login_rate_limited(fake_server) -> None:
    url, behavior = fake_server
    behavior["rate_limited"] = True
    client = CalibreWebClient(url)
    with pytest.raises(CalibreWebError, match="rate-limited"):
        client.login("alice", "hunter2")


def test_upload_without_login_raises(tmp_path: Path) -> None:
    client = CalibreWebClient("http://127.0.0.1:1")
    with pytest.raises(CalibreWebError, match="before login"):
        client.upload(_sample_epub(tmp_path))


def test_upload_forbidden_role(fake_server, tmp_path: Path) -> None:
    url, behavior = fake_server
    client = CalibreWebClient(url)
    client.login("alice", "hunter2")
    behavior["forbidden"] = True
    with pytest.raises(CalibreWebError, match="upload.*role"):
        client.upload(_sample_epub(tmp_path))


def test_network_error_wrapped() -> None:
    client = CalibreWebClient("http://127.0.0.1:1")
    with pytest.raises(CalibreWebError, match="network error"):
        client.login("alice", "hunter2")


def test_upload_cli_delete_after_upload(
    fake_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ade_dedrm import cli

    url, _behavior = fake_server
    target = _sample_epub(tmp_path)
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_URL", url)
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_USERNAME", "alice")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "hunter2")
    rc = cli.main(["upload", str(target), "--delete-after-upload"])
    assert rc == 0
    assert not target.exists()


def test_upload_cli_keeps_file_on_failure(
    fake_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ade_dedrm import cli

    url, behavior = fake_server
    behavior["forbidden"] = True
    target = _sample_epub(tmp_path)
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_URL", url)
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_USERNAME", "alice")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "hunter2")
    rc = cli.main(["upload", str(target), "--delete-after-upload"])
    assert rc != 0
    assert target.exists()

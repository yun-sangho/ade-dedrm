"""Tests for the MCP server wrappers.

These tests deliberately avoid importing the optional ``mcp`` package —
they exercise the underlying tool functions in
:mod:`ade_dedrm.mcpb.tools` and the sanitization helpers in
:mod:`ade_dedrm.mcpb.security` directly. That way the suite runs under
``uv run pytest`` without requiring the ``mcp`` extra to be installed.

The server module (``server.py``) is a thin FastMCP adapter around
``tools.tool_*``; the tool functions themselves are where the schema
and safety properties live, so that's what we test.

Property under test: **no tool function ever returns a string that
contains secret-like material** (Adobe RSA key bytes, Calibre passwords,
pkcs12 blobs, authorization headers, user:pass@host URLs).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ade_dedrm.adobe_state import DeviceState
from ade_dedrm.mcpb import security, tools


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _assert_no_secrets(blob: object, secrets: list[str]) -> None:
    """Fail if any ``secret`` substring appears anywhere in ``blob``.

    ``blob`` is JSON-serialized first so nested dicts/lists are covered.
    """
    serialized = json.dumps(blob, default=str, ensure_ascii=False)
    for s in secrets:
        assert s not in serialized, (
            f"secret leaked into tool output: {s!r} found in {serialized!r}"
        )


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect state_dir() to a throwaway directory for this test."""
    state_home = tmp_path / "ade-dedrm-state"
    state_home.mkdir()
    monkeypatch.setenv("ADE_DEDRM_HOME", str(state_home))
    # Strip any real Calibre env vars so tests are deterministic.
    for key in list(os.environ):
        if key.startswith("ADE_DEDRM_CALIBRE_"):
            monkeypatch.delenv(key, raising=False)
    return state_home


# --------------------------------------------------------------------------- #
# security.py unit tests
# --------------------------------------------------------------------------- #


def test_redact_strips_userinfo_in_url() -> None:
    raw = "failed to connect to https://admin:hunter2@calibre.example.com/login"
    redacted = security.redact(raw)
    assert "hunter2" not in redacted
    assert "admin" not in redacted
    assert "<redacted>" in redacted


def test_redact_strips_authorization_header() -> None:
    raw = "401 response: Authorization: Bearer sk-very-secret-token"
    redacted = security.redact(raw)
    assert "sk-very-secret-token" not in redacted
    assert "<redacted>" in redacted


def test_redact_strips_long_base64_blob() -> None:
    # 80-char base64-ish string (would match an RSA key or pkcs12 blob).
    blob = "A" * 80
    raw = f"key={blob}"
    redacted = security.redact(raw)
    assert blob not in redacted


def test_url_host_only_hides_credentials_and_path() -> None:
    assert security.url_host_only("https://u:p@calibre.example.com/page?q=1") == (
        "calibre.example.com"
    )
    assert security.url_host_only("") == ""
    assert security.url_host_only(None) == ""


def test_validate_input_path_rejects_unknown_extension(tmp_path: Path) -> None:
    evil = tmp_path / "malware.exe"
    evil.write_bytes(b"MZ")
    with pytest.raises(security.ToolInputError, match="unsupported file type"):
        security.validate_input_path(str(evil))


def test_validate_input_path_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(security.ToolInputError, match="does not exist"):
        security.validate_input_path(str(tmp_path / "nope.epub"))


def test_validate_input_path_rejects_paths_inside_state_dir(
    tmp_path: Path, isolated_state: Path
) -> None:
    # Put a decoy .epub inside the protected state dir.
    decoy = isolated_state / "adobekey.der.epub"
    decoy.write_bytes(b"PK\x03\x04")
    with pytest.raises(security.ToolInputError, match="state directory"):
        security.validate_input_path(str(decoy))


def test_validate_output_path_rejects_same_as_input(tmp_path: Path) -> None:
    src = tmp_path / "book.epub"
    src.write_bytes(b"PK\x03\x04")
    with pytest.raises(security.ToolInputError, match="must differ from input"):
        security.validate_output_path(
            str(src), default=src, input_path=src, force=True
        )


def test_validate_output_path_refuses_to_overwrite_without_force(
    tmp_path: Path,
) -> None:
    src = tmp_path / "book.epub"
    src.write_bytes(b"PK\x03\x04")
    existing = tmp_path / "out.epub"
    existing.write_bytes(b"old")
    with pytest.raises(security.ToolInputError, match="already exists"):
        security.validate_output_path(
            str(existing), default=existing, input_path=src, force=False
        )


# --------------------------------------------------------------------------- #
# tool_status
# --------------------------------------------------------------------------- #


def test_status_reports_not_ready_on_empty_state(isolated_state: Path) -> None:
    result = tools.tool_status()
    assert result["status"] == "ok"
    assert result["ade_state_ready"] is False
    assert result["adobe_key_ready"] is False
    assert result["ready_to_decrypt"] is False
    assert result["calibre_configured"] is False
    assert result["calibre_host"] == ""
    assert "bootstrap_ade" in result["hint"]


def test_status_never_leaks_calibre_credentials(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set credentials via env — mimics how MCPB passes user_config values.
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_URL", "https://calibre.example.com")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_USERNAME", "alice-the-reader")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "correct-horse-battery-staple")

    result = tools.tool_status()

    assert result["calibre_configured"] is True
    # Only the hostname should leak, not credentials or full URL.
    assert result["calibre_host"] == "calibre.example.com"
    _assert_no_secrets(
        result,
        [
            "correct-horse-battery-staple",
            "alice-the-reader",
            "https://calibre.example.com",  # full URL path
        ],
    )


# --------------------------------------------------------------------------- #
# tool_bootstrap_ade
# --------------------------------------------------------------------------- #


def test_bootstrap_ade_refuses_non_macos(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    result = tools.tool_bootstrap_ade()
    assert result["status"] == "bootstrap_failed"
    assert result["error_code"] == "unsupported_platform"
    assert "macOS" in result["error"]


def test_bootstrap_ade_is_idempotent_when_state_exists(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Populate a fake complete state.
    state = DeviceState(root=isolated_state)
    state.devicesalt.write_bytes(b"\x00" * 16)
    state.device_xml.write_text("<device/>", encoding="utf-8")
    state.activation_xml.write_text("<activation/>", encoding="utf-8")
    # Force the code path to believe we're on darwin so the platform
    # guard doesn't short-circuit first.
    monkeypatch.setattr("sys.platform", "darwin")

    result = tools.tool_bootstrap_ade(force=False)
    assert result["status"] == "ok"
    assert result["already_initialized"] is True
    assert result["state_dir"] == str(isolated_state)


# --------------------------------------------------------------------------- #
# tool_decrypt input validation
# --------------------------------------------------------------------------- #


def test_decrypt_rejects_missing_file(isolated_state: Path) -> None:
    result = tools.tool_decrypt("/nonexistent/path/to/book.epub")
    assert result["status"] == "invalid_input"
    assert result["error_code"] == "invalid_input"


def test_decrypt_rejects_unsupported_extension(
    tmp_path: Path, isolated_state: Path
) -> None:
    evil = tmp_path / "notes.txt"
    evil.write_text("hello", encoding="utf-8")
    result = tools.tool_decrypt(str(evil))
    assert result["status"] == "invalid_input"
    assert "unsupported file type" in result["error"]


def test_decrypt_rejects_files_in_state_dir(isolated_state: Path) -> None:
    decoy = isolated_state / "adobekey.der.epub"
    decoy.write_bytes(b"PK\x03\x04")
    result = tools.tool_decrypt(str(decoy))
    assert result["status"] == "invalid_input"
    assert "state directory" in result["error"]


def test_decrypt_reports_missing_user_key(
    tmp_path: Path, isolated_state: Path
) -> None:
    # Valid extension + magic bytes but no adobekey.der in state.
    book = tmp_path / "book.epub"
    book.write_bytes(b"PK\x03\x04not really an epub")
    result = tools.tool_decrypt(str(book))
    assert result["status"] == "invalid_input"
    assert result["error_code"] == "no_user_key"
    assert "bootstrap_ade" in result["error"]


def test_decrypt_acsm_requires_bootstrap(
    tmp_path: Path, isolated_state: Path
) -> None:
    acsm = tmp_path / "book.acsm"
    acsm.write_text(
        '<?xml version="1.0"?><adept:fulfillmentToken '
        'xmlns:adept="http://ns.adobe.com/adept"></adept:fulfillmentToken>',
        encoding="utf-8",
    )
    result = tools.tool_decrypt(str(acsm))
    assert result["status"] == "invalid_input"
    assert result["error_code"] == "state_not_initialized"


# --------------------------------------------------------------------------- #
# tool_upload_calibre input validation
# --------------------------------------------------------------------------- #


def test_upload_calibre_rejects_missing_settings(
    tmp_path: Path, isolated_state: Path
) -> None:
    book = tmp_path / "book.epub"
    book.write_bytes(b"PK\x03\x04")
    result = tools.tool_upload_calibre(str(book))
    assert result["status"] == "upload_failed"
    assert result["error_code"] == "calibre_not_configured"
    # The error message must not include secret field *values*, only names.
    _assert_no_secrets(result, ["correct-horse-battery-staple", "hunter2"])


def test_upload_calibre_rejects_wrong_extension(
    tmp_path: Path, isolated_state: Path
) -> None:
    src = tmp_path / "notes.txt"
    src.write_text("hi", encoding="utf-8")
    result = tools.tool_upload_calibre(str(src))
    assert result["status"] == "invalid_input"


# --------------------------------------------------------------------------- #
# Schema sanity: every tool function returns a dict with ``status``
# --------------------------------------------------------------------------- #


def test_all_tool_returns_have_status_field(
    tmp_path: Path, isolated_state: Path
) -> None:
    nonexistent = tmp_path / "missing.epub"
    results = [
        tools.tool_status(),
        tools.tool_decrypt(str(nonexistent)),
        tools.tool_upload_calibre(str(nonexistent)),
    ]
    for r in results:
        assert isinstance(r, dict)
        assert "status" in r
        assert r["status"] in {
            "ok",
            "not_drm",
            "wrong_key",
            "fulfillment_failed",
            "decrypt_failed",
            "upload_failed",
            "bootstrap_failed",
            "invalid_input",
        }

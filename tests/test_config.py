"""Tests for ade_dedrm.config: precedence, .env parsing, persistent save."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ade_dedrm import config
from ade_dedrm.config import (
    CalibreWebSettings,
    ConfigError,
    describe_sources,
    load_calibre_settings,
    parse_env_file,
    persistent_env_path,
    save_calibre_settings,
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ADE_DEDRM_HOME", str(home))
    for key in (
        config.ENV_CALIBRE_URL,
        config.ENV_CALIBRE_USERNAME,
        config.ENV_CALIBRE_PASSWORD,
        config.ENV_CALIBRE_VERIFY_TLS,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_parse_env_file_handles_quotes_comments_export(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        """
# a comment
ADE_DEDRM_CALIBRE_URL="http://example.com"
export ADE_DEDRM_CALIBRE_USERNAME='alice'
ADE_DEDRM_CALIBRE_PASSWORD=hunter2
BLANK=
""",
        encoding="utf-8",
    )
    parsed = parse_env_file(env)
    assert parsed["ADE_DEDRM_CALIBRE_URL"] == "http://example.com"
    assert parsed["ADE_DEDRM_CALIBRE_USERNAME"] == "alice"
    assert parsed["ADE_DEDRM_CALIBRE_PASSWORD"] == "hunter2"
    assert parsed["BLANK"] == ""


def test_persistent_env_loaded_when_no_cwd_env(_isolated_state: Path) -> None:
    persistent = persistent_env_path()
    persistent.write_text(
        "ADE_DEDRM_CALIBRE_URL=http://cw.local\n"
        "ADE_DEDRM_CALIBRE_USERNAME=alice\n"
        "ADE_DEDRM_CALIBRE_PASSWORD=hunter2\n",
        encoding="utf-8",
    )
    settings = load_calibre_settings()
    assert settings == CalibreWebSettings(
        url="http://cw.local",
        username="alice",
        password="hunter2",
        verify_tls=True,
    )


def test_cwd_env_preferred_over_state_env(_isolated_state: Path) -> None:
    persistent = persistent_env_path()
    persistent.write_text(
        "ADE_DEDRM_CALIBRE_URL=http://state\n"
        "ADE_DEDRM_CALIBRE_USERNAME=state-user\n"
        "ADE_DEDRM_CALIBRE_PASSWORD=state-pw\n",
        encoding="utf-8",
    )
    (Path.cwd() / ".env").write_text(
        "ADE_DEDRM_CALIBRE_URL=http://cwd\n"
        "ADE_DEDRM_CALIBRE_USERNAME=cwd-user\n"
        "ADE_DEDRM_CALIBRE_PASSWORD=cwd-pw\n",
        encoding="utf-8",
    )
    settings = load_calibre_settings()
    assert settings.url == "http://cwd"
    assert settings.username == "cwd-user"
    assert settings.password == "cwd-pw"


def test_process_env_overrides_env_file(
    _isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (Path.cwd() / ".env").write_text(
        "ADE_DEDRM_CALIBRE_URL=http://cw.local\n"
        "ADE_DEDRM_CALIBRE_USERNAME=alice\n"
        "ADE_DEDRM_CALIBRE_PASSWORD=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "from-env")
    settings = load_calibre_settings()
    assert settings.password == "from-env"


def test_cli_overrides_everything(
    _isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (Path.cwd() / ".env").write_text(
        "ADE_DEDRM_CALIBRE_URL=http://cw.local\n"
        "ADE_DEDRM_CALIBRE_USERNAME=alice\n"
        "ADE_DEDRM_CALIBRE_PASSWORD=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "from-env")
    settings = load_calibre_settings(cli_overrides={"password": "from-cli"})
    assert settings.password == "from-cli"


def test_missing_required_fields_raise(_isolated_state: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_calibre_settings()
    msg = str(exc_info.value)
    assert "url" in msg and "username" in msg and "password" in msg


def test_verify_tls_env_coercion(
    _isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_URL", "http://cw.local")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_USERNAME", "alice")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "p")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_VERIFY_TLS", "false")
    assert load_calibre_settings().verify_tls is False
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_VERIFY_TLS", "1")
    assert load_calibre_settings().verify_tls is True


def test_explicit_env_file_missing_raises(_isolated_state: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--env-file not found"):
        load_calibre_settings(env_file=tmp_path / "nope.env")


def test_save_calibre_settings_chmod_600(_isolated_state: Path) -> None:
    path = save_calibre_settings(
        {"url": "http://cw.local", "username": "alice", "password": "p"}
    )
    assert path == persistent_env_path()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    parsed = parse_env_file(path)
    assert parsed["ADE_DEDRM_CALIBRE_URL"] == "http://cw.local"
    assert parsed["ADE_DEDRM_CALIBRE_USERNAME"] == "alice"
    assert parsed["ADE_DEDRM_CALIBRE_PASSWORD"] == "p"


def test_save_calibre_settings_merges_and_preserves(_isolated_state: Path) -> None:
    path = persistent_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# my comment\n"
        "UNRELATED=keep-me\n"
        "ADE_DEDRM_CALIBRE_URL=http://old\n",
        encoding="utf-8",
    )
    save_calibre_settings({"url": "http://new", "username": "alice"})
    text = path.read_text(encoding="utf-8")
    assert "# my comment" in text
    assert "UNRELATED=keep-me" in text
    assert "ADE_DEDRM_CALIBRE_URL=http://new" in text
    assert "ADE_DEDRM_CALIBRE_USERNAME=alice" in text
    assert "ADE_DEDRM_CALIBRE_URL=http://old" not in text


def test_save_rejects_unknown_field(_isolated_state: Path) -> None:
    with pytest.raises(ConfigError, match="unknown Calibre Web setting"):
        save_calibre_settings({"bogus": "x"})


def test_describe_sources_effective(_isolated_state: Path) -> None:
    save_calibre_settings(
        {"url": "http://cw.local", "username": "alice", "password": "p"}
    )
    info = describe_sources()
    assert info["env_file_path"] == persistent_env_path()
    assert info["effective"].url == "http://cw.local"
    assert info["effective"].password == "p"
    assert info["missing"] == []


def test_describe_sources_reports_missing(_isolated_state: Path) -> None:
    info = describe_sources()
    assert info["effective"] is None
    assert "url" in " ".join(info["missing"])


def test_strips_trailing_slash_from_url(
    _isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_URL", "http://cw.local/")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_USERNAME", "alice")
    monkeypatch.setenv("ADE_DEDRM_CALIBRE_PASSWORD", "p")
    assert load_calibre_settings().url == "http://cw.local"

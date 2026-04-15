"""User-facing configuration for ade-dedrm.

Holds Calibre Web credentials only. Resolution order for each field
(high → low): CLI override, process environment, ``.env`` file. The
persistent store *is* the ``.env`` file — no separate JSON config.

``.env`` lookup order (first match wins):
  1. explicit path passed via ``env_file`` / ``--env-file``
  2. ``./.env`` in the current working directory
  3. ``<state_dir>/.env`` (e.g. ``~/.config/ade-dedrm/.env``)

The ``state_dir`` copy is where ``ade-dedrm config setup`` / ``config
set-calibre`` write, so values persist across sessions without landing
in random per-project ``.env`` files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ade_dedrm.adobe_state import state_dir

ENV_CALIBRE_URL = "ADE_DEDRM_CALIBRE_URL"
ENV_CALIBRE_USERNAME = "ADE_DEDRM_CALIBRE_USERNAME"
ENV_CALIBRE_PASSWORD = "ADE_DEDRM_CALIBRE_PASSWORD"
ENV_CALIBRE_VERIFY_TLS = "ADE_DEDRM_CALIBRE_VERIFY_TLS"

_CALIBRE_ENV_VARS = (
    ENV_CALIBRE_URL,
    ENV_CALIBRE_USERNAME,
    ENV_CALIBRE_PASSWORD,
    ENV_CALIBRE_VERIFY_TLS,
)


class ConfigError(Exception):
    pass


@dataclass
class CalibreWebSettings:
    url: str
    username: str
    password: str
    verify_tls: bool = True


def persistent_env_path() -> Path:
    """Canonical location for the ``.env`` written by ``config`` commands."""
    return state_dir() / ".env"


def _coerce_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, # comments, optional quotes, export prefix."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def find_env_file(explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"--env-file not found: {explicit}")
        return explicit
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env
    state_env = persistent_env_path()
    if state_env.is_file():
        return state_env
    return None


def _calibre_env_from_process() -> dict[str, str]:
    return {key: os.environ[key] for key in _CALIBRE_ENV_VARS if key in os.environ}


def _calibre_env_from_dotenv(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    return {k: v for k, v in parse_env_file(path).items() if k in _CALIBRE_ENV_VARS}


def load_calibre_settings(
    cli_overrides: dict | None = None,
    env_file: Path | None = None,
) -> CalibreWebSettings:
    cli = {k: v for k, v in (cli_overrides or {}).items() if v is not None}
    dotenv_path = find_env_file(env_file)
    dotenv_values = _calibre_env_from_dotenv(dotenv_path)
    process_values = _calibre_env_from_process()

    def pick(cli_key: str, env_key: str) -> str | None:
        if cli_key in cli and cli[cli_key] != "":
            return cli[cli_key]
        if env_key in process_values and process_values[env_key] != "":
            return process_values[env_key]
        if env_key in dotenv_values and dotenv_values[env_key] != "":
            return dotenv_values[env_key]
        return None

    url = pick("url", ENV_CALIBRE_URL)
    username = pick("username", ENV_CALIBRE_USERNAME)
    password = pick("password", ENV_CALIBRE_PASSWORD)

    verify_tls: bool
    if "verify_tls" in cli:
        verify_tls = bool(cli["verify_tls"])
    elif ENV_CALIBRE_VERIFY_TLS in process_values:
        verify_tls = _coerce_bool(process_values[ENV_CALIBRE_VERIFY_TLS])
    elif ENV_CALIBRE_VERIFY_TLS in dotenv_values:
        verify_tls = _coerce_bool(dotenv_values[ENV_CALIBRE_VERIFY_TLS])
    else:
        verify_tls = True

    missing = [
        name
        for name, value in (("url", url), ("username", username), ("password", password))
        if not value
    ]
    if missing:
        raise ConfigError(
            "missing Calibre Web settings: "
            + ", ".join(missing)
            + f". Set via CLI flags, env vars ({ENV_CALIBRE_URL} etc.), "
            "a .env file, or `ade-dedrm config setup`."
        )

    return CalibreWebSettings(
        url=str(url).rstrip("/"),
        username=str(username),
        password=str(password),
        verify_tls=verify_tls,
    )


def save_calibre_settings(partial: dict) -> Path:
    """Merge ``partial`` into the persistent ``.env`` file.

    ``partial`` uses dataclass field names (``url``/``username``/
    ``password``/``verify_tls``) and only keys that are present in it are
    rewritten. Existing lines, comments, and unrelated variables in the
    file are preserved. The file is created with mode ``0o600``.
    """
    field_to_env = {
        "url": ENV_CALIBRE_URL,
        "username": ENV_CALIBRE_USERNAME,
        "password": ENV_CALIBRE_PASSWORD,
        "verify_tls": ENV_CALIBRE_VERIFY_TLS,
    }
    updates: dict[str, str] = {}
    for field, value in partial.items():
        if value is None:
            continue
        env_key = field_to_env.get(field)
        if env_key is None:
            raise ConfigError(f"unknown Calibre Web setting: {field}")
        if field == "verify_tls":
            updates[env_key] = "true" if bool(value) else "false"
        else:
            updates[env_key] = str(value)

    path = persistent_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    existing_lines = (
        path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    )

    rewritten: list[str] = []
    seen: set[str] = set()
    for raw in existing_lines:
        stripped = raw.strip()
        body = stripped
        if body.startswith("export "):
            body = body[len("export ") :].lstrip()
        key = body.partition("=")[0].strip() if "=" in body else ""
        if key and key in updates:
            rewritten.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rewritten.append(raw)

    new_keys = [k for k in updates if k not in seen]
    if new_keys:
        if rewritten and rewritten[-1].strip() != "":
            rewritten.append("")
        for k in new_keys:
            rewritten.append(f"{k}={updates[k]}")

    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def describe_sources(
    env_file: Path | None = None,
) -> dict:
    """Return a structured snapshot of every credential source for display."""
    try:
        dotenv_path = find_env_file(env_file)
    except ConfigError:
        dotenv_path = None
    dotenv_values = _calibre_env_from_dotenv(dotenv_path)
    process_values = _calibre_env_from_process()

    try:
        effective: CalibreWebSettings | None = load_calibre_settings(env_file=env_file)
        missing: list[str] = []
    except ConfigError as exc:
        effective = None
        missing = [
            part.strip()
            for part in str(exc).split("missing Calibre Web settings:", 1)[-1]
            .split(".", 1)[0]
            .split(",")
        ]

    return {
        "env_file_path": dotenv_path,
        "env_file_values": dotenv_values,
        "process_env_values": process_values,
        "effective": effective,
        "missing": missing,
    }


__all__ = [
    "CalibreWebSettings",
    "ConfigError",
    "ENV_CALIBRE_PASSWORD",
    "ENV_CALIBRE_URL",
    "ENV_CALIBRE_USERNAME",
    "ENV_CALIBRE_VERIFY_TLS",
    "describe_sources",
    "find_env_file",
    "load_calibre_settings",
    "parse_env_file",
    "persistent_env_path",
    "save_calibre_settings",
]

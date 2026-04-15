"""Security helpers for the ade-dedrm MCP server.

The MCP model is "the caller (an LLM) is untrusted" — anything a tool
returns becomes part of the conversation, which is stored and
transmitted by the MCP host (e.g. Claude Desktop). This module provides
the primitives every tool must use to:

1. Validate input paths before reading/writing them.
2. Sanitize exceptions into short, generic messages that never contain
   credentials, full URLs with query strings, filesystem paths outside
   the user's chosen working directory, or stack traces.
3. Mask secrets in any string that might end up in a log or response.

These helpers are deliberately narrow. If a tool wants to return a new
kind of information, add a dedicated helper here first and audit it for
secret leakage, rather than interpolating data ad-hoc.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ade_dedrm.adobe_state import state_dir

#: Extensions ``decrypt`` is willing to touch. Anything else is rejected
#: before we even open the file, so a prompt-injected instruction to
#: "decrypt ~/.ssh/id_rsa" can't coerce the server into reading an
#: unrelated file.
ALLOWED_INPUT_EXTENSIONS = frozenset({".acsm", ".epub", ".pdf"})

#: Extensions ``upload_calibre`` is willing to upload.
ALLOWED_UPLOAD_EXTENSIONS = frozenset({".epub", ".pdf"})


class ToolInputError(Exception):
    """Raised when a tool input fails validation.

    The message is safe to return to the caller: it never contains
    credentials or paths outside what the caller already provided.
    """


@dataclass(frozen=True)
class SafeError:
    """Container for an error that is safe to surface to the MCP client.

    ``code`` is a stable machine-readable identifier; ``message`` is a
    short human-readable explanation already localized/generic enough to
    return verbatim.
    """

    code: str
    message: str

    def as_dict(self) -> dict:
        return {"error_code": self.code, "error": self.message}


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_input_path(raw_path: str, *, allow_upload: bool = False) -> Path:
    """Resolve and validate a path that came from the MCP caller.

    * Expands ``~`` and environment variables.
    * Rejects paths that resolve inside the ade-dedrm state directory
      (so the tool can't be coaxed into reading ``adobekey.der``).
    * Rejects paths whose suffix isn't in the allowlist.

    Raises :class:`ToolInputError` with a safe message on any violation.
    Returns the resolved :class:`Path` otherwise.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolInputError("path is required")

    expanded = Path(os.path.expandvars(raw_path)).expanduser()
    try:
        resolved = expanded.resolve(strict=False)
    except OSError:
        raise ToolInputError("path could not be resolved")

    state_root = state_dir().resolve()
    if _is_relative_to(resolved, state_root):
        raise ToolInputError(
            "refusing to operate on files inside the ade-dedrm state "
            "directory; these are managed by bootstrap_ade only"
        )

    if not resolved.is_file():
        raise ToolInputError("file does not exist or is not a regular file")

    ext = resolved.suffix.lower()
    allowed = ALLOWED_UPLOAD_EXTENSIONS if allow_upload else ALLOWED_INPUT_EXTENSIONS
    if ext not in allowed:
        pretty = ", ".join(sorted(allowed))
        raise ToolInputError(
            f"unsupported file type '{ext or '(none)'}'; allowed: {pretty}"
        )

    return resolved


def validate_output_path(
    raw_path: str | None,
    *,
    default: Path,
    input_path: Path,
    force: bool,
) -> Path:
    """Resolve a caller-supplied output path (or fall back to ``default``).

    Enforces:
      * Not inside the state directory.
      * Not equal to the input path.
      * Doesn't already exist unless ``force`` is True.
    """
    if raw_path is None or raw_path == "":
        resolved = default
    else:
        if not isinstance(raw_path, str):
            raise ToolInputError("output_path must be a string")
        resolved = Path(os.path.expandvars(raw_path)).expanduser().resolve(strict=False)

    state_root = state_dir().resolve()
    if _is_relative_to(resolved, state_root):
        raise ToolInputError(
            "refusing to write output inside the ade-dedrm state directory"
        )

    try:
        if resolved.resolve(strict=False) == input_path.resolve(strict=False):
            raise ToolInputError("output path must differ from input path")
    except OSError:
        raise ToolInputError("output path could not be resolved")

    if resolved.exists() and not force:
        raise ToolInputError(
            "output file already exists; pass force=true to overwrite"
        )

    return resolved


def sanitize_url(url: str | None) -> str:
    """Strip user/password and query string from a URL for safe display."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<invalid url>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port}"
    return urlunparse((parsed.scheme or "https", netloc, parsed.path or "", "", "", ""))


def url_host_only(url: str | None) -> str:
    """Return just the hostname of a URL, or empty string if unparseable.

    Used by ``status`` to show *which* Calibre server is configured
    without revealing the full URL or credentials.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return parsed.hostname or ""


#: Patterns of noisy substrings that are never safe to echo back, even
#: inside an error message. Matches are replaced with ``<redacted>``.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ``https://user:pass@host/...`` style credentials in URLs.
    re.compile(r"(https?://)[^/\s@]+:[^/\s@]+@", re.IGNORECASE),
    # ``Authorization: Bearer XXXX`` / ``Authorization: Basic XXXX``. We
    # deliberately match through the end of the logical line so the
    # scheme *and* the token are stripped, not just the first word.
    re.compile(r"(?i)\bauthorization:[^\r\n]+"),
    # Long base64 blobs (common for keys / pkcs12 dumps).
    re.compile(r"[A-Za-z0-9+/]{64,}={0,2}"),
)


def redact(value: str) -> str:
    """Return ``value`` with common secret-ish substrings replaced."""
    if not value:
        return ""
    out = value
    for pat in _REDACTION_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


def safe_error(code: str, message: str) -> SafeError:
    """Construct a ``SafeError`` whose ``message`` has been redacted."""
    return SafeError(code=code, message=redact(message))


__all__ = [
    "ALLOWED_INPUT_EXTENSIONS",
    "ALLOWED_UPLOAD_EXTENSIONS",
    "SafeError",
    "ToolInputError",
    "redact",
    "safe_error",
    "sanitize_url",
    "url_host_only",
    "validate_input_path",
    "validate_output_path",
]

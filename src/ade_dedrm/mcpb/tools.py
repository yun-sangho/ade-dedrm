"""Tool implementations for the ade-dedrm MCP server.

These are thin wrappers over the existing ``ade_dedrm`` functions that:

* Validate every input through :mod:`ade_dedrm.mcpb.security`.
* Return a :class:`dict` with a stable schema rather than exit codes.
* Never surface credentials, RSA keys, pkcs12 blobs, or raw exception
  strings to the caller.

Every return dict has a ``status`` field: ``"ok"``, ``"not_drm"``,
``"wrong_key"``, ``"fulfillment_failed"``, ``"upload_failed"``,
``"bootstrap_failed"``, or ``"invalid_input"``. The server module
(:mod:`ade_dedrm.mcpb.server`) decorates each of these with
``@mcp.tool()`` and translates ``ToolInputError`` into the same
``invalid_input`` shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ade_dedrm.adobe_state import DeviceState, state_dir
from ade_dedrm.config import ConfigError, load_calibre_settings
from ade_dedrm.mcpb.security import (
    ALLOWED_UPLOAD_EXTENSIONS,
    SafeError,
    ToolInputError,
    redact,
    url_host_only,
    validate_input_path,
    validate_output_path,
)

# --------------------------------------------------------------------------- #
# Return-dict builders
# --------------------------------------------------------------------------- #


def _ok(**fields) -> dict:
    return {"status": "ok", **fields}


def _fail(status: str, code: str, message: str, **fields) -> dict:
    safe_message = redact(message)
    return {
        "status": status,
        "error_code": code,
        "error": safe_message,
        **fields,
    }


def _from_safe_error(status: str, err: SafeError, **fields) -> dict:
    return {
        "status": status,
        "error_code": err.code,
        "error": err.message,
        **fields,
    }


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def tool_status() -> dict:
    """Report readiness of the ade-dedrm state + Calibre Web config.

    Returns only booleans and non-secret metadata. Never returns
    credentials, paths inside the state directory, or certificate
    contents.
    """
    state = DeviceState(root=state_dir())
    key_path = state.root / "adobekey.der"

    ade_ready = state.exists()
    key_ready = key_path.is_file()

    calibre_ok = False
    calibre_host = ""
    try:
        settings = load_calibre_settings()
    except ConfigError:
        settings = None
    if settings is not None:
        calibre_ok = True
        calibre_host = url_host_only(settings.url)

    return _ok(
        platform=sys.platform,
        state_dir=str(state.root),
        ade_state_ready=ade_ready,
        adobe_key_ready=key_ready,
        ready_to_decrypt=ade_ready and key_ready,
        calibre_configured=calibre_ok,
        calibre_host=calibre_host,
        hint=_status_hint(ade_ready, key_ready),
    )


def _status_hint(ade_ready: bool, key_ready: bool) -> str:
    if ade_ready and key_ready:
        return "Ready. Call decrypt(input_path=...) with an .acsm/.epub/.pdf file."
    if not ade_ready:
        return (
            "State not initialized. Call bootstrap_ade() on a macOS machine "
            "that has Adobe Digital Editions installed and authorized."
        )
    return (
        "State exists but adobekey.der is missing. Call bootstrap_ade(force=true) "
        "to re-derive the user key from the local ADE install."
    )


# --------------------------------------------------------------------------- #
# bootstrap_ade
# --------------------------------------------------------------------------- #


def tool_bootstrap_ade(force: bool = False) -> dict:
    """Import activation state from a local Adobe Digital Editions install.

    **macOS only.** On first call, macOS will display a keychain access
    prompt asking permission to read the ``DeviceKey`` and
    ``DeviceFingerprint`` entries under the ``Digital Editions`` service.
    Click ``Allow`` (or ``Always Allow`` to skip future prompts).

    The callers — particularly an LLM using this tool — SHOULD warn the
    user *before* invoking this tool that a macOS system prompt is about
    to appear, so the user isn't surprised by a keychain dialog.

    On success the tool writes:
        ~/.config/ade-dedrm/devicesalt      (mode 0600)
        ~/.config/ade-dedrm/activation.xml  (mode 0600)
        ~/.config/ade-dedrm/device.xml      (mode 0600)
        ~/.config/ade-dedrm/adobekey.der    (mode 0600)

    No secret material is returned in the tool response.
    """
    if not sys.platform.startswith("darwin"):
        return _fail(
            "bootstrap_failed",
            "unsupported_platform",
            f"bootstrap_ade is only supported on macOS (current platform: {sys.platform}). "
            "Copy an already-initialized ~/.config/ade-dedrm/ directory from a macOS machine.",
            platform=sys.platform,
        )

    # Imported lazily so Linux/Windows importers don't pull in macOS-only code.
    from ade_dedrm.adobe_import import ADEImportError, import_from_ade
    from ade_dedrm.keyfetch import ADEPTError as KeyError_
    from ade_dedrm.keyfetch import extract_adobe_key

    state = DeviceState(root=state_dir())
    if state.exists() and not force:
        return _ok(
            already_initialized=True,
            state_dir=str(state.root),
            message=(
                "ade-dedrm state already initialized. "
                "Pass force=true to re-import from Adobe Digital Editions."
            ),
        )

    try:
        import_from_ade(state)
    except ADEImportError as exc:
        return _fail(
            "bootstrap_failed",
            "ade_import_failed",
            str(exc),
            platform=sys.platform,
        )
    except PermissionError:
        return _fail(
            "bootstrap_failed",
            "keychain_denied",
            "macOS denied keychain access (DeviceKey/DeviceFingerprint). "
            "Click Allow on the system prompt and retry.",
        )
    except Exception:
        # Anything else: don't leak the exception text.
        return _fail(
            "bootstrap_failed",
            "ade_import_failed",
            "Failed to import Adobe Digital Editions state. "
            "Make sure ADE is installed and 'Help > Authorize Computer' "
            "has been completed.",
        )

    try:
        key_bytes, label = extract_adobe_key()
    except KeyError_ as exc:
        return _fail(
            "bootstrap_failed",
            "key_extract_failed",
            str(exc),
        )

    key_path = state.root / "adobekey.der"
    try:
        key_path.write_bytes(key_bytes)
        key_path.chmod(0o600)
    except OSError as exc:
        return _fail(
            "bootstrap_failed",
            "key_write_failed",
            f"Could not write adobekey.der: {exc.strerror or 'I/O error'}",
        )

    # Discard the in-memory key now that it's on disk. This is belt-and-
    # suspenders — Python GC will get it eventually, but there's no reason
    # to keep it resident while the server waits for the next tool call.
    key_bytes = b""
    del key_bytes
    del label

    return _ok(
        state_dir=str(state.root),
        message=(
            "ade-dedrm state initialized from local Adobe Digital Editions install. "
            "You can now call decrypt() with .acsm, .epub, or .pdf files."
        ),
    )


# --------------------------------------------------------------------------- #
# decrypt
# --------------------------------------------------------------------------- #


_FORMAT_EPUB_SUFFIX = ".nodrm.epub"
_FORMAT_PDF_SUFFIX = ".nodrm.pdf"


def _detect_drm_format(path: Path) -> str:
    """Return ``'epub'`` / ``'pdf'`` from magic bytes. Raise on anything else."""
    with path.open("rb") as fp:
        head = fp.read(4)
    if head.startswith(b"PK"):
        return "epub"
    if head.startswith(b"%PDF"):
        return "pdf"
    raise ToolInputError("file is neither an EPUB nor a PDF")


def _resolve_user_key() -> bytes:
    """Read the user's adobekey.der from the state directory.

    Raises :class:`ToolInputError` with a safe message if missing.
    """
    key_path = DeviceState(root=state_dir()).root / "adobekey.der"
    if not key_path.is_file():
        raise ToolInputError(
            "adobekey.der not found. Call bootstrap_ade() first."
        )
    try:
        return key_path.read_bytes()
    except OSError:
        raise ToolInputError("could not read adobekey.der")


def tool_decrypt(
    input_path: str,
    output_path: str | None = None,
    force: bool = False,
) -> dict:
    """Decrypt an ``.acsm`` ticket, a DRM-protected EPUB, or a DRM-protected PDF.

    Auto-detects the input by extension and magic bytes:

    * ``.acsm`` → performs the full ACS4 fulfill handshake, downloads
      the encrypted book from the operator, and decrypts it in memory.
      Requires ``bootstrap_ade`` to have been run successfully.
    * ``.epub`` → AES-CBC decrypts each encrypted entry, re-packs the
      ZIP without the Adept ``encryption.xml`` markers.
    * ``.pdf`` → unwraps the RSA-encrypted book key, decrypts every
      stream/string object, re-serializes the PDF without ``/Encrypt``.

    Parameters
    ----------
    input_path : str
        Absolute or home-relative path to an ``.acsm``, ``.epub``, or
        ``.pdf`` file.
    output_path : str, optional
        Where to write the cleaned file. Defaults to a sibling of the
        input with a ``.nodrm.epub`` / ``.nodrm.pdf`` suffix (or
        ``<stem>.epub`` / ``<stem>.pdf`` when the input is an ``.acsm``).
    force : bool, optional
        Overwrite ``output_path`` if it already exists.

    Returns
    -------
    dict
        On success: ``{"status": "ok", "output_path": ..., "format": ...}``.
        On failure: ``{"status": "<category>", "error_code": ..., "error": ...}``.
        Failure categories are ``invalid_input``, ``not_drm``,
        ``wrong_key``, ``fulfillment_failed``, or ``decrypt_failed``.
    """
    try:
        src = validate_input_path(input_path)
    except ToolInputError as exc:
        return _fail("invalid_input", "invalid_input", str(exc))

    if src.suffix.lower() == ".acsm":
        return _decrypt_acsm(src, output_path, force)
    return _decrypt_drm_file(src, output_path, force)


def _default_output(input_path: Path, suffix: str) -> Path:
    return input_path.with_name(input_path.stem + suffix)


def _decrypt_drm_file(
    src: Path, raw_output: str | None, force: bool
) -> dict:
    from ade_dedrm.epub import ADEPTError as EpubError
    from ade_dedrm.epub import decrypt_book
    from ade_dedrm.pdf import ADEPTError as PdfError
    from ade_dedrm.pdf import decrypt_pdf

    try:
        fmt = _detect_drm_format(src)
    except ToolInputError as exc:
        return _fail("invalid_input", "unknown_format", str(exc))

    default = _default_output(
        src, _FORMAT_EPUB_SUFFIX if fmt == "epub" else _FORMAT_PDF_SUFFIX
    )
    try:
        out = validate_output_path(
            raw_output, default=default, input_path=src, force=force
        )
    except ToolInputError as exc:
        return _fail("invalid_input", "invalid_output", str(exc))

    try:
        userkey = _resolve_user_key()
    except ToolInputError as exc:
        return _fail("invalid_input", "no_user_key", str(exc))

    try:
        if fmt == "epub":
            rc = decrypt_book(userkey, src, out)
        else:
            rc = decrypt_pdf(userkey, src, out)
    except (EpubError, PdfError) as exc:
        if out.exists():
            out.unlink(missing_ok=True)
        return _fail("decrypt_failed", "decrypt_failed", str(exc))
    except Exception:
        if out.exists():
            out.unlink(missing_ok=True)
        return _fail(
            "decrypt_failed",
            "decrypt_failed",
            f"Unexpected failure while decrypting {fmt.upper()}",
        )
    finally:
        del userkey  # drop the RSA key from the local frame ASAP

    if rc == 1:
        if out.exists():
            out.unlink(missing_ok=True)
        return _fail(
            "not_drm",
            "not_drm",
            f"{src.name} is not Adobe Adept DRM-protected.",
        )
    if rc == 2:
        if out.exists():
            out.unlink(missing_ok=True)
        return _fail(
            "wrong_key",
            "wrong_key",
            "adobekey.der does not match the key used to fulfill this book. "
            "Run bootstrap_ade(force=true) on the machine whose Adobe ID "
            "fulfilled the book.",
        )

    return _ok(
        output_path=str(out),
        format=fmt,
        message=f"Decrypted {src.name} -> {out.name}",
    )


def _decrypt_acsm(
    src: Path, raw_output: str | None, force: bool
) -> dict:
    from ade_dedrm.adobe_download import download_from_fulfill
    from ade_dedrm.adobe_fulfill import FulfillmentError, fulfill
    from ade_dedrm.adobe_http import AdeptHTTPError
    from ade_dedrm.epub import ADEPTError as EpubError
    from ade_dedrm.epub import decrypt_book
    from ade_dedrm.pdf import ADEPTError as PdfError
    from ade_dedrm.pdf import decrypt_pdf

    state = DeviceState(root=state_dir())
    if not state.exists():
        return _fail(
            "invalid_input",
            "state_not_initialized",
            "ade-dedrm state not initialized. Call bootstrap_ade() first.",
        )

    try:
        userkey = _resolve_user_key()
    except ToolInputError as exc:
        return _fail("invalid_input", "no_user_key", str(exc))

    try:
        reply = fulfill(state, src)
    except (FulfillmentError, AdeptHTTPError) as exc:
        return _fail("fulfillment_failed", "fulfillment_failed", str(exc))
    except Exception:
        return _fail(
            "fulfillment_failed",
            "fulfillment_failed",
            "Adobe ACS4 fulfillment request failed.",
        )

    tmp_path = src.with_suffix(".fulfill.drm.tmp")
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)

    try:
        try:
            _p, fmt = download_from_fulfill(state, reply, tmp_path)
        except (FulfillmentError, AdeptHTTPError) as exc:
            return _fail(
                "fulfillment_failed",
                "download_failed",
                str(exc),
            )
        except Exception:
            return _fail(
                "fulfillment_failed",
                "download_failed",
                "Download from Adobe operator failed.",
            )

        default = _default_output(src, f".{fmt}")
        try:
            out = validate_output_path(
                raw_output, default=default, input_path=src, force=force
            )
        except ToolInputError as exc:
            return _fail("invalid_input", "invalid_output", str(exc))

        try:
            if fmt == "epub":
                rc = decrypt_book(userkey, tmp_path, out)
            else:
                rc = decrypt_pdf(userkey, tmp_path, out)
        except (EpubError, PdfError) as exc:
            if out.exists():
                out.unlink(missing_ok=True)
            return _fail("decrypt_failed", "decrypt_failed", str(exc))
        except Exception:
            if out.exists():
                out.unlink(missing_ok=True)
            return _fail(
                "decrypt_failed",
                "decrypt_failed",
                f"Unexpected failure while decrypting fulfilled {fmt.upper()}.",
            )

        if rc == 1:
            if out.exists():
                out.unlink(missing_ok=True)
            return _fail(
                "not_drm",
                "not_drm",
                "Fulfilled file is not DRM-protected (unexpected).",
            )
        if rc == 2:
            if out.exists():
                out.unlink(missing_ok=True)
            return _fail(
                "wrong_key",
                "wrong_key",
                "adobekey.der does not match the fulfilled book's key.",
            )

        return _ok(
            output_path=str(out),
            format=fmt,
            message=f"Fulfilled and decrypted {src.name} -> {out.name}",
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        del userkey


# --------------------------------------------------------------------------- #
# upload_calibre
# --------------------------------------------------------------------------- #


def tool_upload_calibre(
    file_path: str,
    delete_after: bool = False,
) -> dict:
    """Upload an already-decrypted EPUB or PDF to the configured Calibre Web instance.

    Requires ``ADE_DEDRM_CALIBRE_URL``, ``ADE_DEDRM_CALIBRE_USERNAME``,
    and ``ADE_DEDRM_CALIBRE_PASSWORD`` to be set in the server's
    environment (MCPB passes these from ``user_config`` at launch). The
    credentials themselves are never returned by this tool.

    Parameters
    ----------
    file_path : str
        Path to the file to upload. Must be ``.epub`` or ``.pdf``.
    delete_after : bool, optional
        If True, delete ``file_path`` on the local disk after a
        successful upload. The file is left untouched on any failure.

    Returns
    -------
    dict
        On success:
            ``{"status": "ok", "calibre_host": "...", "book_path": "/book/123"}``
        On failure: ``{"status": "upload_failed", "error_code": ..., "error": ...}``.
    """
    from ade_dedrm.calibre_web import CalibreWebClient, CalibreWebError

    try:
        path = validate_input_path(file_path, allow_upload=True)
    except ToolInputError as exc:
        return _fail("invalid_input", "invalid_input", str(exc))

    if path.suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        return _fail(
            "invalid_input",
            "invalid_input",
            f"upload requires .epub or .pdf, got {path.suffix or '(none)'}",
        )

    try:
        settings = load_calibre_settings()
    except ConfigError as exc:
        return _fail(
            "upload_failed",
            "calibre_not_configured",
            # ConfigError already lists the missing fields without values.
            str(exc),
        )

    client = CalibreWebClient(settings.url, verify_tls=settings.verify_tls)
    try:
        client.login(settings.username, settings.password)
        result = client.upload(path)
    except CalibreWebError as exc:
        return _fail(
            "upload_failed",
            "calibre_upload_failed",
            str(exc),
            calibre_host=url_host_only(settings.url),
        )
    except Exception:
        return _fail(
            "upload_failed",
            "calibre_upload_failed",
            "Calibre Web upload failed unexpectedly.",
            calibre_host=url_host_only(settings.url),
        )

    book_id = result.get("book_id")
    location = result.get("location") or ""
    book_path = f"/book/{book_id}" if book_id is not None else location

    deleted = False
    if delete_after:
        try:
            path.unlink()
            deleted = True
        except OSError:
            deleted = False

    return _ok(
        calibre_host=url_host_only(settings.url),
        book_path=book_path,
        deleted_local_file=deleted,
        message=(
            f"Uploaded {path.name} to Calibre Web"
            + (" and deleted local copy." if deleted else ".")
        ),
    )


__all__ = [
    "tool_bootstrap_ade",
    "tool_decrypt",
    "tool_status",
    "tool_upload_calibre",
]

"""Command-line entry point for ade-dedrm."""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from pathlib import Path

from ade_dedrm import __version__
from ade_dedrm.epub import ADEPTError as EpubError
from ade_dedrm.epub import decrypt_book
from ade_dedrm.keyfetch import ADEPTError as KeyError_
from ade_dedrm.keyfetch import extract_adobe_key
from ade_dedrm.pdf import ADEPTError as PdfError
from ade_dedrm.pdf import decrypt_pdf

EXIT_OK = 0
EXIT_NOT_DRM = 1
EXIT_DECRYPT_FAIL = 2
EXIT_IO = 3
EXIT_FULFILL_FAIL = 4
EXIT_UPLOAD_FAIL = 5


def _invoked_name() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    name = os.path.basename(argv0) if argv0 else ""
    return name or "ade-dedrm"


def _add_calibre_flags(parser: argparse.ArgumentParser) -> None:
    """Attach shared Calibre Web credential flags to ``parser``."""
    parser.add_argument("--calibre-url", dest="calibre_url", default=None)
    parser.add_argument("--calibre-username", dest="calibre_username", default=None)
    parser.add_argument("--calibre-password", dest="calibre_password", default=None)
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument(
        "--calibre-verify-tls",
        dest="calibre_verify_tls",
        action="store_true",
        default=None,
        help="Verify Calibre Web TLS certificate (default).",
    )
    tls.add_argument(
        "--calibre-no-verify-tls",
        dest="calibre_verify_tls",
        action="store_false",
        help="Skip TLS verification when talking to Calibre Web.",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        type=Path,
        default=None,
        help="Path to a .env file to load Calibre Web credentials from.",
    )
    parser.add_argument(
        "--delete-after-upload",
        dest="delete_after_upload",
        action="store_true",
        help="Delete the local file after a successful Calibre Web upload.",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_invoked_name(),
        description="Fulfill ACSM files and remove Adobe Digital Editions (Adept) "
        "DRM from EPUB and PDF.",
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="Bootstrap state + user key from a local macOS ADE install.",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing ade-dedrm state and any -o target.",
    )
    init.add_argument(
        "-o",
        "--key-output",
        type=Path,
        default=None,
        help="Also write a copy of adobekey.der to this path.",
    )

    dec = sub.add_parser(
        "decrypt",
        help="Decrypt an Adobe Adept EPUB/PDF, or fulfill+decrypt a .acsm ticket.",
    )
    dec.add_argument(
        "-k",
        "--key",
        type=Path,
        default=None,
        help="Adobe user key .der file (default: <state_dir>/adobekey.der).",
    )
    dec.add_argument(
        "input",
        type=Path,
        help="Encrypted EPUB/PDF, or a .acsm fulfillment ticket.",
    )
    dec.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "Output file path (default: <input>.nodrm.<ext> for DRM input, "
            "<input_stem>.<ext> for .acsm input)."
        ),
    )
    dec.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    dec.add_argument(
        "--upload",
        action="store_true",
        help="Upload the decrypted file to Calibre Web after success.",
    )
    _add_calibre_flags(dec)

    up = sub.add_parser(
        "upload",
        help="Upload an already-decrypted EPUB/PDF to Calibre Web.",
    )
    up.add_argument("file", type=Path, help="File to upload.")
    _add_calibre_flags(up)

    cfg = sub.add_parser(
        "config",
        help="Manage persistent ade-dedrm settings (Calibre Web credentials).",
    )
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)

    cfg_set = cfg_sub.add_parser(
        "set-calibre",
        help="Save Calibre Web connection details to the persistent .env file.",
    )
    cfg_set.add_argument("--url", dest="url", default=None)
    cfg_set.add_argument("--username", dest="username", default=None)
    cfg_set.add_argument("--password", dest="password", default=None)
    cfg_tls = cfg_set.add_mutually_exclusive_group()
    cfg_tls.add_argument(
        "--verify-tls", dest="verify_tls", action="store_true", default=None
    )
    cfg_tls.add_argument(
        "--no-verify-tls", dest="verify_tls", action="store_false"
    )

    cfg_sub.add_parser("show", help="Print the current persisted settings.")
    cfg_sub.add_parser(
        "setup",
        help="Interactively prompt for Calibre Web url/username/password.",
    )

    return p


def _cmd_init(args: argparse.Namespace) -> int:
    from ade_dedrm.adobe_import import ADEImportError, import_from_ade
    from ade_dedrm.adobe_state import DeviceState, state_dir

    state = DeviceState(root=state_dir())
    if state.exists() and not args.force:
        print(
            f"error: {state.root} already populated (use --force to overwrite)",
            file=sys.stderr,
        )
        return EXIT_IO
    if (
        args.key_output is not None
        and args.key_output.exists()
        and not args.force
    ):
        print(
            f"error: {args.key_output} already exists (use --force to overwrite)",
            file=sys.stderr,
        )
        return EXIT_IO
    try:
        import_from_ade(state)
    except ADEImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO
    try:
        key, label = extract_adobe_key()
    except KeyError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO

    key_path = state.root / "adobekey.der"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    print(f"Initialized ade-dedrm state in {state.root} (key: '{label}')")
    if args.key_output is not None:
        args.key_output.parent.mkdir(parents=True, exist_ok=True)
        args.key_output.write_bytes(key)
        print(f"Also wrote key copy to {args.key_output}")
    return EXIT_OK


def _detect_format(path: Path) -> str:
    """Return 'epub' or 'pdf' based on file magic bytes."""
    with path.open("rb") as fp:
        head = fp.read(4)
    if head.startswith(b"PK"):
        return "epub"
    if head.startswith(b"%PDF"):
        return "pdf"
    raise ValueError(f"{path.name} is neither an EPUB nor a PDF (magic={head!r})")


def _is_acsm(path: Path) -> bool:
    return path.suffix.lower() == ".acsm"


_FORMAT_TAG_RE = re.compile(
    r"(?:[\s._\-]+[\(\[\{]?|[\(\[\{])\s*(?:epub|pdf)\s*[\)\]\}]?\s*$",
    re.IGNORECASE,
)


def _default_output(input_path: Path, ext: str) -> Path:
    stem = input_path.stem
    while True:
        stripped = _FORMAT_TAG_RE.sub("", stem).rstrip()
        if not stripped or stripped == stem:
            break
        stem = stripped
    return input_path.with_name(stem + ext)


def _validate_output(input_path: Path, output: Path, force: bool) -> int | None:
    if output.resolve() == input_path.resolve():
        print("error: input and output must be different files", file=sys.stderr)
        return EXIT_IO
    if output.exists() and not force:
        print(
            f"error: {output} already exists (use --force to overwrite)",
            file=sys.stderr,
        )
        return EXIT_IO
    return None


def _resolve_userkey(args: argparse.Namespace) -> bytes | int:
    """Resolve the Adobe user key bytes, or return an exit-code int on failure.

    Order: explicit --key > <state_dir>/adobekey.der > extract_adobe_key().
    """
    from ade_dedrm.adobe_state import DeviceState, state_dir

    if args.key is not None:
        if not args.key.is_file():
            print(f"error: key file not found: {args.key}", file=sys.stderr)
            return EXIT_IO
        return args.key.read_bytes()

    candidate = DeviceState(root=state_dir()).root / "adobekey.der"
    if candidate.is_file():
        return candidate.read_bytes()

    try:
        userkey, _ = extract_adobe_key()
    except KeyError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "error: no user key available. Pass -k, or run `ade-dedrm init` first.",
            file=sys.stderr,
        )
        return EXIT_IO
    return userkey


def _cmd_decrypt(args: argparse.Namespace) -> int:
    if not args.input.is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return EXIT_IO

    if _is_acsm(args.input):
        return _handle_acsm(args)
    return _handle_drm_file(args)


def _handle_drm_file(args: argparse.Namespace) -> int:
    try:
        fmt = _detect_format(args.input)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO

    userkey = _resolve_userkey(args)
    if isinstance(userkey, int):
        return userkey

    default_ext = ".nodrm.epub" if fmt == "epub" else ".nodrm.pdf"
    output = args.output or _default_output(args.input, default_ext)
    validation = _validate_output(args.input, output, args.force)
    if validation is not None:
        return validation

    try:
        if fmt == "epub":
            result = decrypt_book(userkey, args.input, output)
        else:
            result = decrypt_pdf(userkey, args.input, output)
    except (EpubError, PdfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if output.exists():
            output.unlink()
        return EXIT_DECRYPT_FAIL
    except Exception:
        print("error: decryption failed:", file=sys.stderr)
        traceback.print_exc()
        if output.exists():
            output.unlink()
        return EXIT_DECRYPT_FAIL

    if result == 1:
        print(f"{args.input.name} is not Adobe Adept DRM-protected.")
        if output.exists():
            output.unlink()
        return EXIT_NOT_DRM
    if result == 2:
        print(f"error: wrong key for {args.input.name}", file=sys.stderr)
        if output.exists():
            output.unlink()
        return EXIT_DECRYPT_FAIL

    print(f"Decrypted {args.input.name} -> {output}")
    if getattr(args, "upload", False):
        return _upload_after_decrypt(args, output)
    return EXIT_OK


def _handle_acsm(args: argparse.Namespace) -> int:
    from ade_dedrm.adobe_download import download_from_fulfill
    from ade_dedrm.adobe_fulfill import FulfillmentError, fulfill
    from ade_dedrm.adobe_http import AdeptHTTPError
    from ade_dedrm.adobe_state import DeviceState, state_dir

    state = DeviceState(root=state_dir())
    if not state.exists():
        print(
            "error: no ade-dedrm activation state found. Run `ade-dedrm init` first.",
            file=sys.stderr,
        )
        return EXIT_IO

    userkey = _resolve_userkey(args)
    if isinstance(userkey, int):
        return userkey

    try:
        reply = fulfill(state, args.input)
    except (FulfillmentError, AdeptHTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FULFILL_FAIL

    tmp_path = args.input.with_suffix(".fulfill.drm.tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        _p, fmt = download_from_fulfill(state, reply, tmp_path)
    except (FulfillmentError, AdeptHTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if tmp_path.exists():
            tmp_path.unlink()
        return EXIT_FULFILL_FAIL

    default_ext = f".{fmt}"
    resolved = args.output or _default_output(args.input, default_ext)
    validation = _validate_output(args.input, resolved, args.force)
    if validation is not None:
        tmp_path.unlink()
        return validation

    try:
        if fmt == "epub":
            rc = decrypt_book(userkey, tmp_path, resolved)
        else:
            rc = decrypt_pdf(userkey, tmp_path, resolved)
    except (EpubError, PdfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if resolved.exists():
            resolved.unlink()
        return EXIT_DECRYPT_FAIL
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    if rc == 1:
        print("error: fulfilled file is not DRM-protected (unexpected)", file=sys.stderr)
        if resolved.exists():
            resolved.unlink()
        return EXIT_DECRYPT_FAIL
    if rc == 2:
        print("error: wrong key for fulfilled book", file=sys.stderr)
        if resolved.exists():
            resolved.unlink()
        return EXIT_DECRYPT_FAIL

    print(f"Decrypted {args.input.name} -> {resolved} ({fmt})")
    if getattr(args, "upload", False):
        return _upload_after_decrypt(args, resolved)
    return EXIT_OK


def _cli_calibre_overrides(args: argparse.Namespace) -> dict:
    overrides = {
        "url": getattr(args, "calibre_url", None),
        "username": getattr(args, "calibre_username", None),
        "password": getattr(args, "calibre_password", None),
    }
    verify_tls = getattr(args, "calibre_verify_tls", None)
    if verify_tls is not None:
        overrides["verify_tls"] = verify_tls
    return overrides


def _upload_file(args: argparse.Namespace, path: Path) -> int:
    from ade_dedrm.calibre_web import CalibreWebClient, CalibreWebError
    from ade_dedrm.config import ConfigError, load_calibre_settings

    try:
        settings = load_calibre_settings(
            cli_overrides=_cli_calibre_overrides(args),
            env_file=getattr(args, "env_file", None),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO

    client = CalibreWebClient(settings.url, verify_tls=settings.verify_tls)
    try:
        client.login(settings.username, settings.password)
        result = client.upload(path)
    except CalibreWebError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UPLOAD_FAIL

    book_id = result.get("book_id")
    if book_id is not None:
        print(f"Uploaded {path.name} -> {settings.url}/book/{book_id}")
    else:
        location = result.get("location") or "/"
        print(f"Uploaded {path.name} -> {settings.url}{location}")

    if getattr(args, "delete_after_upload", False):
        try:
            path.unlink()
        except OSError as exc:
            print(
                f"warning: could not delete {path}: {exc}",
                file=sys.stderr,
            )
        else:
            print(f"Deleted local file {path}")
    return EXIT_OK


def _upload_after_decrypt(args: argparse.Namespace, path: Path) -> int:
    rc = _upload_file(args, path)
    if rc != EXIT_OK:
        print(
            f"warning: decrypted file kept at {path} despite upload failure",
            file=sys.stderr,
        )
    return rc


def _cmd_upload(args: argparse.Namespace) -> int:
    if not args.file.is_file():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return EXIT_IO
    return _upload_file(args, args.file)


_CALIBRE_KEYS_ORDERED = ("url", "username", "password", "verify_tls")
_SECRET_KEYS = {"password"}


def _cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "set-calibre":
        return _cmd_config_set_calibre(args)
    if args.config_command == "setup":
        return _cmd_config_setup()
    if args.config_command == "show":
        return _cmd_config_show()
    return EXIT_IO


def _collect_set_calibre_partial(args: argparse.Namespace) -> dict:
    partial: dict = {}
    for field in ("url", "username", "password"):
        value = getattr(args, field, None)
        if value is not None:
            partial[field] = value
    if args.verify_tls is not None:
        partial["verify_tls"] = bool(args.verify_tls)
    return partial


def _cmd_config_set_calibre(args: argparse.Namespace) -> int:
    from ade_dedrm.config import ConfigError, save_calibre_settings

    partial = _collect_set_calibre_partial(args)
    if not partial:
        print(
            "error: nothing to save (pass --url/--username/--password/--verify-tls "
            "or use `ade-dedrm config setup` for an interactive prompt)",
            file=sys.stderr,
        )
        return EXIT_IO
    try:
        path = save_calibre_settings(partial)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO
    print(f"Saved Calibre Web settings to {path}")
    return EXIT_OK


def _cmd_config_setup() -> int:
    import getpass

    from ade_dedrm.config import (
        ConfigError,
        describe_sources,
        save_calibre_settings,
    )

    sources = describe_sources()
    effective = sources.get("effective")
    env_file_values = sources.get("env_file_values") or {}
    process_values = sources.get("process_env_values") or {}

    current_url = (
        (effective.url if effective else None)
        or process_values.get("ADE_DEDRM_CALIBRE_URL")
        or env_file_values.get("ADE_DEDRM_CALIBRE_URL")
    )
    current_username = (
        (effective.username if effective else None)
        or process_values.get("ADE_DEDRM_CALIBRE_USERNAME")
        or env_file_values.get("ADE_DEDRM_CALIBRE_USERNAME")
    )
    has_existing_pw = bool(effective.password if effective else None) or bool(
        process_values.get("ADE_DEDRM_CALIBRE_PASSWORD")
        or env_file_values.get("ADE_DEDRM_CALIBRE_PASSWORD")
    )

    print("Interactive Calibre Web setup. Press Enter to keep the existing value.")

    def _prompt(label: str, default: str | None) -> str:
        hint = f" [{default}]" if default else ""
        try:
            entered = input(f"{label}{hint}: ").strip()
        except EOFError:
            print()
            return ""
        return entered or (default or "")

    url = _prompt("URL", current_url)
    username = _prompt("Username", current_username)
    pw_hint = " [keep existing]" if has_existing_pw else ""
    try:
        password = getpass.getpass(f"Password{pw_hint}: ")
    except EOFError:
        print()
        password = ""

    partial: dict = {}
    if url:
        partial["url"] = url
    if username:
        partial["username"] = username
    if password:
        partial["password"] = password

    if not partial:
        print("nothing entered, nothing saved")
        return EXIT_OK

    try:
        path = save_calibre_settings(partial)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO

    print(f"Saved Calibre Web settings to {path}")
    return EXIT_OK


def _cmd_config_show() -> int:
    from ade_dedrm.config import describe_sources, persistent_env_path

    sources = describe_sources()

    env_file = sources.get("env_file_path")
    env_file_values = sources.get("env_file_values") or {}
    if env_file is not None:
        print(f"# .env file: {env_file}")
        if not env_file_values:
            print("  (no ADE_DEDRM_CALIBRE_* vars in this file)")
        else:
            for var in (
                "ADE_DEDRM_CALIBRE_URL",
                "ADE_DEDRM_CALIBRE_USERNAME",
                "ADE_DEDRM_CALIBRE_PASSWORD",
                "ADE_DEDRM_CALIBRE_VERIFY_TLS",
            ):
                if var in env_file_values:
                    display = "***" if "PASSWORD" in var else env_file_values[var]
                    print(f"  {var}: {display}")
    else:
        default = persistent_env_path()
        print(f"# .env file: (none found; default save location: {default})")

    process_env_values = sources.get("process_env_values") or {}
    print("\n# process environment:")
    if not process_env_values:
        print("  (no ADE_DEDRM_CALIBRE_* vars set)")
    else:
        for var in (
            "ADE_DEDRM_CALIBRE_URL",
            "ADE_DEDRM_CALIBRE_USERNAME",
            "ADE_DEDRM_CALIBRE_PASSWORD",
            "ADE_DEDRM_CALIBRE_VERIFY_TLS",
        ):
            if var in process_env_values:
                display = "***" if "PASSWORD" in var else process_env_values[var]
                print(f"  {var}: {display}")

    effective = sources.get("effective")
    print("\n# effective settings:")
    if effective is None:
        missing = sources.get("missing") or []
        print("  (incomplete — missing: " + ", ".join(missing) + ")")
        print(
            "  hint: run `ade-dedrm config setup` or set "
            "ADE_DEDRM_CALIBRE_URL/USERNAME/PASSWORD",
        )
    else:
        for key in _CALIBRE_KEYS_ORDERED:
            value = getattr(effective, key)
            if key in _SECRET_KEYS and value:
                value = "***"
            print(f"  {key}: {value}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "decrypt":
        return _cmd_decrypt(args)
    if args.command == "upload":
        return _cmd_upload(args)
    if args.command == "config":
        return _cmd_config(args)
    parser.error(f"unknown command: {args.command}")
    return EXIT_IO

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


def _invoked_name() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    name = os.path.basename(argv0) if argv0 else ""
    return name or "ade-dedrm"


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
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "decrypt":
        return _cmd_decrypt(args)
    parser.error(f"unknown command: {args.command}")
    return EXIT_IO

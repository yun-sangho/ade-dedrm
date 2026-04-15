"""FastMCP server entry point for ade-dedrm.

Exposes four tools to any MCP host (Claude Desktop, etc.):

* ``status`` — check readiness; returns booleans and non-secret metadata.
* ``bootstrap_ade`` — import state from a local Adobe Digital Editions
  install (macOS only). Triggers a macOS keychain prompt on first use.
* ``decrypt`` — fulfill an ``.acsm`` and/or strip Adept DRM from an
  ``.epub`` / ``.pdf``.
* ``upload_calibre`` — send an already-decrypted file to the configured
  Calibre Web instance.

Run with:

    python -m ade_dedrm.mcpb.server
    # or, after `uv sync --extra mcp`:
    ade-dedrm-mcp

The server speaks MCP over stdio. Log messages go to stderr (never to
stdout, which is reserved for the MCP JSON-RPC stream) and are *not*
visible to the LLM caller.
"""

from __future__ import annotations

import logging
import sys

from ade_dedrm.mcpb import tools

# Logs go to stderr only. stdout is reserved for the MCP stdio transport;
# writing anything there corrupts the JSON-RPC stream.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s ade-dedrm-mcp %(levelname)s %(message)s",
)
log = logging.getLogger("ade_dedrm.mcpb")


def _load_fastmcp():
    """Import FastMCP lazily so ``ade-dedrm-mcp --help``-style invocations
    with the optional ``mcp`` extra missing still give a clear message.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit(
            "ade-dedrm-mcp requires the optional 'mcp' dependency. "
            "Install it with: uv sync --extra mcp  (or: pip install 'ade-dedrm[mcp]')"
        ) from exc
    return FastMCP


def _build_server():
    FastMCP = _load_fastmcp()

    mcp = FastMCP(
        "ade-dedrm",
        instructions=(
            "Remove Adobe Digital Editions (Adept) DRM from legitimately "
            "purchased ebooks, and optionally upload the cleaned file to a "
            "self-hosted Calibre Web instance. "
            "Call status() first to check readiness. On macOS, call "
            "bootstrap_ade() once to import state from a local ADE install; "
            "this triggers a macOS keychain prompt, so warn the user first."
        ),
    )

    @mcp.tool()
    def status() -> dict:
        """Return a readiness snapshot of the ade-dedrm state and Calibre Web config.

        Fields (all safe to display):
            platform            : 'darwin' | 'linux' | 'win32'
            state_dir           : absolute path to the ade-dedrm state directory
            ade_state_ready     : bool, True if devicesalt+activation+device are present
            adobe_key_ready     : bool, True if adobekey.der is present
            ready_to_decrypt    : bool, True if both of the above are present
            calibre_configured  : bool, True if URL+username+password are set
            calibre_host        : str, hostname only (no credentials, no full URL)
            hint                : str, human-readable next step

        Never returns credentials, private keys, file contents, or raw paths
        inside the state directory.
        """
        return tools.tool_status()

    @mcp.tool()
    def bootstrap_ade(force: bool = False) -> dict:
        """Import Adobe Digital Editions activation state from the local install (macOS only).

        IMPORTANT — BEFORE CALLING THIS TOOL, warn the user that macOS will
        display a keychain access prompt ("...wants to access key DeviceKey
        in your keychain"). They must click 'Allow' (or 'Always Allow') for
        the bootstrap to succeed. The prompt may appear behind other
        windows, so ask them to check their notifications.

        Preconditions (user must verify):
            1. Running on macOS.
            2. Adobe Digital Editions is installed from adobe.com.
            3. ADE has been authorized with the user's Adobe ID via
               Help > Authorize Computer.

        Parameters
        ----------
        force : bool, optional
            If True, overwrite an existing ade-dedrm state directory.
            Default False — on a second call the tool returns
            already_initialized=True without touching anything.

        Returns
        -------
        dict
            Success shape: ``{"status": "ok", "state_dir": str, "message": str}``.
            If already initialized: adds ``"already_initialized": true``.
            Failure shape: ``{"status": "bootstrap_failed", "error_code": ..., "error": ...}``
            where ``error_code`` is one of ``unsupported_platform``,
            ``ade_import_failed``, ``keychain_denied``, ``key_extract_failed``,
            or ``key_write_failed``.
        """
        return tools.tool_bootstrap_ade(force=force)

    @mcp.tool()
    def decrypt(
        input_path: str,
        output_path: str | None = None,
        force: bool = False,
    ) -> dict:
        """Decrypt an .acsm, Adept-protected .epub, or Adept-protected .pdf.

        Auto-detects the input type:
          * .acsm → full ACS4 fulfillment + download + decrypt in one call.
            Requires that bootstrap_ade() has been run successfully.
          * .epub (PK magic) → AES-CBC decrypt, re-pack the ZIP without
            Adept markers. Output suffix: .nodrm.epub.
          * .pdf (%PDF magic) → unwrap the RSA-encrypted book key, decrypt
            every stream/string, re-serialize without /Encrypt. Output
            suffix: .nodrm.pdf.

        Parameters
        ----------
        input_path : str
            Path to the file. ``~`` and environment variables are expanded.
            Only ``.acsm``, ``.epub``, and ``.pdf`` are accepted. Paths
            inside the ade-dedrm state directory are refused.
        output_path : str, optional
            Where to write the output. Defaults to a sibling of the input.
        force : bool, optional
            Overwrite an existing output file.

        Returns
        -------
        dict
            Success: ``{"status": "ok", "output_path": str, "format": "epub"|"pdf", ...}``
            Failures (``status`` value → what happened):
              * ``"invalid_input"`` — path validation failed
              * ``"not_drm"`` — file isn't Adept-protected
              * ``"wrong_key"`` — adobekey.der doesn't match
              * ``"fulfillment_failed"`` — ACS4 server rejected/errored
              * ``"decrypt_failed"`` — decryption raised an unexpected error
        """
        return tools.tool_decrypt(
            input_path=input_path, output_path=output_path, force=force
        )

    @mcp.tool()
    def upload_calibre(
        file_path: str,
        delete_after: bool = False,
    ) -> dict:
        """Upload a decrypted .epub or .pdf to the configured Calibre Web instance.

        Credentials are read from the server's environment
        (ADE_DEDRM_CALIBRE_URL / _USERNAME / _PASSWORD), which MCPB
        populates from user_config at launch. The credentials themselves
        are never returned by this tool.

        Parameters
        ----------
        file_path : str
            Path to the .epub or .pdf to upload.
        delete_after : bool, optional
            Delete the local file after a successful upload. Left
            untouched on any failure.

        Returns
        -------
        dict
            Success: ``{"status": "ok", "calibre_host": str, "book_path": str, "deleted_local_file": bool, ...}``
            Failure: ``{"status": "upload_failed", "error_code": ..., "error": ...}``
            where ``error_code`` is one of ``calibre_not_configured`` or
            ``calibre_upload_failed``.
        """
        return tools.tool_upload_calibre(
            file_path=file_path, delete_after=delete_after
        )

    return mcp


def main() -> None:
    """Entry point for ``ade-dedrm-mcp`` / ``python -m ade_dedrm.mcpb.server``."""
    log.info("starting ade-dedrm MCP server (stdio transport)")
    server = _build_server()
    server.run()  # FastMCP defaults to stdio


if __name__ == "__main__":
    main()

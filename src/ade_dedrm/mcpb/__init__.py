"""MCP (Model Context Protocol) server for ade-dedrm.

Exposes the existing ``ade_dedrm`` functionality as MCP tools so Claude
Desktop (or any other MCP host) can decrypt ACSM/EPUB/PDF files via
natural language, without the user touching a terminal.

The server is packaged as an MCP Bundle (``.mcpb``) using the ``uv``
runtime type, which means the host application manages Python/uv and the
user never needs to install a runtime manually.

Design rules (see ``security.py`` for enforcement):

1. No tool returns credentials, keys, or any secret material.
2. No generic read_file/run_command/show_config tools exist.
3. Input paths are validated against a known-extension allowlist and
   refused if they point inside the ade-dedrm state directory.
4. All error messages shown to the model are sanitized.
"""

from __future__ import annotations

__all__ = ["server", "tools", "security"]

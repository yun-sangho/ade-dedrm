"""Extract the Adobe ADE user key from a local macOS install.

Ported from DeDRM_tools/DeDRM_plugin/adobekey.py (macOS branch only).
Original copyright (C) 2009-2022 i♥cabbages, Apprentice Harper et al.
Licensed under GPL v3. See NOTICE for attribution.
"""

from __future__ import annotations

import sys
from base64 import b64decode
from pathlib import Path

from lxml import etree

NSMAP = {"adept": "http://ns.adobe.com/adept"}

# ADE 4.x on modern macOS stores data under ~/Documents/Digital Editions.
# Older ADE versions used ~/Library/Application Support/Adobe/Digital Editions.
ADE_SEARCH_ROOTS = (
    Path.home() / "Documents/Digital Editions",
    Path.home() / "Library/Application Support/Adobe/Digital Editions",
)


class ADEPTError(Exception):
    pass


def _find_activation_dat() -> Path | None:
    for root in ADE_SEARCH_ROOTS:
        direct = root / "activation.dat"
        if direct.is_file():
            return direct
    for root in ADE_SEARCH_ROOTS:
        if root.is_dir():
            for candidate in root.rglob("activation.dat"):
                if candidate.is_file():
                    return candidate
    return None


def extract_adobe_key() -> tuple[bytes, str]:
    """Return (der_key_bytes, key_label) for the active ADE user.

    The label is synthesized from the credentials' UUID / account and is only
    informative — the caller typically just writes the bytes to a .der file.
    """
    if not sys.platform.startswith("darwin"):
        raise ADEPTError("Key extraction is only supported on macOS.")

    actpath = _find_activation_dat()
    if actpath is None:
        raise ADEPTError(
            "Could not find activation.dat. Launch Adobe Digital Editions and "
            "run Help > Authorize Computer... with your Adobe ID, then try again."
        )

    tree = etree.parse(str(actpath))
    adept = lambda tag: "{%s}%s" % (NSMAP["adept"], tag)

    pk_expr = f".//{adept('credentials')}/{adept('privateLicenseKey')}"
    pk_text = tree.findtext(pk_expr)
    if not pk_text:
        raise ADEPTError("activation.dat is missing <privateLicenseKey>.")

    # The first 26 bytes are Adobe's internal wrapper; the remainder is a
    # PKCS#1 RSA private key in DER form.
    userkey = b64decode(pk_text)[26:]

    label_parts: list[str] = []
    user_uuid = tree.findtext(f".//{adept('credentials')}/{adept('user')}") or ""
    if user_uuid.startswith("urn:uuid:"):
        label_parts.append(user_uuid[9:])
    username_elem = tree.find(f".//{adept('credentials')}/{adept('username')}")
    if username_elem is not None:
        method = username_elem.attrib.get("method")
        if method:
            label_parts.append(method)
        if username_elem.text:
            label_parts.append(username_elem.text)
    label = "_".join(label_parts) if label_parts else "Unknown"

    return userkey, label

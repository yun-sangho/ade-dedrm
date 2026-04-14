"""Import an existing Adobe Digital Editions activation on macOS.

Copies activation.dat, pulls DeviceKey/DeviceFingerprint from the macOS
keychain, then reassembles device.xml so the fulfillment flow has all
three state files it needs.

Ported from DeACSM/libadobeImportAccount.py (macOS branch).
"""

from __future__ import annotations

import base64
import locale
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from lxml import etree

from ade_dedrm.adobe_state import ADEPT_NS, DeviceState, state_dir

# These are the ADE version strings we'll claim to be. Must stay in sync
# with the lists in DeACSM/libadobe.py.
ADE_VERSION = {
    "build_id": 78765,
    "hobbes": "9.3.58046",
    "client_version": "2.0.1.78765",
    "client_os": "Windows Vista",  # macOS ADE lies about its OS, so we match.
}

ADE_ACTIVATION_SOURCES = (
    Path.home() / "Library/Application Support/Adobe/Digital Editions/activation.dat",
    Path.home() / "Documents/Digital Editions/activation.dat",
)


class ADEImportError(Exception):
    pass


def _find_activation_source() -> Path:
    for candidate in ADE_ACTIVATION_SOURCES:
        if candidate.is_file():
            return candidate
    # Fall back to a rglob over the common roots in case ADE installed
    # into a slightly different subdirectory.
    roots = [p.parent for p in ADE_ACTIVATION_SOURCES]
    for root in roots:
        if root.is_dir():
            for hit in root.rglob("activation.dat"):
                if hit.is_file():
                    return hit
    raise ADEImportError(
        "Could not find Adobe Digital Editions activation.dat. "
        "Make sure ADE is installed and Help > Authorize Computer has been run."
    )


def _mac_keychain_credential(label: str) -> bytes:
    """Read a generic password from the macOS keychain.

    Invokes the `security` binary so the system permission prompt (if any)
    is raised by Apple's own UI. Returns the raw bytes stored under
    (service="Digital Editions", account=label).
    """
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-g",
            "-s",
            "Digital Editions",
            "-a",
            label,
        ],
        capture_output=True,
        text=True,
    )
    # `security -g` prints the password line on stderr, payload on stdout.
    blob = result.stderr + result.stdout
    match = re.search(r'password:\s*(?:0x([0-9A-Fa-f]+)\s*)?"([^"]*)"', blob)
    if not match:
        raise ADEImportError(
            f"Could not read '{label}' from macOS keychain (service='Digital Editions'). "
            "Cancel? Deny? Check Keychain Access.app for the 'Digital Editions' entry."
        )
    hex_form, str_form = match.groups()
    if hex_form:
        return bytes.fromhex(hex_form)
    return str_form.encode("latin-1")


def _build_device_xml(device_type: str, fingerprint_b64: bytes) -> str:
    adept = lambda tag: f"adept:{tag}"  # noqa: E731
    try:
        language = (locale.getdefaultlocale()[0] or "en").split("_")[0]
    except Exception:
        language = "en"

    try:
        hostname = platform.uname().node or "unknown"
    except Exception:
        hostname = "unknown"

    # Serial is purely cosmetic for our use case — use a stable host-derived
    # value so re-imports don't churn the file.
    serial = hostname[:16].ljust(16, "x")

    parts = [
        '<?xml version="1.0"?>',
        '<adept:deviceInfo xmlns:adept="http://ns.adobe.com/adept">',
        f"<{adept('deviceType')}>{device_type}</{adept('deviceType')}>",
        f"<{adept('deviceClass')}>Desktop</{adept('deviceClass')}>",
        f"<{adept('deviceSerial')}>{serial}</{adept('deviceSerial')}>",
        f"<{adept('deviceName')}>{hostname}</{adept('deviceName')}>",
        f'<{adept("version")} name="hobbes" value="{ADE_VERSION["hobbes"]}"/>',
        f'<{adept("version")} name="clientOS" value="{ADE_VERSION["client_os"]}"/>',
        f'<{adept("version")} name="clientLocale" value="{language}"/>',
        f"<{adept('fingerprint')}>{fingerprint_b64.decode('ascii')}</{adept('fingerprint')}>",
        "</adept:deviceInfo>",
    ]
    return "\n".join(parts)


def import_from_ade(state: DeviceState | None = None) -> DeviceState:
    """Populate the state directory from a local macOS ADE install.

    Returns the DeviceState on success. Raises ADEImportError otherwise.
    """
    if not sys.platform.startswith("darwin"):
        raise ADEImportError("Importing an ADE activation is only supported on macOS.")

    state = state or DeviceState(root=state_dir())
    state.ensure_dir()

    source = _find_activation_source()

    device_key = _mac_keychain_credential("DeviceKey")
    if len(device_key) != 16:
        raise ADEImportError(
            f"DeviceKey from keychain has unexpected length ({len(device_key)} bytes, expected 16)."
        )
    device_fingerprint = _mac_keychain_credential("DeviceFingerprint")

    # Write devicesalt (raw 16 bytes)
    state.devicesalt.write_bytes(device_key)

    # Copy activation.dat → activation.xml verbatim
    shutil.copyfile(source, state.activation_xml)

    # Extract deviceType from the copied activation.xml so our synthetic
    # device.xml matches.
    activation = etree.parse(str(state.activation_xml))
    dev_type_elem = activation.find(
        f"./{{{ADEPT_NS}}}activationToken/{{{ADEPT_NS}}}deviceType"
    )
    device_type = dev_type_elem.text if dev_type_elem is not None else "standalone"

    fingerprint_b64 = base64.b64encode(device_fingerprint)
    state.device_xml.write_text(_build_device_xml(device_type, fingerprint_b64), encoding="utf-8")

    return state

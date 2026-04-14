"""Device activation state for ADEPT fulfillment.

Stores the three files that together represent a registered ADE device:
    devicesalt       — 16-byte AES key used to wrap the pkcs12
    device.xml       — deviceType / serial / fingerprint / version
    activation.xml   — credentials (pkcs12), user/device UUIDs, certs,
                       privateLicenseKey, operator/license caches

All live under ~/.config/ade-dedrm/ (or $ADE_DEDRM_HOME if set). This
matches the *shape* of DeACSM's state but with our own location.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12 as crypto_pkcs12
from lxml import etree

ADEPT_NS = "http://ns.adobe.com/adept"
NSMAP = {"adept": ADEPT_NS}


def _adept(tag: str) -> str:
    return f"{{{ADEPT_NS}}}{tag}"


def state_dir() -> Path:
    override = os.environ.get("ADE_DEDRM_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ade-dedrm"


@dataclass
class DeviceState:
    root: Path

    @property
    def devicesalt(self) -> Path:
        return self.root / "devicesalt"

    @property
    def device_xml(self) -> Path:
        return self.root / "device.xml"

    @property
    def activation_xml(self) -> Path:
        return self.root / "activation.xml"

    def exists(self) -> bool:
        return (
            self.devicesalt.is_file()
            and self.device_xml.is_file()
            and self.activation_xml.is_file()
        )

    def ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def load_devicesalt(self) -> bytes:
        return self.devicesalt.read_bytes()

    def load_activation(self) -> etree._ElementTree:
        return etree.parse(str(self.activation_xml))

    def load_device(self) -> etree._ElementTree:
        return etree.parse(str(self.device_xml))


def decrypt_with_device_key(devicesalt: bytes, data: bytes) -> bytes:
    cipher = AES.new(devicesalt, AES.MODE_CBC, data[:16])
    plain = bytearray(cipher.decrypt(data[16:]))
    pad_len = plain[-1]
    return bytes(plain[:-pad_len])


def load_pkcs12_private_key_der(state: DeviceState) -> bytes:
    """Load the pkcs12 from activation.xml and return the private key as
    unencrypted PKCS#8 DER bytes.

    The pkcs12 itself is encrypted with base64(devicesalt) as its password —
    this is DeACSM/libgourou's convention, not a crypto best practice.
    """
    activation = state.load_activation()
    pkcs12_b64 = activation.find(f"./{_adept('credentials')}/{_adept('pkcs12')}")
    if pkcs12_b64 is None or not pkcs12_b64.text:
        raise RuntimeError("activation.xml is missing <adept:pkcs12>")

    pkcs12_bytes = base64.b64decode(pkcs12_b64.text)
    password = base64.b64encode(state.load_devicesalt())

    key, _cert, _extra = crypto_pkcs12.load_key_and_certificates(pkcs12_bytes, password)
    if key is None:
        raise RuntimeError("Failed to decrypt pkcs12 private key")

    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_pkcs12_cert_der(state: DeviceState) -> bytes:
    activation = state.load_activation()
    pkcs12_b64 = activation.find(f"./{_adept('credentials')}/{_adept('pkcs12')}")
    if pkcs12_b64 is None or not pkcs12_b64.text:
        raise RuntimeError("activation.xml is missing <adept:pkcs12>")

    pkcs12_bytes = base64.b64decode(pkcs12_b64.text)
    password = base64.b64encode(state.load_devicesalt())

    _key, cert, _extra = crypto_pkcs12.load_key_and_certificates(pkcs12_bytes, password)
    if cert is None:
        raise RuntimeError("Failed to decrypt pkcs12 certificate")

    return cert.public_bytes(serialization.Encoding.DER)


def save_activation(state: DeviceState, tree: etree._ElementTree) -> None:
    xml = etree.tostring(
        tree, encoding="utf-8", pretty_print=True, xml_declaration=False
    ).decode("utf-8")
    state.activation_xml.write_text('<?xml version="1.0"?>\n' + xml, encoding="utf-8")

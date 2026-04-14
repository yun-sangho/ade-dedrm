"""Tests for adobe_state: state dir resolution, pkcs12 roundtrip."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import CertificateBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID

from ade_dedrm.adobe_state import DeviceState, load_pkcs12_private_key_der, state_dir


def test_state_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADE_DEDRM_HOME", str(tmp_path / "custom"))
    assert state_dir() == tmp_path / "custom"


def test_state_dir_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ADE_DEDRM_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "xdg" / "ade-dedrm"


def _make_pkcs12(password: bytes) -> bytes:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    subject = issuer = Name([NameAttribute(NameOID.COMMON_NAME, "test")])
    import datetime

    cert = (
        CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(priv.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2030, 1, 1))
        .sign(priv, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"ade-dedrm-test",
        key=priv,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password),
    )


def test_pkcs12_roundtrip_through_state(tmp_path: Path) -> None:
    state = DeviceState(root=tmp_path / "state")
    state.ensure_dir()

    devicesalt = os.urandom(16)
    password = base64.b64encode(devicesalt)
    p12 = _make_pkcs12(password)

    state.devicesalt.write_bytes(devicesalt)

    activation_xml = f"""<?xml version="1.0"?>
<adept:activationInfo xmlns:adept="http://ns.adobe.com/adept">
  <adept:credentials>
    <adept:user>urn:uuid:00000000-0000-0000-0000-000000000000</adept:user>
    <adept:pkcs12>{base64.b64encode(p12).decode('ascii')}</adept:pkcs12>
  </adept:credentials>
</adept:activationInfo>
"""
    state.activation_xml.write_text(activation_xml)

    der = load_pkcs12_private_key_der(state)
    # Must be loadable as an RSA private key
    loaded = serialization.load_der_private_key(der, password=None)
    assert loaded.key_size == 1024

"""Unit tests for adobe_sign (Adobe's non-standard tree hash + textbook RSA)."""

from __future__ import annotations

import base64
import hashlib

from Crypto.PublicKey import RSA
from lxml import etree

from ade_dedrm.adobe_sign import (
    ADEPT_NS,
    _pkcs1v15_ff_pad,
    hash_node,
    sign_node,
    textbook_rsa_sign,
)


def test_pkcs1v15_ff_pad_layout() -> None:
    msg = b"hello"
    padded = _pkcs1v15_ff_pad(msg, 32)
    assert len(padded) == 32
    assert padded.startswith(b"\x00\x01")
    assert padded[-6:] == b"\x00hello"
    # Everything between prefix and 0x00 separator is 0xFF:
    assert padded[2:-6] == b"\xff" * 24


def test_textbook_rsa_sign_is_deterministic_and_verifiable() -> None:
    key = RSA.generate(1024)
    der = key.export_key("DER", pkcs=8)
    msg = hashlib.sha1(b"payload").digest()
    sig1 = textbook_rsa_sign(der, msg)
    sig2 = textbook_rsa_sign(der, msg)
    assert sig1 == sig2, "textbook RSA must be deterministic"

    # Verify by decrypting with the public key and checking the padding.
    c_int = int.from_bytes(sig1, "big")
    m_int = pow(c_int, key.e, key.n)
    recovered = m_int.to_bytes(len(sig1), "big")
    assert recovered.startswith(b"\x00\x01")
    assert recovered.endswith(msg)


def test_hash_node_skips_signature_and_hmac() -> None:
    xml = f"""<adept:req xmlns:adept="{ADEPT_NS}">
      <adept:foo>bar</adept:foo>
    </adept:req>"""
    node_without = etree.fromstring(xml)

    xml_with = f"""<adept:req xmlns:adept="{ADEPT_NS}">
      <adept:foo>bar</adept:foo>
      <adept:signature>IGNOREME</adept:signature>
      <adept:hmac>ALSOIGNORE</adept:hmac>
    </adept:req>"""
    node_with = etree.fromstring(xml_with)

    assert hash_node(node_without) == hash_node(node_with)


def test_hash_node_attribute_order_independent() -> None:
    a = etree.fromstring(f'<adept:x xmlns:adept="{ADEPT_NS}" b="2" a="1"/>')
    b = etree.fromstring(f'<adept:x xmlns:adept="{ADEPT_NS}" a="1" b="2"/>')
    assert hash_node(a) == hash_node(b)


def test_sign_node_end_to_end() -> None:
    key = RSA.generate(1024)
    der = key.export_key("DER", pkcs=8)
    node = etree.fromstring(
        f'<adept:req xmlns:adept="{ADEPT_NS}"><adept:body>hi</adept:body></adept:req>'
    )
    sig_b64 = sign_node(node, der)
    sig = base64.b64decode(sig_b64)
    # Expected size: RSA modulus in bytes
    assert len(sig) == (key.n.bit_length() + 7) // 8

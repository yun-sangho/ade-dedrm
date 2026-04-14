"""Adobe ADEPT request signing.

Ported from DeACSM/libadobe.py (hash_node_ctx, sign_node) and
DeACSM/customRSA.py. Adobe uses a non-standard tree hash followed by
textbook RSA with manual PKCS#1 v1.5 (0xFF) padding.

Original: Copyright (c) 2021-2022 Leseratte10, GPL v3.
See NOTICE for attribution.
"""

from __future__ import annotations

import base64

from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from lxml import etree

ASN_NS_TAG = 1
ASN_CHILD = 2
ASN_END_TAG = 3
ASN_TEXT = 4
ASN_ATTRIBUTE = 5

ADEPT_NS = "http://ns.adobe.com/adept"


def _append_raw(hash_ctx, data: bytes) -> None:
    hash_ctx.update(bytes(data))


def _append_tag(hash_ctx, tag: int) -> None:
    if tag > 5:
        return
    _append_raw(hash_ctx, bytes([tag]))


def _append_string(hash_ctx, string: str) -> None:
    data = string.encode("utf-8")
    length = len(data)
    _append_raw(hash_ctx, bytes([(length >> 8) & 0xFF, length & 0xFF]))
    _append_raw(hash_ctx, data)


def _hash_node(node: etree._Element, hash_ctx) -> None:
    qtag = etree.QName(node.tag)

    # Adobe excludes hmac and signature nodes (in the adept namespace) from
    # the hash so that a signature can be verified *including* the signature
    # element itself.
    if qtag.localname in ("hmac", "signature") and qtag.namespace == ADEPT_NS:
        return

    _append_tag(hash_ctx, ASN_NS_TAG)
    _append_string(hash_ctx, qtag.namespace or "")
    _append_string(hash_ctx, qtag.localname)

    # Attributes must be sorted. Adobe specifies bytewise UTF-8 sort; lxml
    # already stores attribute keys in insertion order, so we sort on the
    # serialized Clark name which is a reasonable approximation.
    for attr in sorted(node.keys()):
        _append_tag(hash_ctx, ASN_ATTRIBUTE)
        qattr = etree.QName(attr)
        _append_string(hash_ctx, qattr.namespace or "")
        _append_string(hash_ctx, qattr.localname)
        _append_string(hash_ctx, node.get(attr))

    _append_tag(hash_ctx, ASN_CHILD)

    if node.text is not None:
        text = node.text.strip()
        remaining = len(text)
        done = 0
        while remaining > 0:
            chunk_len = min(remaining, 0x7FFF)
            _append_tag(hash_ctx, ASN_TEXT)
            _append_string(hash_ctx, text[done : done + chunk_len])
            done += chunk_len
            remaining -= chunk_len

    for child in node:
        _hash_node(child, hash_ctx)

    _append_tag(hash_ctx, ASN_END_TAG)


def hash_node(node: etree._Element) -> bytes:
    """Return the SHA-1 digest of an XML node using Adobe's tree hash format."""
    ctx = SHA.new()
    _hash_node(node, ctx)
    return ctx.digest()


def _pkcs1v15_ff_pad(message: bytes, target_len: int) -> bytes:
    """Adobe's non-standard PKCS#1 v1.5 padding using only 0xFF bytes.

    Layout: 0x00 0x01 <0xFF * padding> 0x00 <message>
    """
    max_len = target_len - 11
    if len(message) > max_len:
        raise OverflowError(
            f"Message too long ({len(message)} > {max_len}) for {target_len}-byte key"
        )
    pad_len = target_len - len(message) - 3
    return b"\x00\x01" + b"\xff" * pad_len + b"\x00" + message


def textbook_rsa_sign(private_key_der: bytes, message: bytes) -> bytes:
    """Sign `message` with raw textbook RSA using Adobe's 0xFF padding.

    `private_key_der` must be importable by Crypto.PublicKey.RSA.importKey
    (PKCS#1 or PKCS#8 DER).
    """
    key = RSA.importKey(private_key_der)
    key_len = (key.n.bit_length() + 7) // 8
    padded = _pkcs1v15_ff_pad(message, key_len)
    m_int = int.from_bytes(padded, "big")
    if m_int >= key.n:
        raise ValueError("Padded message larger than RSA modulus")
    c_int = pow(m_int, key.d, key.n)
    return c_int.to_bytes(key_len, "big")


def sign_node(node: etree._Element, private_key_der: bytes) -> str:
    """Return a base64-encoded Adobe signature for the given node."""
    digest = hash_node(node)
    signature = textbook_rsa_sign(private_key_der, digest)
    return base64.b64encode(signature).decode("ascii")

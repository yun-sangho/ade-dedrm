"""Sanity tests for pdf.py low-level utilities."""

from __future__ import annotations

from io import BytesIO

import pytest

from ade_dedrm.pdf import (
    KWD,
    LIT,
    PDFParser,
    PSBaseParser,
    PSLiteral,
    PSLiteralTable,
    _nunpack,
    _unpad_pkcs7,
    decrypt_pdf,
)


def test_unpad_pkcs7_strips_padding() -> None:
    assert _unpad_pkcs7(b"hello\x03\x03\x03") == b"hello"


def test_nunpack_handles_variable_lengths() -> None:
    assert _nunpack(b"") == 0
    assert _nunpack(b"", default=42) == 42
    assert _nunpack(b"\x01") == 1
    assert _nunpack(b"\x01\x02") == 0x0102
    assert _nunpack(b"\x01\x02\x03") == 0x010203
    assert _nunpack(b"\x01\x02\x03\x04") == 0x01020304


def test_ps_symbol_interning_is_identity() -> None:
    a = LIT(b"Catalog")
    b = LIT(b"Catalog")
    assert a is b  # intern should return the same object
    c = KWD(b"obj")
    d = KWD(b"obj")
    assert c is d


def test_ps_base_parser_tokenizes_dict() -> None:
    src = b"<< /Type /Catalog /Size 42 >>"
    parser = PSBaseParser(BytesIO(src))
    tokens = []
    for _ in range(10):
        try:
            tokens.append(parser.nexttoken())
        except Exception:
            break
    kinds = [t[1] for t in tokens]
    # We should see: <<, /Type literal, /Catalog literal, /Size literal, 42 int, >>
    assert any(isinstance(k, PSLiteral) and k.name == "Type" for k in kinds)
    assert any(isinstance(k, PSLiteral) and k.name == "Catalog" for k in kinds)
    assert 42 in kinds


def test_decrypt_pdf_plain_pdf_returns_1(tmp_path) -> None:
    """A PDF without /Encrypt should be reported as not-DRM-protected."""
    # Minimal PDF 1.4 with a single Catalog; no encryption.
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        b"xref\n0 3\n"
        b"0000000000 65535 f \n"
        b"0000000010 00000 n \n"
        b"0000000050 00000 n \n"
        b"trailer<</Size 3/Root 1 0 R>>\n"
        b"startxref\n"
        b"100\n"
        b"%%EOF"
    )
    src = tmp_path / "plain.pdf"
    src.write_bytes(body)
    out = tmp_path / "out.pdf"
    assert decrypt_pdf(b"dummy-key", src, out) == 1
    # Output file must not be left behind when there was nothing to decrypt
    assert not out.exists()

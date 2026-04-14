"""Unit tests for adobe_pdf_patch helpers."""

from __future__ import annotations

import base64
import zlib
from io import BytesIO
from pathlib import Path

import pytest

from ade_dedrm.adobe_pdf_patch import (
    PDFPatchError,
    _BackwardReader,
    _cleanup_encrypt_element,
    _deflate_b64,
    _find_startxref,
    _parse_encrypt_ref,
    _trim_encrypt_string,
    _update_ebx,
    patch_drm_into_pdf,
)


def test_backward_reader_yields_lines_in_reverse() -> None:
    fp = BytesIO(b"first\nsecond\nthird\nlast")
    reader = _BackwardReader(fp)
    lines = list(reader.readlines())
    assert lines == ["last", "third", "second", "first"]


def test_trim_encrypt_string_matches_nested_brackets() -> None:
    raw = (
        "<</Size 10/Root 1 0 R/Info 2 0 R/Encrypt 3 0 R"
        "/ID[<abc> <def>]>>extra noise after >>"
    )
    trimmed = _trim_encrypt_string(raw)
    assert trimmed.endswith(">>")
    assert "extra noise" not in trimmed
    # bracket parity is zero inside the trimmed portion
    assert trimmed.count("<<") == trimmed.count(">>")


def test_cleanup_encrypt_element_normalizes_id_spacing() -> None:
    assert _cleanup_encrypt_element("ID[<abc><def>]") == "ID[<abc> <def>]"
    assert _cleanup_encrypt_element("Size  10") == "Size 10"


def test_deflate_b64_roundtrip() -> None:
    original = b"<?xml version='1.0'?><rights>hello</rights>"
    encoded = _deflate_b64(original)
    # Decode inverse: base64 -> raw deflate -> inflate with -15 wbits
    raw = base64.b64decode(encoded)
    inflated = zlib.decompress(raw, -15)
    assert inflated == original


def test_update_ebx_injects_license_entries() -> None:
    ebx_line = "3 0 obj<</Filter/EBX_HANDLER/Length 128/V 4>>"
    new = _update_ebx(ebx_line, "<rights/>", "urn:uuid:aaa")
    assert "/EBX_BOOKID(urn:uuid:aaa)" in new
    assert "/ADEPT_LICENSE(" in new
    assert new.endswith(">>")


def test_parse_encrypt_ref_extracts_objnum_and_gen() -> None:
    line = "<</Size 10/Root 1 0 R/Encrypt 3 0 R/ID[<a><b>]>>"
    obj, gen = _parse_encrypt_ref(line)
    assert obj == "3"
    assert gen == "0"


def test_find_startxref_reads_trailer(tmp_path: Path) -> None:
    body = b"%PDF-1.4\n...objects...\nxref\n0 1\nstartxref\n1234\n%%EOF"
    pdf = tmp_path / "mini.pdf"
    pdf.write_bytes(body)
    assert _find_startxref(pdf) == 1234


def test_patch_drm_into_pdf_appends_incremental_update(tmp_path: Path) -> None:
    # Synthesize a PDF body that our patcher can parse. We only need:
    #   - a trailer line with /Encrypt ref and /ID
    #   - an EBX_HANDLER line
    #   - startxref + EOF
    src = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog>>endobj\n"
        b"2 0 obj<</Type/Info>>endobj\n"
        b"3 0 obj<</Filter/EBX_HANDLER/V 4/Length 128>>endobj\n"
        b"xref\n"
        b"trailer\n"
        b"<</Size 4/Root 1 0 R/Info 2 0 R/Encrypt 3 0 R/ID[<abc><def>]>>\n"
        b"startxref\n"
        b"100\n"
        b"%%EOF"
    )
    inp = tmp_path / "in.pdf"
    out = tmp_path / "out.pdf"
    inp.write_bytes(src)

    patch_drm_into_pdf(inp, "<rights/>", out, "urn:uuid:bbb")

    patched = out.read_bytes()
    # Original content preserved up to the old EOF
    assert patched.startswith(src)
    # Incremental update tail carries the new encryption object
    tail = patched[len(src) :].decode("latin-1")
    assert "3 0 obj" in tail
    assert "/ADEPT_LICENSE(" in tail
    assert "/EBX_BOOKID(urn:uuid:bbb)" in tail
    assert tail.endswith("%%EOF")
    # A new xref with /Prev pointing at the old startxref (100)
    assert "/Prev 100" in tail
    assert "startxref" in tail


def test_patch_drm_into_pdf_raises_when_structure_missing(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot a real pdf\n")
    with pytest.raises(PDFPatchError):
        patch_drm_into_pdf(broken, "<rights/>", tmp_path / "out.pdf", "urn:uuid:ccc")

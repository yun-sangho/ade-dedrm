"""Smoke tests: imports resolve, CLI parses, round-trip decrypt works."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import pytest
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

from ade_dedrm import cli, epub, keyfetch  # noqa: F401
from ade_dedrm.cli import _default_output
from ade_dedrm.epub import decrypt_book, is_adept_epub


def test_cli_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Fulfill ACSM" in out
    assert "{init,decrypt}" in out


def test_decrypt_subcommand_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["decrypt", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--key" in out


@pytest.mark.parametrize(
    ("input_name", "ext", "expected"),
    [
        # Hyphen / underscore / space / dot separators.
        ("은하영웅전설_2_야망편-epub.acsm", ".epub", "은하영웅전설_2_야망편.epub"),
        ("은하영웅전설_2_야망편_epub.acsm", ".epub", "은하영웅전설_2_야망편.epub"),
        ("은하영웅전설_2_야망편 epub.acsm", ".epub", "은하영웅전설_2_야망편.epub"),
        ("은하영웅전설_2_야망편.epub.acsm", ".epub", "은하영웅전설_2_야망편.epub"),
        ("book-pdf.acsm", ".pdf", "book.pdf"),
        ("book_pdf.acsm", ".pdf", "book.pdf"),
        # Bracket wrappers.
        ("book(epub).acsm", ".epub", "book.epub"),
        ("book[epub].acsm", ".epub", "book.epub"),
        ("book{epub}.acsm", ".epub", "book.epub"),
        ("book (pdf).acsm", ".pdf", "book.pdf"),
        ("book_(epub).acsm", ".epub", "book.epub"),
        ("book-[pdf].acsm", ".pdf", "book.pdf"),
        # Case-insensitive.
        ("book-EPUB.acsm", ".epub", "book.epub"),
        ("book-PDF.acsm", ".pdf", "book.pdf"),
        ("book_(ePub).acsm", ".epub", "book.epub"),
        # Already-decrypted path with ``.nodrm`` default extension.
        ("book-epub.epub", ".nodrm.epub", "book.nodrm.epub"),
        ("book_(pdf).pdf", ".nodrm.pdf", "book.nodrm.pdf"),
        # Doubly-tagged stems get peeled iteratively.
        ("book-epub.pdf.acsm", ".pdf", "book.pdf"),
        # No format tag → stem is preserved untouched.
        ("plain.acsm", ".epub", "plain.epub"),
        # Tag-like substring without a separator → not stripped (avoid false positives).
        ("bookepub.acsm", ".epub", "bookepub.epub"),
        # Tag not at the end → not stripped.
        ("my-epub-book.acsm", ".epub", "my-epub-book.epub"),
        # Stripping would leave an empty stem → keep original.
        ("epub.acsm", ".epub", "epub.epub"),
        ("-epub.acsm", ".epub", "-epub.epub"),
        ("(pdf).acsm", ".pdf", "(pdf).pdf"),
    ],
)
def test_default_output_strips_format_tag(
    tmp_path: Path, input_name: str, ext: str, expected: str
) -> None:
    result = _default_output(tmp_path / input_name, ext)
    assert result == tmp_path / expected


def test_not_drm_returns_1(tmp_path: Path) -> None:
    plain = tmp_path / "plain.epub"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", "<container/>")
    out = tmp_path / "out.epub"
    assert decrypt_book(b"", plain, out) == 1
    assert not is_adept_epub(plain)


def _build_fake_adept_epub(tmp_path: Path, rsa_key: RSA.RsaKey) -> tuple[Path, bytes]:
    """Create a minimal DRM-protected ePub whose content is 'hello world'."""
    bookkey = b"\x11" * 16
    wrapped = PKCS1_v1_5.new(rsa_key.publickey()).encrypt(bookkey)
    encoded = base64.b64encode(wrapped).decode("ascii")
    # Adobe wraps to length 172 when the RSA modulus is 1024-bit, but at 2048
    # bits the wrap is 344 chars. Our decrypter whitelists 172 / 192, so force
    # a 1024-bit key.
    assert len(encoded) == 172, f"unexpected wrapped length {len(encoded)}"

    plaintext = b"hello world"
    # AES-CBC with a 16-byte IV prefix, PKCS#7 padding, zlib-compressed body.
    import zlib

    compressed = zlib.compress(plaintext)[2:-4]  # raw deflate like epub spec
    pad_len = 16 - (len(compressed) % 16)
    padded = compressed + bytes([pad_len]) * pad_len
    iv = b"\xaa" * 16
    aes = AES.new(bookkey, AES.MODE_CBC, b"\x00" * 16)
    ciphertext = aes.encrypt(iv + padded)

    rights_xml = f"""<?xml version="1.0"?>
<adept:rights xmlns:adept="http://ns.adobe.com/adept">
  <adept:licenseToken>
    <adept:encryptedKey>{encoded}</adept:encryptedKey>
  </adept:licenseToken>
</adept:rights>
""".encode()

    encryption_xml = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData>
      <enc:CipherReference URI="OEBPS/content.xhtml"/>
    </enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""

    src = tmp_path / "drm.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/rights.xml", rights_xml)
        zf.writestr("META-INF/encryption.xml", encryption_xml)
        zf.writestr("OEBPS/content.xhtml", ciphertext)
    return src, plaintext


def test_roundtrip_decrypt(tmp_path: Path) -> None:
    rsa_key = RSA.generate(1024)
    src, expected = _build_fake_adept_epub(tmp_path, rsa_key)
    out = tmp_path / "clean.epub"

    rc = decrypt_book(rsa_key.export_key("DER"), src, out)
    assert rc == 0

    with zipfile.ZipFile(out, "r") as zf:
        assert zf.read("OEBPS/content.xhtml") == expected
        assert zf.read("mimetype") == b"application/epub+zip"

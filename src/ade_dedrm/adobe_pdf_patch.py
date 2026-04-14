"""Patch an ADEPT license into a fulfilled PDF.

When Google/Adobe ACS4 returns a PDF, the file is already encrypted with
an ADEPT security handler but the /ADEPT_LICENSE field inside the
encryption object is empty. DeACSM's approach (which we mirror here) is
to write an incremental update at the end of the file: a new version of
the encryption object that carries the license blob, plus xref/trailer
entries pointing to that new object.

After patching, the PDF is structurally identical to one produced by
real Adobe Digital Editions, and can be decrypted by ineptpdf (our
`pdf.decrypt_pdf`).

Ported from DeACSM/libpdf.py. GPL v3.
"""

from __future__ import annotations

import base64
import os
import zlib
from pathlib import Path


class PDFPatchError(Exception):
    pass


class _BackwardReader:
    """Yield lines from a file, starting at EOF and moving backwards.

    Lines are returned as latin-1 decoded strings without the trailing
    newline. The underlying file must be opened in binary mode.
    """

    BLK_SIZE = 4096

    def __init__(self, fp) -> None:
        self._fp = fp

    def readlines(self):
        self._fp.seek(0, os.SEEK_END)
        buffer = bytearray()

        while True:
            nl = buffer.rfind(b"\x0a")
            current = self._fp.tell()

            if nl != -1:
                line = bytes(buffer[nl + 1 :])
                del buffer[nl:]
                yield line.decode("latin-1")
                continue

            if current == 0:
                if buffer:
                    yield bytes(buffer).decode("latin-1")
                return

            to_read = min(self.BLK_SIZE, current)
            self._fp.seek(current - to_read, 0)
            buffer[:0] = self._fp.read(to_read)
            self._fp.seek(current - to_read, 0)


def _trim_encrypt_string(encrypt: str) -> str:
    """Slice the encryption dictionary so it stops at its matching '>>'."""
    depth = 0
    i = 0
    n = len(encrypt)
    while i < n - 1:
        if encrypt[i] == "<" and encrypt[i + 1] == "<":
            depth += 1
        if encrypt[i] == ">" and encrypt[i + 1] == ">":
            depth -= 1
            if depth == 0:
                return encrypt[: i + 2]
        i += 1
    return encrypt


def _cleanup_encrypt_element(element: str) -> str:
    if element.startswith("ID[<"):
        element = element.replace("><", "> <")
    element = " ".join(element.split())
    element = element.replace("[ ", "[").replace("] ", "]")
    return element


def _deflate_b64(data: bytes) -> bytes:
    compressed = zlib.compress(data)
    # Strip 2-byte zlib header and 4-byte Adler32 trailer to match Adobe's
    # "raw deflate within an ADEPT_LICENSE" format.
    return base64.b64encode(compressed[2:-4])


def _update_ebx(ebx: str, rights_xml: str, resource_id: str) -> str:
    b64 = _deflate_b64(rights_xml.encode("utf-8")).decode("ascii")
    # ebx ends with ">>"; we splice the new entries before the terminator.
    return ebx[:-2] + f"/EBX_BOOKID({resource_id})/ADEPT_LICENSE({b64})>>"


def _find_line_containing(path: Path, needles: tuple[str, ...]) -> str | None:
    with path.open("rb") as fp:
        reader = _BackwardReader(fp)
        for line in reader.readlines():
            if all(n in line for n in needles):
                return line
    return None


def _find_encrypt_line(path: Path) -> str:
    line = _find_line_containing(path, ("R/Encrypt", "R/ID"))
    if line is None:
        raise PDFPatchError("Could not find the encryption dictionary in the PDF")
    return line


def _find_ebx_line(path: Path) -> str:
    line = _find_line_containing(path, ("/EBX_HANDLER/",))
    if line is None:
        raise PDFPatchError("Could not find the EBX_HANDLER object in the PDF")
    return line


def _find_startxref(path: Path) -> int:
    with path.open("rb") as fp:
        reader = _BackwardReader(fp)
        prev = ""
        for idx, line in enumerate(reader.readlines()):
            if line == "startxref":
                try:
                    return int(prev)
                except ValueError as exc:
                    raise PDFPatchError(
                        f"startxref line found but next line is not an integer: {prev!r}"
                    ) from exc
            prev = line
            if idx > 20:
                raise PDFPatchError("Could not find startxref near EOF")
    raise PDFPatchError("Reached start of file without finding startxref")


def _parse_encrypt_ref(encrypt_line: str) -> tuple[str, str]:
    """Return the (objnum, gen) pair that /Encrypt references."""
    parts = encrypt_line.split(" ")
    state = 0
    obj_num = gen = None
    for element in parts:
        if element == "R/Encrypt":
            state = 2
            continue
        if state == 2:
            obj_num = element
            state = 1
            continue
        if state == 1:
            gen = element
            state = 0
            continue
    if obj_num is None or gen is None:
        raise PDFPatchError(f"Could not parse /Encrypt ref from: {encrypt_line!r}")
    return obj_num, gen


def patch_drm_into_pdf(
    in_path: Path, rights_xml: str, out_path: Path, resource_id: str
) -> None:
    """Write an incrementally-updated PDF with the ADEPT license embedded."""
    in_path = Path(in_path)
    out_path = Path(out_path)

    startxref = _find_startxref(in_path)
    encrypt_line = _find_encrypt_line(in_path)
    ebx_line = _find_ebx_line(in_path)

    obj_num, gen = _parse_encrypt_ref(encrypt_line)
    trimmed_encrypt = _trim_encrypt_string(encrypt_line)
    new_ebx = _update_ebx(ebx_line, rights_xml, resource_id)

    file_size = in_path.stat().st_size
    filesize_pad = str(file_size).zfill(10)

    # Incremental update:
    #   <objnum> <gen> obj
    #   <new EBX dict with ADEPT_LICENSE>
    #   endobj
    #   xref
    #   <objnum> 1
    #   <filesize padded> 00000 n\r\n
    #   trailer
    #   <original trailer dict with Prev pointing to old startxref>
    #   startxref
    #   <offset of the new xref>
    #   %%EOF
    parts = [
        "\r",
        f"{obj_num} {gen} obj\r",
        new_ebx,
        "\rendobj",
    ]
    body = "".join(parts)
    new_xref_offset = file_size + len(body)

    parts.append("\rxref\r")
    parts.append(f"{obj_num} {int(gen) + 1}\r")
    parts.append(f"{filesize_pad} 00000 n\r\n")
    parts.append("trailer\r")

    fragments = trimmed_encrypt.split("/")
    did_prev = False
    out_fragments: list[str] = []
    for elem in fragments:
        if elem.startswith("Prev"):
            did_prev = True
            out_fragments.append(f"Prev {startxref}")
        else:
            out_fragments.append(_cleanup_encrypt_element(elem))
    trailer_joined = "/".join(out_fragments)

    if not did_prev:
        # Inject /Prev right before the closing ">>".
        if not trailer_joined.endswith(">>"):
            raise PDFPatchError(
                f"Unexpected trailer dict termination: {trailer_joined[-4:]!r}"
            )
        trailer_joined = trailer_joined[:-2] + f"/Prev {startxref}>>"

    parts.append(trailer_joined)
    parts.append(f"\rstartxref\r{new_xref_offset}\r%%EOF")

    appended = "".join(parts).encode("latin-1")

    with in_path.open("rb") as src, out_path.open("wb") as dst:
        dst.write(src.read())
        dst.write(appended)

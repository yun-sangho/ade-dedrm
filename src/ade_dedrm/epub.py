"""Adobe Adept (ADE) ePub DRM decryption.

Ported from DeDRM_tools/DeDRM_plugin/ineptepub.py
Original copyright (C) 2009-2022 i♥cabbages, Apprentice Harper et al.
Licensed under GPL v3. See NOTICE for attribution.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
import zlib
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from lxml import etree

NSMAP = {
    "adept": "http://ns.adobe.com/adept",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}
META_NAMES = ("mimetype", "META-INF/rights.xml")


class ADEPTError(Exception):
    pass


def _unpad_pkcs7(data: bytes) -> bytes:
    return data[: -data[-1]]


class _Decryptor:
    def __init__(self, bookkey: bytes, encryption_xml: bytes) -> None:
        enc = lambda tag: "{%s}%s" % (NSMAP["enc"], tag)
        self._aes = AES.new(bookkey, AES.MODE_CBC, b"\x00" * 16)
        self._encryption = etree.fromstring(encryption_xml)
        self._encrypted: set[bytes] = set()
        self._encrypted_no_decomp: set[bytes] = set()
        self._has_remaining_xml = False

        to_remove = set()
        expr = f"./{enc('EncryptedData')}/{enc('CipherData')}/{enc('CipherReference')}"
        for elem in self._encryption.findall(expr):
            path = elem.get("URI")
            if path is None:
                continue
            algo = (
                elem.getparent()
                .getparent()
                .find(f"./{enc('EncryptionMethod')}")
                .get("Algorithm")
            )
            path_b = path.encode("utf-8")
            if algo == "http://www.w3.org/2001/04/xmlenc#aes128-cbc":
                self._encrypted.add(path_b)
                to_remove.add(elem.getparent().getparent())
            elif algo == "http://ns.adobe.com/adept/xmlenc#aes128-cbc-uncompressed":
                self._encrypted_no_decomp.add(path_b)
                to_remove.add(elem.getparent().getparent())
            else:
                self._has_remaining_xml = True

        for elem in to_remove:
            elem.getparent().remove(elem)

    def has_remaining(self) -> bool:
        return self._has_remaining_xml

    def get_xml(self) -> bytes:
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(
            self._encryption, encoding="utf-8", pretty_print=True, xml_declaration=False
        )

    @staticmethod
    def _decompress(data: bytes) -> bytes:
        dc = zlib.decompressobj(-15)
        try:
            out = dc.decompress(data)
            ex = dc.decompress(b"Z") + dc.flush()
            if ex:
                out += ex
        except zlib.error:
            return data
        return out

    def decrypt(self, path: str, data: bytes) -> bytes:
        path_b = path.encode("utf-8")
        if path_b in self._encrypted or path_b in self._encrypted_no_decomp:
            data = self._aes.decrypt(data)[16:]
            data = data[: -data[-1]]
            if path_b not in self._encrypted_no_decomp:
                data = self._decompress(data)
        return data


def _remove_hardening(rights: etree._Element, keytype: str, keydata: bytes) -> bytes:
    adept = lambda tag: "{%s}%s" % (NSMAP["adept"], tag)
    get = lambda name: "".join(rights.findtext(f".//{adept(name)}"))

    resource = UUID(get("resource"))
    device = UUID(get("device"))
    fulfillment = UUID(get("fulfillment")[:36])
    kekiv = UUID(int=resource.int ^ device.int ^ fulfillment.int).bytes

    rem = int(keytype, 10) % 16
    h = hashlib.sha256(keytype.encode("ascii")).digest()
    kek = h[2 * rem : 16 + rem] + h[rem : 2 * rem]

    return _unpad_pkcs7(AES.new(kek, AES.MODE_CBC, kekiv).decrypt(keydata))


def is_adept_epub(inpath: Path) -> bool:
    """True if the file looks like an Adobe Adept-protected ePub."""
    try:
        with ZipFile(inpath, "r") as zf:
            names = set(zf.namelist())
            if "META-INF/rights.xml" not in names or "META-INF/encryption.xml" not in names:
                return False
            rights = etree.fromstring(zf.read("META-INF/rights.xml"))
    except (zipfile.BadZipFile, etree.XMLSyntaxError):
        return False
    adept = lambda tag: "{%s}%s" % (NSMAP["adept"], tag)
    bookkey = rights.findtext(f".//{adept('encryptedKey')}") or ""
    return len(bookkey) in (172, 192)


def decrypt_book(userkey: bytes, inpath: Path, outpath: Path) -> int:
    """Decrypt an Adobe Adept ePub.

    Return codes:
        0 — success
        1 — not DRM-protected / not an Adept ePub
        2 — wrong key / decryption failure
    """
    with ZipFile(inpath, "r") as inf:
        namelist = inf.namelist()
        if "META-INF/rights.xml" not in namelist or "META-INF/encryption.xml" not in namelist:
            return 1
        for name in META_NAMES:
            if name in namelist:
                namelist.remove(name)

        rights = etree.fromstring(inf.read("META-INF/rights.xml"))
        adept = lambda tag: "{%s}%s" % (NSMAP["adept"], tag)
        bookkey_elem = rights.find(f".//{adept('encryptedKey')}")
        if bookkey_elem is None or bookkey_elem.text is None:
            return 1
        bookkey_b64 = bookkey_elem.text
        keytype = bookkey_elem.attrib.get("keyType", "0")

        if len(bookkey_b64) not in (172, 192):
            # 64 = PassHash (B&N), unsupported. Anything else = not an Adept book.
            return 1

        rsakey = RSA.importKey(userkey)
        bookkey = base64.b64decode(bookkey_b64)
        if int(keytype, 10) > 2:
            bookkey = _remove_hardening(rights, keytype, bookkey)
        try:
            bookkey = PKCS1_v1_5.new(rsakey).decrypt(bookkey, None)
        except ValueError:
            bookkey = None
        if not bookkey:
            return 2

        decryptor = _Decryptor(bookkey, inf.read("META-INF/encryption.xml"))

        with ZipFile(outpath, "w", compression=ZIP_DEFLATED, allowZip64=False) as outf:
            for path in ["mimetype", *namelist]:
                data = inf.read(path)
                zi = ZipInfo(path)
                zi.compress_type = ZIP_STORED if path == "mimetype" else ZIP_DEFLATED

                if path == "META-INF/encryption.xml":
                    if not decryptor.has_remaining():
                        continue
                    data = decryptor.get_xml()

                try:
                    oldzi = inf.getinfo(path)
                    zi.date_time = oldzi.date_time
                    zi.comment = oldzi.comment
                    zi.extra = oldzi.extra
                    zi.internal_attr = oldzi.internal_attr
                    zi.external_attr = oldzi.external_attr
                    zi.create_system = oldzi.create_system
                    if any(ord(c) >= 128 for c in path):
                        zi.flag_bits |= 0x800
                except KeyError:
                    pass

                if path == "META-INF/encryption.xml":
                    outf.writestr(zi, data)
                else:
                    outf.writestr(zi, decryptor.decrypt(path, data))

    return 0

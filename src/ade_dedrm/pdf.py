"""Decrypt Adobe ADEPT-protected PDF files.

Ported from DeDRM_tools/DeDRM_plugin/ineptpdf.py (v10.0.4).
Original Copyright (C) 2009-2022 i♥cabbages, Apprentice Harper, noDRM et al.
GPL v3. See NOTICE for attribution.

The port drops:
  * Python 2 compatibility branches
  * Calibre/Tkinter GUI shims and standalone CLI entry points
  * Standard (password-based) encryption paths including V=5, R=5/6, AES-256
  * Adobe.APS (German public library) and B&N ignoble (PassHash) schemes
  * `adeptGetUserUUID`, `getPDFencryptionType` utility helpers

Only the Adobe ADEPT EBX_HANDLER branch (V<=4, RC4) is retained — that's
the one that matches fulfillment output from ACS4 operators. The entry
point is `decrypt_pdf(userkey_der, in_path, out_path)`.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import itertools
import re
import struct
import zlib
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID

import xml.etree.ElementTree as etree
from Crypto.Cipher import AES, ARC4, PKCS1_v1_5
from Crypto.PublicKey import RSA


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ADEPTError(Exception):
    pass


class ADEPTNewVersionError(Exception):
    pass


class PSException(Exception):
    pass


class PSEOF(PSException):
    pass


class PSSyntaxError(PSException):
    pass


class PSTypeError(PSException):
    pass


class PSValueError(PSException):
    pass


class PDFException(PSException):
    pass


class PDFTypeError(PDFException):
    pass


class PDFValueError(PDFException):
    pass


class PDFNotImplementedError(PSException):
    pass


class PDFSyntaxError(PDFException):
    pass


class PDFNoValidXRef(PDFSyntaxError):
    pass


class PDFEncryptionError(PDFException):
    pass


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #


STRICT = 0
GEN_XREF_STM = 1  # 0=never, 1=only if present in input, 2=always
gen_xref_stm = False  # document-level flag, set by PDFSerializer


def _unpad_pkcs7(data: bytes) -> bytes:
    return data[: -data[-1]]


def _sha256(message: bytes) -> bytes:
    return hashlib.sha256(message).digest()


def _choplist(n: int, seq):
    chunk = []
    for x in seq:
        chunk.append(x)
        if len(chunk) == n:
            yield tuple(chunk)
            chunk = []


def _nunpack(s: bytes, default: int = 0) -> int:
    """Unpack up to 4 big-endian bytes as an unsigned integer."""
    length = len(s)
    if length == 0:
        return default
    if length == 1:
        return s[0] if isinstance(s[0], int) else ord(s)
    if length == 2:
        return struct.unpack(">H", s)[0]
    if length == 3:
        return struct.unpack(">L", b"\x00" + s)[0]
    if length == 4:
        return struct.unpack(">L", s)[0]
    raise TypeError(f"invalid length: {length}")


# --------------------------------------------------------------------------- #
# PS symbols
# --------------------------------------------------------------------------- #


class PSObject:
    pass


class PSLiteral(PSObject):
    """A PostScript literal (e.g. "/Name"). Use PSLiteralTable.intern()."""

    def __init__(self, name: bytes) -> None:
        self.name = name.decode("utf-8")

    def __repr__(self) -> str:
        parts = []
        for ch in self.name:
            if not ch.isalnum():
                parts.append(f"#{ord(ch):02x}")
            else:
                parts.append(ch)
        return "/" + "".join(parts)


class PSKeyword(PSObject):
    """A PostScript keyword (e.g. "showpage"). Use PSKeywordTable.intern()."""

    def __init__(self, name: bytes) -> None:
        self.name = name.decode("utf-8")

    def __repr__(self) -> str:
        return self.name


class _PSSymbolTable:
    def __init__(self, klass) -> None:
        self.dic: dict[bytes, object] = {}
        self.klass = klass

    def intern(self, name: bytes):
        if name in self.dic:
            return self.dic[name]
        obj = self.klass(name)
        self.dic[name] = obj
        return obj


PSLiteralTable = _PSSymbolTable(PSLiteral)
PSKeywordTable = _PSSymbolTable(PSKeyword)
LIT = PSLiteralTable.intern
KWD = PSKeywordTable.intern
KEYWORD_BRACE_BEGIN = KWD(b"{")
KEYWORD_BRACE_END = KWD(b"}")
KEYWORD_ARRAY_BEGIN = KWD(b"[")
KEYWORD_ARRAY_END = KWD(b"]")
KEYWORD_DICT_BEGIN = KWD(b"<<")
KEYWORD_DICT_END = KWD(b">>")


def literal_name(x) -> str:
    if not isinstance(x, PSLiteral):
        if STRICT:
            raise PSTypeError(f"Literal required: {x!r}")
        return str(x)
    return x.name


def keyword_name(x) -> str:
    if not isinstance(x, PSKeyword):
        if STRICT:
            raise PSTypeError(f"Keyword required: {x!r}")
        return str(x)
    return x.name


# --------------------------------------------------------------------------- #
# PS base parser (tokenizer)
# --------------------------------------------------------------------------- #


EOL = re.compile(rb"[\r\n]")
SPC = re.compile(rb"\s")
NONSPC = re.compile(rb"\S")
HEX = re.compile(rb"[0-9a-fA-F]")
END_LITERAL = re.compile(rb"[#/%\[\]()<>{}\s]")
END_HEX_STRING = re.compile(rb"[^\s0-9a-fA-F]")
HEX_PAIR = re.compile(rb"[0-9a-fA-F]{2}|.")
END_NUMBER = re.compile(rb"[^0-9]")
END_KEYWORD = re.compile(rb"[#/%\[\]()<>{}\s]")
END_STRING = re.compile(rb"[()\\]")
OCT_STRING = re.compile(rb"[0-7]")
ESC_STRING = {
    b"b": 8, b"t": 9, b"n": 10, b"f": 12, b"r": 13,
    b"(": 40, b")": 41, b"\\": 92,
}


class EmptyArrayValue:
    def __str__(self) -> str:
        return "<>"


class PSBaseParser:
    """Most basic PostScript parser that performs only basic tokenization."""

    BUFSIZ = 4096

    def __init__(self, fp) -> None:
        self.fp = fp
        self.seek(0)

    def __repr__(self) -> str:
        return f"<PSBaseParser: {self.fp!r}, bufpos={self.bufpos}>"

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.flush()

    def tell(self) -> int:
        return self.bufpos + self.charpos

    def seek(self, pos: int) -> None:
        self.fp.seek(pos)
        self.bufpos = pos
        self.buf = b""
        self.charpos = 0
        self.parse1 = self.parse_main
        self.tokens: list[tuple[int, object]] = []

    def fillbuf(self) -> None:
        if self.charpos < len(self.buf):
            return
        self.bufpos = self.fp.tell()
        self.buf = self.fp.read(self.BUFSIZ)
        if not self.buf:
            raise PSEOF("Unexpected EOF")
        self.charpos = 0

    def parse_main(self, s: bytes, i: int):
        m = NONSPC.search(s, i)
        if not m:
            return (self.parse_main, len(s))
        j = m.start(0)
        c = bytes([s[j]])
        self.tokenstart = self.bufpos + j
        if c == b"%":
            self.token = c
            return (self.parse_comment, j + 1)
        if c == b"/":
            self.token = b""
            return (self.parse_literal, j + 1)
        if c in b"-+" or c.isdigit():
            self.token = c
            return (self.parse_number, j + 1)
        if c == b".":
            self.token = c
            return (self.parse_decimal, j + 1)
        if c.isalpha():
            self.token = c
            return (self.parse_keyword, j + 1)
        if c == b"(":
            self.token = b""
            self.paren = 1
            return (self.parse_string, j + 1)
        if c == b"<":
            self.token = b""
            return (self.parse_wopen, j + 1)
        if c == b">":
            self.token = b""
            return (self.parse_wclose, j + 1)
        self.add_token(KWD(c))
        return (self.parse_main, j + 1)

    def add_token(self, obj) -> None:
        self.tokens.append((self.tokenstart, obj))

    def parse_comment(self, s, i):
        m = EOL.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_comment, len(s))
        j = m.start(0)
        self.token += s[i:j]
        return (self.parse_main, j)

    def parse_literal(self, s, i):
        m = END_LITERAL.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_literal, len(s))
        j = m.start(0)
        self.token += s[i:j]
        c = bytes([s[j]])
        if c == b"#":
            self.hex = b""
            return (self.parse_literal_hex, j + 1)
        self.add_token(LIT(self.token))
        return (self.parse_main, j)

    def parse_literal_hex(self, s, i):
        c = bytes([s[i]])
        if HEX.match(c) and len(self.hex) < 2:
            self.hex += c
            return (self.parse_literal_hex, i + 1)
        if self.hex:
            self.token += bytes([int(self.hex, 16)])
        return (self.parse_literal, i)

    def parse_number(self, s, i):
        m = END_NUMBER.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_number, len(s))
        j = m.start(0)
        self.token += s[i:j]
        c = bytes([s[j]])
        if c == b".":
            self.token += c
            return (self.parse_decimal, j + 1)
        try:
            self.add_token(int(self.token))
        except ValueError:
            pass
        return (self.parse_main, j)

    def parse_decimal(self, s, i):
        m = END_NUMBER.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_decimal, len(s))
        j = m.start(0)
        self.token += s[i:j]
        self.add_token(Decimal(self.token.decode("utf-8")))
        return (self.parse_main, j)

    def parse_keyword(self, s, i):
        m = END_KEYWORD.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_keyword, len(s))
        j = m.start(0)
        self.token += s[i:j]
        if self.token == b"true":
            token = True
        elif self.token == b"false":
            token = False
        else:
            token = KWD(self.token)
        self.add_token(token)
        return (self.parse_main, j)

    def parse_string(self, s, i):
        m = END_STRING.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_string, len(s))
        j = m.start(0)
        self.token += s[i:j]
        c = bytes([s[j]])
        if c == b"\\":
            self.oct = b""
            return (self.parse_string_1, j + 1)
        if c == b"(":
            self.paren += 1
            self.token += c
            return (self.parse_string, j + 1)
        if c == b")":
            self.paren -= 1
            if self.paren:
                self.token += c
                return (self.parse_string, j + 1)
        self.add_token(self.token)
        return (self.parse_main, j + 1)

    def parse_string_1(self, s, i):
        c = bytes([s[i]])
        if OCT_STRING.match(c) and len(self.oct) < 3:
            self.oct += c
            return (self.parse_string_1, i + 1)
        if self.oct:
            self.token += bytes([int(self.oct, 8)])
            return (self.parse_string, i)
        if c in ESC_STRING:
            self.token += bytes([ESC_STRING[c]])
        return (self.parse_string, i + 1)

    def parse_wopen(self, s, i):
        c = bytes([s[i]])
        if c.isspace() or HEX.match(c):
            return (self.parse_hexstring, i)
        if c == b"<":
            self.add_token(KEYWORD_DICT_BEGIN)
            i += 1
        if c == b">":
            self.add_token(EmptyArrayValue())
            i += 1
        return (self.parse_main, i)

    def parse_wclose(self, s, i):
        c = bytes([s[i]])
        if c == b">":
            self.add_token(KEYWORD_DICT_END)
            i += 1
        return (self.parse_main, i)

    def parse_hexstring(self, s, i):
        m = END_HEX_STRING.search(s, i)
        if not m:
            self.token += s[i:]
            return (self.parse_hexstring, len(s))
        j = m.start(0)
        self.token += s[i:j]
        token = HEX_PAIR.sub(
            lambda mm: bytes([int(mm.group(0), 16)]),
            SPC.sub(b"", self.token),
        )
        self.add_token(token)
        return (self.parse_main, j)

    def nexttoken(self):
        while not self.tokens:
            self.fillbuf()
            (self.parse1, self.charpos) = self.parse1(self.buf, self.charpos)
        return self.tokens.pop(0)

    def nextline(self):
        """Fetch the next line ending in \\r or \\n."""
        linebuf = b""
        linepos = self.bufpos + self.charpos
        eol = False
        while True:
            self.fillbuf()
            if eol:
                c = bytes([self.buf[self.charpos]])
                if c == b"\n":
                    linebuf += c
                    self.charpos += 1
                break
            m = EOL.search(self.buf, self.charpos)
            if m:
                linebuf += self.buf[self.charpos : m.end(0)]
                self.charpos = m.end(0)
                if bytes([linebuf[-1]]) == b"\r":
                    eol = True
                else:
                    break
            else:
                linebuf += self.buf[self.charpos :]
                self.charpos = len(self.buf)
        return (linepos, linebuf)

    def revreadlines(self):
        """Yield lines backwards from EOF (used to locate trailers)."""
        self.fp.seek(0, 2)
        pos = self.fp.tell()
        buf = b""
        while pos > 0:
            prevpos = pos
            pos = max(0, pos - self.BUFSIZ)
            self.fp.seek(pos)
            s = self.fp.read(prevpos - pos)
            if not s:
                break
            while True:
                n = max(s.rfind(b"\r"), s.rfind(b"\n"))
                if n == -1:
                    buf = s + buf
                    break
                yield s[n:] + buf
                s = s[:n]
                buf = b""


# --------------------------------------------------------------------------- #
# PS stack parser
# --------------------------------------------------------------------------- #


class PSStackParser(PSBaseParser):
    def __init__(self, fp) -> None:
        super().__init__(fp)
        self.reset()

    def reset(self) -> None:
        self.context: list = []
        self.curtype = None
        self.curstack: list = []
        self.results: list = []

    def seek(self, pos: int) -> None:
        super().seek(pos)
        self.reset()

    def push(self, *objs) -> None:
        self.curstack.extend(objs)

    def pop(self, n: int):
        objs = self.curstack[-n:]
        self.curstack[-n:] = []
        return objs

    def popall(self):
        objs = self.curstack
        self.curstack = []
        return objs

    def add_results(self, *objs) -> None:
        self.results.extend(objs)

    def start_type(self, pos, kind) -> None:
        self.context.append((pos, self.curtype, self.curstack))
        self.curtype = kind
        self.curstack = []

    def end_type(self, kind):
        if self.curtype != kind:
            raise PSTypeError(f"Type mismatch: {self.curtype!r} != {kind!r}")
        objs = [obj for (_, obj) in self.curstack]
        (pos, self.curtype, self.curstack) = self.context.pop()
        return (pos, objs)

    def do_keyword(self, pos, token) -> None:
        return

    def nextobject(self, direct: bool = False):
        while not self.results:
            (pos, token) = self.nexttoken()
            if isinstance(
                token,
                (int, Decimal, bool, bytearray, bytes, str, PSLiteral),
            ):
                self.push((pos, token))
            elif token == KEYWORD_ARRAY_BEGIN:
                self.start_type(pos, "a")
            elif token == KEYWORD_ARRAY_END:
                try:
                    self.push(self.end_type("a"))
                except PSTypeError:
                    if STRICT:
                        raise
            elif token == KEYWORD_DICT_BEGIN:
                self.start_type(pos, "d")
            elif token == KEYWORD_DICT_END:
                try:
                    (pos, objs) = self.end_type("d")
                    if len(objs) % 2 != 0:
                        objs.append("")
                    d = {literal_name(k): v for (k, v) in _choplist(2, objs)}
                    self.push((pos, d))
                except PSTypeError:
                    if STRICT:
                        raise
            else:
                self.do_keyword(pos, token)
            if self.context:
                continue
            if direct:
                return self.pop(1)[0]
            self.flush()
        return self.results.pop(0)


# --------------------------------------------------------------------------- #
# PDF objects
# --------------------------------------------------------------------------- #


LITERAL_CRYPT = LIT(b"Crypt")
LITERALS_FLATE_DECODE = (LIT(b"FlateDecode"), LIT(b"Fl"))
LITERALS_LZW_DECODE = (LIT(b"LZWDecode"), LIT(b"LZW"))
LITERALS_ASCII85_DECODE = (LIT(b"ASCII85Decode"), LIT(b"A85"))
LITERAL_OBJSTM = LIT(b"ObjStm")
LITERAL_XREF = LIT(b"XRef")
LITERAL_CATALOG = LIT(b"Catalog")


class PDFObject(PSObject):
    pass


class PDFObjRef(PDFObject):
    def __init__(self, doc, objid: int, genno: int) -> None:
        if objid == 0 and STRICT:
            raise PDFValueError("PDF object id cannot be 0.")
        self.doc = doc
        self.objid = objid
        self.genno = genno

    def __repr__(self) -> str:
        return f"<PDFObjRef:{self.objid} {self.genno}>"

    def resolve(self):
        return self.doc.getobj(self.objid)


def resolve1(x):
    while isinstance(x, PDFObjRef):
        x = x.resolve()
    return x


def resolve_all(x):
    while isinstance(x, PDFObjRef):
        x = x.resolve()
    if isinstance(x, list):
        x = [resolve_all(v) for v in x]
    elif isinstance(x, dict):
        for k, v in list(x.items()):
            x[k] = resolve_all(v)
    return x


def decipher_all(decipher, objid, genno, x):
    if isinstance(x, (bytes, bytearray, str)):
        return decipher(objid, genno, x)
    decf = lambda v: decipher_all(decipher, objid, genno, v)
    if isinstance(x, list):
        return [decf(v) for v in x]
    if isinstance(x, dict):
        return {k: decf(v) for k, v in x.items()}
    return x


def int_value(x) -> int:
    x = resolve1(x)
    if not isinstance(x, int):
        if STRICT:
            raise PDFTypeError(f"Integer required: {x!r}")
        return 0
    return x


def num_value(x):
    x = resolve1(x)
    if not isinstance(x, (int, Decimal)):
        if STRICT:
            raise PDFTypeError(f"Int or Decimal required: {x!r}")
        return 0
    return x


def str_value(x):
    x = resolve1(x)
    if not isinstance(x, (bytes, bytearray, str)):
        if STRICT:
            raise PDFTypeError(f"String required: {x!r}")
        return b""
    return x


def list_value(x):
    x = resolve1(x)
    if not isinstance(x, (list, tuple)):
        if STRICT:
            raise PDFTypeError(f"List required: {x!r}")
        return []
    return x


def dict_value(x):
    x = resolve1(x)
    if not isinstance(x, dict):
        if STRICT:
            raise PDFTypeError(f"Dict required: {x!r}")
        return {}
    return x


def stream_value(x):
    x = resolve1(x)
    if not isinstance(x, PDFStream):
        if STRICT:
            raise PDFTypeError(f"PDFStream required: {x!r}")
        return PDFStream({}, b"")
    return x


def ascii85decode(data: bytes) -> bytes:
    n = 0
    b = 0
    out = b""
    for ch in data:
        c = bytes([ch])
        if b"!" <= c <= b"u":
            n += 1
            b = b * 85 + (ch - 33)
            if n == 5:
                out += struct.pack(">L", b)
                n = b = 0
        elif c == b"z":
            assert n == 0
            out += b"\0\0\0\0"
        elif c == b"~":
            if n:
                for _ in range(5 - n):
                    b = b * 85 + 84
                out += struct.pack(">L", b)[: n - 1]
            break
    return out


# --------------------------------------------------------------------------- #
# PDF stream
# --------------------------------------------------------------------------- #


class PDFStream(PDFObject):
    def __init__(self, dic: dict, rawdata: bytes, decipher=None) -> None:
        length = int_value(dic.get("Length", 0))
        eol = rawdata[length:]
        if eol in (b"\r", b"\n", b"\r\n"):
            rawdata = rawdata[:length]
        self.dic = dic
        self.rawdata = rawdata
        self.decipher = decipher
        self.data = None
        self.decdata = None
        self.objid = None
        self.genno = None

    def set_objid(self, objid: int, genno: int) -> None:
        self.objid = objid
        self.genno = genno

    def __repr__(self) -> str:
        if self.rawdata is not None:
            return f"<PDFStream({self.objid!r}): raw={len(self.rawdata)}, {self.dic!r}>"
        return f"<PDFStream({self.objid!r}): data={len(self.data)}, {self.dic!r}>"

    def decode(self) -> None:
        assert self.data is None and self.rawdata is not None
        data = self.rawdata
        if self.decipher:
            data = self.decipher(self.objid, self.genno, data)
            if gen_xref_stm:
                self.decdata = data
        if "Filter" not in self.dic:
            self.data = data
            self.rawdata = None
            return
        filters = self.dic["Filter"]
        if not isinstance(filters, list):
            filters = [filters]
        for f in filters:
            if f in LITERALS_FLATE_DECODE:
                data = zlib.decompress(data)
            elif f in LITERALS_LZW_DECODE:
                raise PDFNotImplementedError("LZW filter is not supported")
            elif f in LITERALS_ASCII85_DECODE:
                data = ascii85decode(data)
            elif f == LITERAL_CRYPT:
                raise PDFNotImplementedError("/Crypt filter is unsupported")
            else:
                raise PDFNotImplementedError(f"Unsupported filter: {f!r}")
            if "DP" in self.dic:
                params = self.dic["DP"]
            else:
                params = self.dic.get("DecodeParms", {})
            if "Predictor" in params:
                pred = int_value(params["Predictor"])
                if pred:
                    if pred != 12:
                        raise PDFNotImplementedError(f"Unsupported predictor: {pred!r}")
                    if "Columns" not in params:
                        raise PDFValueError("Columns undefined for predictor=12")
                    columns = int_value(params["Columns"])
                    buf = b""
                    ent0 = b"\x00" * columns
                    for i in range(0, len(data), columns + 1):
                        pred_byte = data[i]
                        ent1 = data[i + 1 : i + 1 + columns]
                        if pred_byte == 2:
                            ent1 = bytes(
                                (a + b) & 255 for (a, b) in zip(ent0, ent1)
                            )
                        buf += ent1
                        ent0 = ent1
                    data = buf
        self.data = data
        self.rawdata = None

    def get_data(self) -> bytes:
        if self.data is None:
            self.decode()
        return self.data

    def get_rawdata(self):
        return self.rawdata

    def get_decdata(self) -> bytes:
        if self.decdata is not None:
            return self.decdata
        data = self.rawdata
        if self.decipher and data:
            data = self.decipher(self.objid, self.genno, data)
        return data


# --------------------------------------------------------------------------- #
# XRef tables
# --------------------------------------------------------------------------- #


class PDFXRef:
    KEYWORD_TRAILER = KWD(b"trailer")

    def __init__(self) -> None:
        self.offsets: dict | None = None
        self.trailer: dict = {}

    def __repr__(self) -> str:
        return f"<PDFXRef: objs={len(self.offsets or {})}>"

    def objids(self):
        return iter(self.offsets.keys())

    def load(self, parser) -> None:
        self.offsets = {}
        while True:
            try:
                (pos, line) = parser.nextline()
            except PSEOF:
                raise PDFNoValidXRef("Unexpected EOF - file corrupted?")
            if not line:
                raise PDFNoValidXRef(f"Premature eof: {parser!r}")
            if line.startswith(b"trailer"):
                parser.seek(pos)
                break
            f = line.strip().split(b" ")
            if len(f) != 2:
                raise PDFNoValidXRef(f"Trailer not found: {parser!r}: line={line!r}")
            try:
                (start, nobjs) = map(int, f)
            except ValueError:
                raise PDFNoValidXRef(f"Invalid line: {parser!r}: line={line!r}")
            for objid in range(start, start + nobjs):
                try:
                    (_, line) = parser.nextline()
                except PSEOF:
                    raise PDFNoValidXRef("Unexpected EOF - file corrupted?")
                f = line.strip().split(b" ")
                if len(f) != 3:
                    raise PDFNoValidXRef(
                        f"Invalid XRef format: {parser!r}, line={line!r}"
                    )
                (pos_b, genno_b, use) = f
                if use != b"n":
                    continue
                self.offsets[objid] = (
                    int(genno_b.decode("utf-8")),
                    int(pos_b.decode("utf-8")),
                )
        self.load_trailer(parser)

    def load_trailer(self, parser) -> None:
        try:
            (_, kwd) = parser.nexttoken()
            assert kwd is self.KEYWORD_TRAILER
            (_, dic) = parser.nextobject(direct=True)
        except PSEOF:
            x = parser.pop(1)
            if not x:
                raise PDFNoValidXRef("Unexpected EOF - file corrupted")
            (_, dic) = x[0]
        self.trailer = dict_value(dic)

    def getpos(self, objid: int):
        (_genno, pos) = self.offsets[objid]
        return (None, pos)


class PDFXRefStream:
    def __init__(self) -> None:
        self.index: list = []
        self.data: bytes = b""
        self.entlen = 0
        self.fl1 = self.fl2 = self.fl3 = 0
        self.trailer: dict = {}

    def __repr__(self) -> str:
        return f"<PDFXRef: objids={self.index}>"

    def objids(self):
        for first, size in self.index:
            for objid in range(first, first + size):
                yield objid

    def load(self, parser) -> None:
        parser.nexttoken()  # objid
        parser.nexttoken()  # genno
        parser.nexttoken()  # 'obj'
        (_, stream) = parser.nextobject()
        if not isinstance(stream, PDFStream) or stream.dic["Type"] is not LITERAL_XREF:
            raise PDFNoValidXRef("Invalid PDF stream spec.")
        size = stream.dic["Size"]
        index = stream.dic.get("Index", (0, size))
        self.index = list(
            zip(
                itertools.islice(index, 0, None, 2),
                itertools.islice(index, 1, None, 2),
            )
        )
        (self.fl1, self.fl2, self.fl3) = stream.dic["W"]
        self.data = stream.get_data()
        self.entlen = self.fl1 + self.fl2 + self.fl3
        self.trailer = stream.dic

    def getpos(self, objid: int):
        offset = 0
        for first, size in self.index:
            if first <= objid < first + size:
                break
            offset += size
        else:
            raise KeyError(objid)
        i = self.entlen * ((objid - first) + offset)
        ent = self.data[i : i + self.entlen]
        f1 = _nunpack(ent[: self.fl1], 1)
        if f1 == 1:
            pos = _nunpack(ent[self.fl1 : self.fl1 + self.fl2])
            return (None, pos)
        if f1 == 2:
            objid2 = _nunpack(ent[self.fl1 : self.fl1 + self.fl2])
            index = _nunpack(ent[self.fl1 + self.fl2 :])
            return (objid2, index)
        raise KeyError(objid)


# --------------------------------------------------------------------------- #
# PDF document (ADEPT-only path)
# --------------------------------------------------------------------------- #


class PDFDocument:
    KEYWORD_OBJ = KWD(b"obj")

    def __init__(self) -> None:
        self.xrefs: list = []
        self.objs: dict = {}
        self.parsed_objs: dict = {}
        self.root = None
        self.catalog = None
        self.parser = None
        self.encryption = None
        self.decipher = None
        self.ready = False
        self.decrypt_key: bytes | None = None
        self.genkey = None

    def set_parser(self, parser) -> None:
        if self.parser:
            return
        self.parser = parser
        self.ready = True
        self.xrefs = parser.read_xref()
        for xref in self.xrefs:
            trailer = xref.trailer
            if not trailer:
                continue
            if "Encrypt" in trailer:
                try:
                    self.encryption = (
                        list_value(trailer["ID"]),
                        dict_value(trailer["Encrypt"]),
                    )
                except Exception:
                    self.encryption = (
                        b"ffffffffffffffffffffffffffffffffffff",
                        dict_value(trailer["Encrypt"]),
                    )
            if "Root" in trailer:
                self.set_root(dict_value(trailer["Root"]))
                break
            raise PDFSyntaxError("No /Root object! - Is this really a PDF?")
        self.ready = False

    def set_root(self, root) -> None:
        self.root = root
        self.catalog = dict_value(self.root)
        if self.catalog.get("Type") is not LITERAL_CATALOG and STRICT:
            raise PDFSyntaxError("Catalog not found!")

    # ------------------------------------------------------------------ #
    # Encryption: ADEPT EBX_HANDLER only
    # ------------------------------------------------------------------ #

    def initialize(self, userkey: bytes) -> None:
        if not self.encryption:
            self.ready = True
            raise PDFEncryptionError("Document is not encrypted.")
        (docid, param) = self.encryption
        filter_name = literal_name(param["Filter"])
        if filter_name != "EBX_HANDLER":
            raise PDFEncryptionError(
                f"Unsupported PDF encryption filter: {filter_name!r}. "
                "Only ADEPT EBX_HANDLER is supported."
            )
        self._initialize_ebx(userkey, docid, param)

    @staticmethod
    def _remove_hardening(rights, keytype: str, keydata: bytes) -> bytes:
        adept = lambda tag: f"{{http://ns.adobe.com/adept}}{tag}"
        get = lambda name: "".join(rights.findtext(f".//{adept(name)}"))

        resource = UUID(get("resource"))
        device = UUID(get("device"))
        fulfillment = UUID(get("fulfillment")[:36])
        kekiv = UUID(int=resource.int ^ device.int ^ fulfillment.int).bytes

        rem = int(keytype, 10) % 16
        h = _sha256(keytype.encode("ascii"))
        kek = h[2 * rem : 16 + rem] + h[rem : 2 * rem]

        return _unpad_pkcs7(AES.new(kek, AES.MODE_CBC, kekiv).decrypt(keydata))

    def _initialize_ebx(self, userkey: bytes, docid, param: dict) -> None:
        rsakey = RSA.importKey(userkey)
        length = int_value(param.get("Length", 0)) // 8

        rights_raw = param.get("ADEPT_LICENSE")
        if rights_raw is None:
            raise ADEPTError("PDF is missing /ADEPT_LICENSE — is it really ADEPT?")
        if isinstance(rights_raw, str):
            rights_raw = rights_raw.encode("latin-1")
        rights = codecs.decode(rights_raw, "base64")
        rights = zlib.decompress(rights, -15)
        rights = etree.fromstring(rights)

        expr = ".//{http://ns.adobe.com/adept}encryptedKey"
        bookkey_elem = rights.find(expr)
        if bookkey_elem is None or bookkey_elem.text is None:
            raise ADEPTError("ADEPT_LICENSE rights.xml missing encryptedKey")
        bookkey = codecs.decode(bookkey_elem.text.encode("utf-8"), "base64")
        keytype = bookkey_elem.attrib.get("keyType", "0")

        if int(keytype, 10) > 2:
            bookkey = self._remove_hardening(rights, keytype, bookkey)

        try:
            bookkey = PKCS1_v1_5.new(rsakey).decrypt(bookkey, None)
        except ValueError:
            bookkey = None
        if not bookkey:
            raise ADEPTError("Failed to decrypt book session key (wrong user key?)")

        ebx_V = int_value(param.get("V", 4))
        if length > 0:
            if len(bookkey) == length:
                V = 3 if ebx_V == 3 else 2
            elif len(bookkey) == length + 1:
                V = bookkey[0]
                bookkey = bookkey[1:]
            else:
                raise ADEPTError(
                    f"Book session key length mismatch: got {len(bookkey)}, expected {length}"
                )
        else:
            V = 3 if ebx_V == 3 else 2

        self.decrypt_key = bookkey
        self.genkey = self._genkey_v3 if V == 3 else self._genkey_v2
        self.decipher = self._decrypt_rc4
        self.ready = True

    def _genkey_v2(self, objid: int, genno: int) -> bytes:
        o = struct.pack("<L", objid)[:3]
        g = struct.pack("<L", genno)[:2]
        key = self.decrypt_key + o + g
        return hashlib.md5(key).digest()[: min(len(self.decrypt_key) + 5, 16)]

    def _genkey_v3(self, objid: int, genno: int) -> bytes:
        o = struct.pack("<L", objid ^ 0x3569AC)
        g = struct.pack("<L", genno ^ 0xCA96)
        key = (
            self.decrypt_key
            + bytes([o[0], g[0], o[1], g[1], o[2]])
            + b"sAlT"
        )
        return hashlib.md5(key).digest()[: min(len(self.decrypt_key) + 5, 16)]

    def _decrypt_rc4(self, objid: int, genno: int, data: bytes) -> bytes:
        key = self.genkey(objid, genno)
        return ARC4.new(key).decrypt(data)

    # ------------------------------------------------------------------ #
    # Object resolution
    # ------------------------------------------------------------------ #

    def getobj(self, objid: int):
        if not self.ready:
            raise PDFException("PDFDocument not initialized")
        if objid in self.objs:
            return self.objs[objid]

        for xref in self.xrefs:
            try:
                (stmid, index) = xref.getpos(objid)
                break
            except KeyError:
                continue
        else:
            return None

        if stmid:
            if gen_xref_stm:
                return PDFObjStmRef(objid, stmid, index)
            stream = stream_value(self.getobj(stmid))
            if stream.dic.get("Type") is not LITERAL_OBJSTM and STRICT:
                raise PDFSyntaxError(f"Not an object stream: {stream!r}")
            try:
                n = stream.dic["N"]
            except KeyError:
                if STRICT:
                    raise PDFSyntaxError(f"N is not defined: {stream!r}")
                n = 0
            if stmid in self.parsed_objs:
                objs = self.parsed_objs[stmid]
            else:
                parser = PDFObjStrmParser(stream.get_data(), self)
                objs = []
                try:
                    while True:
                        (_, obj) = parser.nextobject()
                        objs.append(obj)
                except PSEOF:
                    pass
                self.parsed_objs[stmid] = objs
            genno = 0
            i = n * 2 + index
            try:
                obj = objs[i]
            except IndexError:
                if STRICT:
                    raise PDFSyntaxError(f"Invalid object number: objid={objid!r}")
                return None
            if isinstance(obj, PDFStream):
                obj.set_objid(objid, 0)
        else:
            self.parser.seek(index)
            self.parser.nexttoken()  # objid
            (_, genno) = self.parser.nexttoken()
            (_, kwd) = self.parser.nexttoken()
            if kwd is not self.KEYWORD_OBJ:
                raise PDFSyntaxError(f"Invalid object spec: offset={index!r}")
            (_, obj) = self.parser.nextobject()
            if isinstance(obj, PDFStream):
                obj.set_objid(objid, genno)
            if self.decipher:
                obj = decipher_all(self.decipher, objid, genno, obj)
        self.objs[objid] = obj
        return obj


class PDFObjStmRef:
    maxindex = 0

    def __init__(self, objid: int, stmid: int, index: int) -> None:
        self.objid = objid
        self.stmid = stmid
        self.index = index
        if index > PDFObjStmRef.maxindex:
            PDFObjStmRef.maxindex = index


# --------------------------------------------------------------------------- #
# PDF parsers
# --------------------------------------------------------------------------- #


class PDFParser(PSStackParser):
    KEYWORD_R = KWD(b"R")
    KEYWORD_ENDOBJ = KWD(b"endobj")
    KEYWORD_STREAM = KWD(b"stream")
    KEYWORD_XREF = KWD(b"xref")
    KEYWORD_STARTXREF = KWD(b"startxref")

    def __init__(self, doc: PDFDocument, fp) -> None:
        super().__init__(fp)
        self.doc = doc
        self.doc.set_parser(self)

    def __repr__(self) -> str:
        return "<PDFParser>"

    def do_keyword(self, pos, token) -> None:
        if token in (self.KEYWORD_XREF, self.KEYWORD_STARTXREF):
            self.add_results(*self.pop(1))
            return
        if token is self.KEYWORD_ENDOBJ:
            self.add_results(*self.pop(4))
            return
        if token is self.KEYWORD_R:
            try:
                ((_, objid), (_, genno)) = self.pop(2)
                obj = PDFObjRef(self.doc, int(objid), int(genno))
                self.push((pos, obj))
            except PSSyntaxError:
                pass
            return
        if token is self.KEYWORD_STREAM:
            ((_, dic),) = self.pop(1)
            dic = dict_value(dic)
            try:
                objlen = int_value(dic["Length"])
            except KeyError:
                if STRICT:
                    raise PDFSyntaxError(f"/Length is undefined: {dic!r}")
                objlen = 0
            self.seek(pos)
            try:
                (_, line) = self.nextline()
            except PSEOF:
                if STRICT:
                    raise PDFSyntaxError("Unexpected EOF")
                return
            pos += len(line)
            self.fp.seek(pos)
            data = self.fp.read(objlen)
            self.seek(pos + objlen)
            while True:
                try:
                    (_, line) = self.nextline()
                except PSEOF:
                    if STRICT:
                        raise PDFSyntaxError("Unexpected EOF")
                    break
                if b"endstream" in line:
                    i = line.index(b"endstream")
                    objlen += i
                    data += line[:i]
                    break
                objlen += len(line)
                data += line
            self.seek(pos + objlen)
            obj = PDFStream(dic, data, self.doc.decipher)
            self.push((pos, obj))
            return

        self.push((pos, token))

    def find_xref(self) -> int:
        prev = None
        for line in self.revreadlines():
            line = line.strip()
            if line == b"startxref":
                break
            if line:
                prev = line
        else:
            raise PDFNoValidXRef("Unexpected EOF")
        return int(prev)

    def read_xref_from(self, start: int, xrefs: list) -> None:
        self.seek(start)
        self.reset()
        try:
            (pos, token) = self.nexttoken()
        except PSEOF:
            raise PDFNoValidXRef("Unexpected EOF")
        if isinstance(token, int):
            if GEN_XREF_STM == 1:
                global gen_xref_stm
                gen_xref_stm = True
            self.seek(pos)
            self.reset()
            xref = PDFXRefStream()
            xref.load(self)
        else:
            if token is not self.KEYWORD_XREF:
                raise PDFNoValidXRef(f"xref not found: pos={pos}, token={token!r}")
            self.nextline()
            xref = PDFXRef()
            xref.load(self)
        xrefs.append(xref)
        trailer = xref.trailer
        if "XRefStm" in trailer:
            pos = int_value(trailer["XRefStm"])
            self.read_xref_from(pos, xrefs)
        if "Prev" in trailer:
            pos = int_value(trailer["Prev"])
            self.read_xref_from(pos, xrefs)

    def read_xref(self) -> list:
        xrefs: list = []
        try:
            pos = self.find_xref()
            self.read_xref_from(pos, xrefs)
        except PDFNoValidXRef:
            # Fallback: scan every `N G obj` header.
            self.seek(0)
            pat = re.compile(rb"^(\d+)\s+(\d+)\s+obj\b")
            offsets: dict = {}
            xref = PDFXRef()
            trailerpos = None
            while True:
                try:
                    (pos, line) = self.nextline()
                except PSEOF:
                    break
                if line.startswith(b"trailer"):
                    trailerpos = pos
                m = pat.match(line)
                if not m:
                    continue
                (objid, _genno) = m.groups()
                offsets[int(objid)] = (0, pos)
            if not offsets:
                raise
            xref.offsets = offsets
            if trailerpos is not None:
                self.seek(trailerpos)
                xref.load_trailer(self)
                xrefs.append(xref)
        return xrefs


class PDFObjStrmParser(PDFParser):
    KEYWORD_R = KWD(b"R")

    def __init__(self, data: bytes, doc: PDFDocument) -> None:
        PSStackParser.__init__(self, BytesIO(data))
        self.doc = doc

    def flush(self) -> None:
        self.add_results(*self.popall())

    def do_keyword(self, pos, token) -> None:
        if token is self.KEYWORD_R:
            try:
                ((_, objid), (_, genno)) = self.pop(2)
                obj = PDFObjRef(self.doc, int(objid), int(genno))
                self.push((pos, obj))
            except PSSyntaxError:
                pass
            return
        self.push((pos, token))


# --------------------------------------------------------------------------- #
# Serializer
# --------------------------------------------------------------------------- #


class PDFSerializer:
    def __init__(self, inf, userkey: bytes) -> None:
        global gen_xref_stm
        gen_xref_stm = GEN_XREF_STM > 1
        self.version = inf.read(8)
        inf.seek(0)
        self.doc = PDFDocument()
        PDFParser(self.doc, inf)
        self.doc.initialize(userkey)

        self.objids: set = set()
        trailer: dict = {}
        for xref in reversed(self.doc.xrefs):
            trailer = xref.trailer
            for objid in xref.objids():
                self.objids.add(objid)
        trailer = dict(trailer)
        trailer.pop("Prev", None)
        trailer.pop("XRefStm", None)
        if "Encrypt" in trailer:
            self.objids.discard(trailer.pop("Encrypt").objid)
        self.trailer = trailer
        self.last: bytes = b""

    # -- writer helpers ------------------------------------------------- #

    def write(self, data: bytes) -> None:
        self.outf.write(data)
        self.last = data[-1:]

    def tell(self) -> int:
        return self.outf.tell()

    def escape_string(self, string: bytes) -> bytes:
        string = string.replace(b"\\", b"\\\\")
        string = string.replace(b"\n", b"\\n")
        string = string.replace(b"(", b"\\(")
        string = string.replace(b")", b"\\)")
        return string

    def serialize_object(self, obj) -> None:
        if isinstance(obj, dict):
            if (
                "ResFork" in obj
                and "Type" in obj
                and "Subtype" not in obj
                and isinstance(obj["Type"], int)
            ):
                obj["Subtype"] = obj["Type"]
                del obj["Type"]
            self.write(b"<<")
            for key, val in obj.items():
                self.write(str(LIT(key.encode("utf-8"))).encode("utf-8"))
                self.serialize_object(val)
            self.write(b">>")
        elif isinstance(obj, list):
            self.write(b"[")
            for val in obj:
                self.serialize_object(val)
            self.write(b"]")
        elif isinstance(obj, bytearray):
            self.write(b"(" + self.escape_string(bytes(obj)) + b")")
        elif isinstance(obj, bytes):
            self.write(b"<" + binascii.hexlify(obj).upper() + b">")
        elif isinstance(obj, str):
            self.write(b"(" + self.escape_string(obj.encode("utf-8")) + b")")
        elif isinstance(obj, bool):
            if self.last.isalnum():
                self.write(b" ")
            self.write(str(obj).lower().encode("utf-8"))
        elif isinstance(obj, int):
            if self.last.isalnum():
                self.write(b" ")
            self.write(str(obj).encode("utf-8"))
        elif isinstance(obj, Decimal):
            if self.last.isalnum():
                self.write(b" ")
            self.write(str(obj).encode("utf-8"))
        elif isinstance(obj, PDFObjRef):
            if self.last.isalnum():
                self.write(b" ")
            self.write(f"{obj.objid} 0 R".encode("utf-8"))
        elif isinstance(obj, PDFStream):
            if obj.dic.get("Type") == LITERAL_OBJSTM and not gen_xref_stm:
                self.write(b"(deleted)")
            else:
                data = obj.get_decdata()
                if "Length" in obj.dic:
                    obj.dic["Length"] = len(data)
                self.serialize_object(obj.dic)
                self.write(b"stream\n")
                self.write(data)
                self.write(b"\nendstream")
        else:
            data = str(obj).encode("utf-8")
            if bytes([data[0]]).isalnum() and self.last.isalnum():
                self.write(b" ")
            self.write(data)

    def serialize_indirect(self, objid: int, obj) -> None:
        self.write(f"{objid} 0 obj".encode("utf-8"))
        self.serialize_object(obj)
        if self.last.isalnum():
            self.write(b"\n")
        self.write(b"endobj\n")

    # -- top-level emission --------------------------------------------- #

    def dump(self, outf) -> None:
        self.outf = outf
        self.write(self.version)
        self.write(b"\n%\xe2\xe3\xcf\xd3\n")
        doc = self.doc
        xrefs: dict = {}
        maxobj = max(self.objids)
        trailer = dict(self.trailer)
        trailer["Size"] = maxobj + 1
        for objid in self.objids:
            obj = doc.getobj(objid)
            if isinstance(obj, PDFObjStmRef):
                xrefs[objid] = obj
                continue
            if obj is not None:
                xrefs[objid] = (self.tell(), 0)
                self.serialize_indirect(objid, obj)
        startxref = self.tell()

        if not gen_xref_stm:
            self.write(b"xref\n")
            self.write(f"0 {maxobj + 1}\n".encode("utf-8"))
            for objid in range(maxobj + 1):
                if objid in xrefs:
                    self.write(f"{xrefs[objid][0]:010d} 00000 n \n".encode("utf-8"))
                else:
                    self.write(b"0000000000 65535 f \n")
            self.write(b"trailer\n")
            self.serialize_object(trailer)
            self.write(f"\nstartxref\n{startxref}\n%%EOF".encode("utf-8"))
            return

        # Cross-reference stream variant (for PDFs that originally used one).
        maxoffset = max(startxref, maxobj)
        maxindex = PDFObjStmRef.maxindex
        fl2 = 2
        power = 65536
        while maxoffset >= power:
            fl2 += 1
            power *= 256
        fl3 = 1
        power = 256
        while maxindex >= power:
            fl3 += 1
            power *= 256

        index: list = []
        first = None
        prev = None
        data_chunks: list = []
        startxref = self.tell()
        maxobj += 1
        xrefs[maxobj] = (startxref, 0)
        for objid in sorted(xrefs):
            if first is None:
                first = objid
            elif objid != prev + 1:
                index.extend((first, prev - first + 1))
                first = objid
            prev = objid
            objref = xrefs[objid]
            if isinstance(objref, PDFObjStmRef):
                f1, f2, f3 = 2, objref.stmid, objref.index
            else:
                f1, f2, f3 = 1, objref[0], 0
            data_chunks.append(struct.pack(">B", f1))
            data_chunks.append(struct.pack(">L", f2)[-fl2:])
            data_chunks.append(struct.pack(">L", f3)[-fl3:])
        index.extend((first, prev - first + 1))
        data = zlib.compress(b"".join(data_chunks))
        dic = {
            "Type": LITERAL_XREF,
            "Size": prev + 1,
            "Index": index,
            "W": [1, fl2, fl3],
            "Length": len(data),
            "Filter": LITERALS_FLATE_DECODE[0],
            "Root": trailer["Root"],
        }
        if "Info" in trailer:
            dic["Info"] = trailer["Info"]
        xrefstm = PDFStream(dic, data)
        self.serialize_indirect(maxobj, xrefstm)
        self.write(f"startxref\n{startxref}\n%%EOF".encode("utf-8"))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def decrypt_pdf(userkey: bytes, inpath: Path, outpath: Path) -> int:
    """Decrypt an Adobe ADEPT-protected PDF.

    Return codes match :func:`ade_dedrm.epub.decrypt_book`:
        0 — success
        1 — not DRM-protected
        2 — wrong key / decryption failure
    """
    inpath = Path(inpath)
    outpath = Path(outpath)

    try:
        with inpath.open("rb") as inf:
            serializer = PDFSerializer(inf, userkey)
            with outpath.open("wb") as outf:
                serializer.dump(outf)
    except PDFEncryptionError as exc:
        if "not encrypted" in str(exc):
            return 1
        raise ADEPTError(str(exc)) from exc
    except ADEPTError:
        if outpath.exists():
            outpath.unlink()
        return 2
    except Exception:
        if outpath.exists():
            outpath.unlink()
        raise
    return 0

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/binary_handler.py (v2.5.1, 9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - every read checks for EOF and raises BinaryEOFError (the original read_asciiz compared to "" instead of b"" and
#   looped forever at EOF; the other readers raised struct.error);
# - strings use one lossless codec (cp1252, with the 5 bytes cp1252 leaves undefined mapped to the C1 code points as
#   Windows does) instead of utf-8/"replace" on read and ascii on write; decoded strings never hold surrogates;
# - writers reject NUL characters inside zero-terminated strings;
# - added BinaryReader (bounds-checked cursor over an in-memory buffer) and helpers to move little-endian 32-bit
#   arrays to and from bytes without per-element work.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Readers and writers for the little-endian data types of BI file formats.

See https://community.bistudio.com/wiki/Generic_FileFormat_Data_Types.

Two styles are offered:

- ``read_*``/``write_*`` functions on binary file objects (port of A3OB's ``binary_handler``);
- ``BinaryReader``, a bounds-checked cursor over an in-memory buffer, used by the P3D reader for speed.

Every read raises ``BinaryEOFError`` (a subclass of ``EOFError``) when the data ends early. No read loops at EOF.
"""

from __future__ import annotations

import codecs
import struct
import sys
from array import array

# --------------------------------------------------------------------------------------------------------------------
# Strings
# --------------------------------------------------------------------------------------------------------------------

#: Codec for strings stored in BI files. The files carry no encoding information; the tools are Windows ANSI programs,
#: so cp1252 is the most likely code page. The codec is lossless and only produces valid Unicode: the 5 bytes cp1252
#: leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) decode to the C1 control characters U+0081, U+008D, U+008F, U+0090
#: and U+009D (what Windows ``MultiByteToWideChar`` does for code page 1252) and encode back to the same byte. Python's
#: ``surrogateescape`` is not used: lone surrogates crash Houdini when stored in string attributes or group names.
STRING_ENCODING = "cp1252"

#: Bytes cp1252 does not define; they map to the code point of the same value.
CP1252_UNDEFINED = (0x81, 0x8D, 0x8F, 0x90, 0x9D)

_DECODING_TABLE = "".join(
    chr(byte) if byte in CP1252_UNDEFINED else bytes((byte,)).decode(STRING_ENCODING) for byte in range(256))
_ENCODING_TABLE = codecs.charmap_build(_DECODING_TABLE)


def decode_string(raw) -> str:
    """Decode bytes read from a file into ``str``, losslessly. The result never contains surrogates."""
    return codecs.charmap_decode(bytes(raw), "strict", _DECODING_TABLE)[0]


def encode_string(value) -> bytes:
    """Encode a ``str`` (or pass ``bytes`` through) for writing. Raises ``ValueError`` if it cannot be encoded."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return codecs.charmap_encode(value, "strict", _ENCODING_TABLE)[0]
    except UnicodeEncodeError as ex:
        raise ValueError("string %r cannot be encoded as %s: %s" % (value, STRING_ENCODING, ex)) from None


def encode_asciiz(value) -> bytes:
    """Encode a zero-terminated string, terminator included."""
    raw = encode_string(value)
    if b"\x00" in raw:
        raise ValueError("zero-terminated string %r contains a NUL character" % (value,))
    return raw + b"\x00"


# --------------------------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------------------------

class BinaryEOFError(EOFError):
    """The data ended before a value could be read."""


# --------------------------------------------------------------------------------------------------------------------
# 32-bit arrays
# --------------------------------------------------------------------------------------------------------------------

def _typecode_u32() -> str:
    for code in ("I", "L"):
        if array(code).itemsize == 4:
            return code
    raise ImportError("no 4-byte unsigned array typecode on this platform")


#: ``array`` typecodes of 4-byte unsigned ints and IEEE-754 single floats.
U32 = _typecode_u32()
F32 = "f"
if array(F32).itemsize != 4:  # pragma: no cover - every CPython platform has 4-byte C floats
    raise ImportError("array('f') is not 4 bytes on this platform")

_BIG_ENDIAN = sys.byteorder == "big"


def array_from_bytes(typecode: str, data) -> array:
    """Build an ``array`` from little-endian bytes. A plain memory copy: f32 values keep their exact bits."""
    output = array(typecode)
    output.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover
        output.byteswap()
    return output


def array_to_bytes(values: array) -> bytes:
    """Little-endian bytes of an ``array``."""
    if _BIG_ENDIAN:  # pragma: no cover
        values = array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def as_array(typecode: str, values) -> array:
    """Return ``values`` as an ``array`` of ``typecode`` (no copy if it already is one)."""
    if isinstance(values, array) and values.typecode == typecode:
        return values
    return array(typecode, values)


# --------------------------------------------------------------------------------------------------------------------
# Buffer reader
# --------------------------------------------------------------------------------------------------------------------

_U8 = struct.Struct("<B")
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")


class BinaryReader:
    """Bounds-checked little-endian cursor over an in-memory buffer."""

    __slots__ = ("data", "view", "pos", "size")

    def __init__(self, data, pos: int = 0):
        self.data = bytes(data)
        self.view = memoryview(self.data)
        self.pos = pos
        self.size = len(self.data)

    @property
    def remaining(self) -> int:
        return self.size - self.pos

    def need(self, count: int, what: str) -> None:
        if count < 0 or self.pos + count > self.size:
            raise BinaryEOFError(
                "unexpected end of data at offset %d while reading %s: need %d bytes, %d left"
                % (self.pos, what, count, self.size - self.pos)
            )

    def take(self, count: int, what: str = "data") -> memoryview:
        """Return the next ``count`` bytes as a memoryview and advance."""
        self.need(count, what)
        start = self.pos
        self.pos = start + count
        return self.view[start:self.pos]

    def u8(self, what: str = "byte") -> int:
        self.need(1, what)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u32(self, what: str = "uint32") -> int:
        self.need(4, what)
        value = _U32.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def u32s(self, count: int, what: str = "uint32 values") -> tuple:
        self.need(4 * count, what)
        values = struct.unpack_from("<%dI" % count, self.data, self.pos)
        self.pos += 4 * count
        return values

    def f32(self, what: str = "float32") -> float:
        self.need(4, what)
        value = _F32.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def asciiz(self, what: str = "string") -> bytes:
        """Read a zero-terminated string and return its raw bytes (without the terminator)."""
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise BinaryEOFError("unterminated %s at offset %d: no NUL before end of data" % (what, self.pos))
        value = self.data[self.pos:end]
        self.pos = end + 1
        return value


# --------------------------------------------------------------------------------------------------------------------
# File object readers (port of binary_handler)
# --------------------------------------------------------------------------------------------------------------------

def read_exact(file, count: int) -> bytes:
    """Read exactly ``count`` bytes or raise ``BinaryEOFError``."""
    data = file.read(count)
    if len(data) != count:
        raise BinaryEOFError("unexpected end of file: needed %d bytes, got %d" % (count, len(data)))
    return data


def read_byte(file) -> int:
    return read_exact(file, 1)[0]


def read_bytes(file, count: int = 1) -> tuple:
    return tuple(read_exact(file, count))


def read_bool(file) -> bool:
    return read_byte(file) != 0


def read_short(file) -> int:
    return struct.unpack("<h", read_exact(file, 2))[0]


def read_shorts(file, count: int = 1) -> tuple:
    return struct.unpack("<%dh" % count, read_exact(file, 2 * count))


def read_ushort(file) -> int:
    return struct.unpack("<H", read_exact(file, 2))[0]


def read_ushorts(file, count: int = 1) -> tuple:
    return struct.unpack("<%dH" % count, read_exact(file, 2 * count))


def read_long(file) -> int:
    return struct.unpack("<i", read_exact(file, 4))[0]


def read_longs(file, count: int = 1) -> tuple:
    return struct.unpack("<%di" % count, read_exact(file, 4 * count))


def read_ulong(file) -> int:
    return struct.unpack("<I", read_exact(file, 4))[0]


def read_ulongs(file, count: int = 1) -> tuple:
    return struct.unpack("<%dI" % count, read_exact(file, 4 * count))


def read_compressed_uint(file) -> int:
    output = read_byte(file)
    extra = output
    byte_idx = 1
    while extra & 0x80:
        extra = read_byte(file)
        output += (extra - 1) << (byte_idx * 7)
        byte_idx += 1
    return output


def read_half(file) -> float:
    return struct.unpack("<e", read_exact(file, 2))[0]


def read_halfs(file, count: int = 1) -> tuple:
    return struct.unpack("<%de" % count, read_exact(file, 2 * count))


def read_float(file) -> float:
    return struct.unpack("<f", read_exact(file, 4))[0]


def read_floats(file, count: int = 1) -> tuple:
    return struct.unpack("<%df" % count, read_exact(file, 4 * count))


def read_double(file) -> float:
    return struct.unpack("<d", read_exact(file, 8))[0]


def read_doubles(file, count: int = 1) -> tuple:
    return struct.unpack("<%dd" % count, read_exact(file, 8 * count))


def read_char(file, count: int = 1) -> str:
    return decode_string(read_exact(file, count))


def read_asciiz(file) -> str:
    """Read a zero-terminated string. Raises ``BinaryEOFError`` if the file ends before the terminator."""
    chunks = bytearray()
    while True:
        char = file.read(1)
        if not char:
            raise BinaryEOFError("unexpected end of file inside a zero-terminated string")
        if char == b"\x00":
            return decode_string(chunks)
        chunks += char


def read_asciiz_field(file, field_len: int) -> str:
    field = read_exact(file, field_len)
    end = field.find(b"\x00")
    if end < 0:
        raise ValueError("ASCIIZ field of %d bytes has no terminator" % field_len)
    return decode_string(field[:end])


def read_lascii(file) -> str:
    length = read_byte(file)
    return decode_string(read_exact(file, length))


# --------------------------------------------------------------------------------------------------------------------
# File object writers (port of binary_handler)
# --------------------------------------------------------------------------------------------------------------------

def write_byte(file, *args) -> None:
    file.write(struct.pack("%dB" % len(args), *args))


def write_bool(file, value) -> None:
    write_byte(file, 1 if value else 0)


def write_short(file, *args) -> None:
    file.write(struct.pack("<%dh" % len(args), *args))


def write_ushort(file, *args) -> None:
    file.write(struct.pack("<%dH" % len(args), *args))


def write_long(file, *args) -> None:
    file.write(struct.pack("<%di" % len(args), *args))


def write_ulong(file, *args) -> None:
    file.write(struct.pack("<%dI" % len(args), *args))


def write_compressed_uint(file, value: int) -> None:
    if value < 0:
        raise ValueError("compressed uint cannot be negative: %d" % value)
    temp = value
    while True:
        if temp < 128:
            write_byte(file, temp)
            break
        write_byte(file, (temp & 127) + 128)
        temp = temp >> 7


def write_half(file, *args) -> None:
    file.write(struct.pack("<%de" % len(args), *args))


def write_float(file, *args) -> None:
    file.write(struct.pack("<%df" % len(args), *args))


def write_double(file, *args) -> None:
    file.write(struct.pack("<%dd" % len(args), *args))


def write_chars(file, value) -> None:
    file.write(encode_string(value))


def write_asciiz(file, value) -> None:
    file.write(encode_asciiz(value))


def write_asciiz_field(file, value, field_len: int) -> None:
    raw = encode_asciiz(value)
    if len(raw) > field_len:
        raise ValueError("ASCIIZ value is longer (%d + 1) than field length (%d)" % (len(raw) - 1, field_len))
    file.write(raw.ljust(field_len, b"\x00"))


def write_lascii(file, value) -> None:
    raw = encode_string(value)
    if len(raw) > 255:
        raise ValueError("LASCII string cannot be longer than 255 bytes")
    file.write(bytes((len(raw),)) + raw)

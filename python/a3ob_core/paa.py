# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Derived from A3OB (v2.5.1): MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#   io/data_paa.py     PAA_File.read (:128), PAA_TAGG.read (:40), PAA_MIPMAP.read (:59), PAA_MIPMAP.decompress (:75)
#   io/compression.py  lzo1x_decompress (:20), dxt5_decompress (:141), dxt1_decompress (:289)
# Modified: bytes and offsets instead of file objects; the DXT blocks decode to 8-bit RGBA with integer arithmetic (A3OB
# makes float channels for Blender); rows come out top to bottom (A3OB: bottom to top); the largest mipmap only; numpy
# is used when it can be imported, with the plain-Python path as the fallback.
"""PAA texture reader (task #53, D29). Decodes the largest mipmap of a ``.paa`` file to RGBA8.

``decode_paa(path)`` gives ``(width, height, rgba)``: ``rgba`` is ``bytes``, 4 bytes per pixel, rows from the top of the
picture down. ``decode_paa_bytes(data)`` does the same for bytes already in memory. ``read_paa(data)`` gives the parsed
header (type, TAGGs, mipmaps) without decoding.

Like A3OB, this decodes DXT1 and DXT5 (LZO-compressed mipmaps where the flag is set). The other types in the format
(DXT2 to DXT4, ARGB4444, ARGB1555, ARGB8888, AI88) raise ``PaaError`` with the type named, since A3OB cannot decode
them either. Palettes are not supported (A3OB: same).

numpy is optional. This module imports it inside a ``try``; without it the plain-Python decoder runs (the same result,
but slow on big textures). ``a3ob_core`` never imports ``hou``.
"""

import struct
from typing import List, Optional, Tuple

try:  # optional: Houdini 22 ships numpy 2.3.2
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the interpreter
    _np = None

__all__ = ["PaaError", "PaaTagg", "PaaMipmap", "PaaFile", "read_paa", "decode_paa", "decode_paa_bytes", "lzo1x_decompress",
           "decode_dxt1", "decode_dxt5", "TYPE_DXT1", "TYPE_DXT5"]

TYPE_DXT1 = 0xFF01
TYPE_DXT5 = 0xFF05
#: Names of the types A3OB knows (data_paa.py PAA_Type), for the error text.
TYPE_NAMES = {0xFF01: "DXT1", 0xFF02: "DXT2", 0xFF03: "DXT3", 0xFF04: "DXT4", 0xFF05: "DXT5", 0x4444: "ARGB4444",
              0x1555: "ARGB1555", 0x8888: "ARGB8888", 0x8080: "AI88"}


class PaaError(Exception):
    """A .paa file that cannot be read or decoded. The text says what and why."""


# ---------------------------------------------------------------------------------------------------------------------
# LZO1X (compression.py lzo1x_decompress, :20)
# ---------------------------------------------------------------------------------------------------------------------

def lzo1x_decompress(data, expected: int) -> bytearray:
    """Decompress an LZO1X stream that must give exactly ``expected`` bytes. Raises ``PaaError`` on a bad stream."""
    out = bytearray()
    pos = 0
    state = 0
    size = len(data)

    def need(count):
        if pos + count > size:
            raise PaaError("the LZO data ends early")

    def room(length):
        if expected - len(out) < length:
            raise PaaError("the LZO data is longer than the %d bytes expected (output overrun)" % expected)

    def copy_match(distance, length):
        room(length)
        start = len(out) - distance
        if distance <= 0 or start < 0:
            raise PaaError("the LZO data points before the start of the output")
        # A match may be longer than its distance (a repeating pattern), so copy whole chunks of ``distance`` bytes.
        out.extend(out[start:] * (length // distance))
        out.extend(out[start:start + (length % distance)])

    def get_length(x, mask):
        nonlocal pos
        length = x & mask
        if not length:
            while True:
                need(1)
                x = data[pos]
                pos += 1
                if x:
                    break
                length += 255
            length += mask + x
        return length

    def literals(count):
        nonlocal pos
        room(count)
        need(count)
        out.extend(data[pos:pos + count])
        pos += count

    def next_byte():
        nonlocal pos
        need(1)
        pos += 1
        return data[pos - 1]

    x = next_byte()
    if x > 17:
        length = x - 17
        literals(length)
        state = min(4, length)
        x = next_byte()
    while True:
        if x <= 15:
            if not state:
                length = 3 + get_length(x, 15)
                literals(length)
                state = 4
            elif state < 4:
                state = x & 3
                copy_match((next_byte() << 2) + (x >> 2) + 1, 2)
                literals(state)
            else:
                state = x & 3
                copy_match((next_byte() << 2) + (x >> 2) + 2049, 3)
                literals(state)
        elif x > 127:
            state = x & 3
            copy_match((next_byte() << 3) + ((x >> 2) & 7) + 1, 5 + ((x >> 5) & 3))
            literals(state)
        elif x > 63:
            state = x & 3
            copy_match((next_byte() << 3) + ((x >> 2) & 7) + 1, 3 + ((x >> 5) & 1))
            literals(state)
        elif x > 31:
            length = 2 + get_length(x, 31)
            need(2)
            extra = data[pos] | (data[pos + 1] << 8)
            pos += 2
            state = extra & 3
            copy_match((extra >> 2) + 1, length)
            literals(state)
        else:
            length = 2 + get_length(x, 7)
            need(2)
            extra = data[pos] | (data[pos + 1] << 8)
            pos += 2
            distance = 16384 + ((x & 8) << 11) + (extra >> 2)
            state = extra & 3
            if distance == 16384:
                if length != 3:
                    raise PaaError("the LZO data has a bad end marker")
                break
            copy_match(distance, length)
            literals(state)
        x = next_byte()
    if len(out) != expected:
        raise PaaError("the LZO data gave %d bytes, %d expected" % (len(out), expected))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# DXT1 and DXT5 (compression.py dxt1_decompress :289, dxt5_decompress :141)
#
# All arithmetic is integer, so numpy and plain Python give the same bytes. A 5 or 6 bit channel is widened with
# round(v * 255 / max); the two in-between colours are the rounded 2/3 and 1/3 mixes (or the half mix), like A3OB's.
# ---------------------------------------------------------------------------------------------------------------------

def _expand565(v: int) -> Tuple[int, int, int]:
    return ((v >> 11) * 255 + 15) // 31, (((v >> 5) & 0x3F) * 255 + 31) // 63, ((v & 0x1F) * 255 + 15) // 31


def _color_palette(v0: int, v1: int, four_colors: bool):
    """Four (r, g, b, a) entries. ``four_colors``: DXT5 always (alpha is separate); DXT1 only when v0 > v1."""
    c0, c1 = _expand565(v0), _expand565(v1)
    if v0 > v1:
        c2 = tuple((2 * a + b + 1) // 3 for a, b in zip(c0, c1))
        c3 = tuple((a + 2 * b + 1) // 3 for a, b in zip(c0, c1))
        return (c0 + (255,), c1 + (255,), c2 + (255,), c3 + (255,))
    c2 = tuple((a + b + 1) // 2 for a, b in zip(c0, c1))
    return (c0 + (255,), c1 + (255,), c2 + (255,), (0, 0, 0, 255 if four_colors else 0))


def _alpha_palette(a0: int, a1: int):
    if a0 > a1:
        return [a0, a1] + [((7 - k) * a0 + k * a1 + 3) // 7 for k in range(1, 7)]
    return [a0, a1] + [((5 - k) * a0 + k * a1 + 2) // 5 for k in range(1, 5)] + [0, 255]


def _check_size(width: int, height: int, data, block_bytes: int) -> None:
    if width <= 0 or height <= 0 or width % 4 or height % 4:
        raise PaaError("the picture size %d x %d is not a multiple of 4" % (width, height))
    need = width // 4 * (height // 4) * block_bytes
    if len(data) < need:
        raise PaaError("the picture data is %d bytes, %d needed for %d x %d" % (len(data), need, width, height))


def _decode_python(data, width: int, height: int, block_bytes: int) -> bytes:
    out = bytearray(width * height * 4)
    row_bytes = width * 4
    offset = 0
    for brow in range(height // 4):
        for bcol in range(width // 4):
            v0, v1, table = struct.unpack_from("<HHI", data, offset + block_bytes - 8)
            palette = _color_palette(v0, v1, block_bytes == 16)
            alpha = None
            if block_bytes == 16:
                alpha = _alpha_palette(data[offset], data[offset + 1])
                bits = int.from_bytes(data[offset + 2:offset + 8], "little")
            offset += block_bytes
            for row in range(4):
                line = bytearray()
                for col in range(4):
                    pixel = row * 4 + col
                    r, g, b, a = palette[(table >> (2 * pixel)) & 3]
                    if alpha is not None:
                        a = alpha[(bits >> (3 * pixel)) & 7]
                    line += bytes((r, g, b, a))
                start = (brow * 4 + row) * row_bytes + bcol * 16
                out[start:start + 16] = line
    return bytes(out)


def _decode_numpy(data, width: int, height: int, block_bytes: int) -> bytes:
    np = _np
    bh, bw = height // 4, width // 4
    blocks = np.frombuffer(data, dtype=np.uint8, count=bh * bw * block_bytes).reshape(bh, bw, block_bytes)
    color = blocks[:, :, block_bytes - 8:].astype(np.uint32)
    v0 = color[:, :, 0] | (color[:, :, 1] << 8)
    v1 = color[:, :, 2] | (color[:, :, 3] << 8)
    table = color[:, :, 4] | (color[:, :, 5] << 8) | (color[:, :, 6] << 16) | (color[:, :, 7] << 24)

    def expand(v):  # (bh, bw) -> three (bh, bw) channels, 8 bit
        return (((v >> 11) * 255 + 15) // 31, (((v >> 5) & 0x3F) * 255 + 31) // 63, ((v & 0x1F) * 255 + 15) // 31)

    c0, c1 = np.stack(expand(v0), -1), np.stack(expand(v1), -1)
    four = (v0 > v1)[:, :, None]
    c2 = np.where(four, (2 * c0 + c1 + 1) // 3, (c0 + c1 + 1) // 2)
    c3 = np.where(four, (c0 + 2 * c1 + 1) // 3, 0)
    palette = np.stack([c0, c1, c2, c3], axis=2)  # (bh, bw, 4 entries, 3 channels)
    codes = ((table[:, :, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3).astype(np.intp)  # (bh, bw, 16)
    rgb = np.take_along_axis(palette, codes[:, :, :, None], axis=2)  # (bh, bw, 16, 3)
    if block_bytes == 8:
        a3 = np.broadcast_to((v0 > v1)[:, :, None, None] * 255, rgb.shape[:3] + (1,))
        pixel_alpha = np.where(codes[:, :, :, None] == 3, a3, 255)
    else:
        a0, a1 = blocks[:, :, 0].astype(np.int64), blocks[:, :, 1].astype(np.int64)
        k = np.arange(1, 7, dtype=np.int64)
        eight = (a0 > a1)[:, :, None]
        seven = ((7 - k) * a0[:, :, None] + k * a1[:, :, None] + 3) // 7  # (bh, bw, 6)
        k5 = np.arange(1, 5, dtype=np.int64)
        five = ((5 - k5) * a0[:, :, None] + k5 * a1[:, :, None] + 2) // 5  # (bh, bw, 4)
        five = np.concatenate([five, np.zeros_like(five[:, :, :1]), np.full_like(five[:, :, :1], 255)], axis=2)
        mix = np.where(eight, seven, five)
        apal = np.concatenate([a0[:, :, None], a1[:, :, None], mix], axis=2)  # (bh, bw, 8)
        bits = np.zeros(blocks.shape[:2], dtype=np.uint64)
        for i in range(6):
            bits |= blocks[:, :, 2 + i].astype(np.uint64) << np.uint64(8 * i)
        acodes = ((bits[:, :, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)).astype(np.intp)
        pixel_alpha = np.take_along_axis(apal, acodes, axis=2)[:, :, :, None]
    pixels = np.concatenate([rgb, pixel_alpha], axis=3).astype(np.uint8)  # (bh, bw, 16, 4)
    pixels = pixels.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4)  # (bh, row, bw, col, channel)
    return pixels.reshape(height * width * 4).tobytes()


def _decode_dxt(data, width: int, height: int, block_bytes: int, use_numpy: Optional[bool]) -> bytes:
    _check_size(width, height, data, block_bytes)
    if use_numpy is None:
        use_numpy = _np is not None
    if use_numpy and _np is None:
        raise PaaError("numpy is not available")
    return (_decode_numpy if use_numpy else _decode_python)(data, width, height, block_bytes)


def decode_dxt1(data, width: int, height: int, use_numpy: Optional[bool] = None) -> bytes:
    """DXT1 blocks to RGBA8 bytes, top row first. ``use_numpy``: None picks numpy when it is there."""
    return _decode_dxt(data, width, height, 8, use_numpy)


def decode_dxt5(data, width: int, height: int, use_numpy: Optional[bool] = None) -> bytes:
    """DXT5 blocks to RGBA8 bytes, top row first."""
    return _decode_dxt(data, width, height, 16, use_numpy)


# ---------------------------------------------------------------------------------------------------------------------
# The file (data_paa.py PAA_File.read :128, PAA_TAGG.read :40, PAA_MIPMAP.read :59)
# ---------------------------------------------------------------------------------------------------------------------

class PaaTagg:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self.data = data


class PaaMipmap:
    def __init__(self, width: int, height: int, data: bytes, lzo: bool):
        self.width = width
        self.height = height
        self.data = data  # as stored (LZO-compressed when ``lzo``)
        self.lzo = lzo


class PaaFile:
    def __init__(self, type_id: int, taggs: List[PaaTagg], mips: List[PaaMipmap]):
        self.type_id = type_id
        self.taggs = taggs
        self.mips = mips

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type_id, "0x%04x" % self.type_id)

    def largest(self) -> PaaMipmap:
        return max(self.mips, key=lambda mip: mip.width * mip.height)

    def tagg(self, name: str) -> Optional[PaaTagg]:
        return next((t for t in self.taggs if t.name == name), None)


def _truncated(what: str) -> PaaError:
    return PaaError("the file is truncated (it ends inside %s)" % what)


def read_paa(data) -> PaaFile:
    """Parse the header, TAGGs and mipmaps. Raises ``PaaError`` for a short file, an unknown type or a palette."""
    data = bytes(data)
    size = len(data)
    if size < 2:
        raise _truncated("the type field")
    type_id = struct.unpack_from("<H", data, 0)[0]
    if type_id not in TYPE_NAMES:
        raise PaaError("unknown format type 0x%04x (not a .paa file?)" % type_id)
    pos = 2
    taggs: List[PaaTagg] = []
    while data[pos:pos + 4] == b"GGAT":
        if pos + 8 > size:
            raise _truncated("a TAGG header")
        name = data[pos + 4:pos + 8].decode("latin-1")[::-1]
        if pos + 12 > size:
            raise _truncated("a TAGG header")
        length = struct.unpack_from("<I", data, pos + 8)[0]
        pos += 12
        if pos + length > size:
            raise _truncated("the TAGG %s" % name)
        taggs.append(PaaTagg(name, data[pos:pos + length]))
        pos += length
    if pos + 2 > size:
        raise _truncated("the palette field")
    if struct.unpack_from("<H", data, pos)[0] != 0:
        raise PaaError("indexed palettes are not supported")
    pos += 2
    mips: List[PaaMipmap] = []
    while True:
        if pos + 4 > size:
            raise _truncated("a mipmap header")
        width, height = struct.unpack_from("<HH", data, pos)
        pos += 4
        if width == height == 0:
            break
        lzo = bool(width & 0x8000)
        width &= 0x7FFF
        if pos + 3 > size:
            raise _truncated("a mipmap header")
        length = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)
        pos += 3
        if pos + length > size:
            raise _truncated("a %d x %d mipmap" % (width, height))
        mips.append(PaaMipmap(width, height, data[pos:pos + length], lzo))
        pos += length
    if not mips:
        raise PaaError("the file has no mipmaps")
    return PaaFile(type_id, taggs, mips)


def decode_paa_bytes(data, use_numpy: Optional[bool] = None) -> Tuple[int, int, bytes]:
    """``(width, height, rgba)`` of the largest mipmap of a .paa held in memory."""
    paa = read_paa(data)
    if paa.type_id not in (TYPE_DXT1, TYPE_DXT5):
        raise PaaError("the format %s cannot be decoded (only DXT1 and DXT5 can, like Arma 3 Object Builder)"
                       % paa.type_name)
    mip = paa.largest()
    block_bytes = 8 if paa.type_id == TYPE_DXT1 else 16
    raw = mip.data
    if mip.lzo:
        raw = lzo1x_decompress(raw, mip.width * mip.height * block_bytes // 16)
    return mip.width, mip.height, _decode_dxt(raw, mip.width, mip.height, block_bytes, use_numpy)


def decode_paa(path, use_numpy: Optional[bool] = None) -> Tuple[int, int, bytes]:
    """``(width, height, rgba)`` of the largest mipmap of the .paa file at ``path``. Raises ``PaaError`` (also for an
    unreadable file, with the path in the text)."""
    try:
        with open(path, "rb") as file:
            data = file.read()
    except OSError as ex:
        raise PaaError("%s cannot be read (%s)" % (path, ex.strerror or ex)) from None
    try:
        return decode_paa_bytes(data, use_numpy)
    except PaaError as ex:
        raise PaaError("%s: %s" % (path, ex)) from None

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB). New file, not derived from A3OB.
"""A small PNG writer (task #53, D29).

Why PNG: Houdini's viewport and COPs read it with no plug-in, it is lossless, it keeps the alpha channel, and Python's
own ``zlib`` can write it, so the cache needs nothing beyond the standard library. (TGA or EXR would read too, but PNG is
the smallest of the lossless ones for textures and is the one every tool opens.)
"""

import os
import struct
import zlib

__all__ = ["encode_png", "write_png"]


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def encode_png(width: int, height: int, rgba: bytes, level: int = 3) -> bytes:
    """The bytes of an 8-bit RGBA PNG; ``rgba`` holds ``width * height * 4`` bytes, top row first."""
    if len(rgba) != width * height * 4:
        raise ValueError("the pixel data is %d bytes, %d expected for %d x %d" % (len(rgba), width * height * 4,
                                                                                  width, height))
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (none) for every row
        raw += rgba[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + _chunk(b"IEND", b""))


def write_png(path: str, width: int, height: int, rgba: bytes) -> None:
    """Write the PNG through a temporary file and rename it, so a reader never sees a half-written picture."""
    temp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(temp, "wb") as file:
            file.write(encode_png(width, height, rgba))
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise

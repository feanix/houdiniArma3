# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py (P3D_TAGG.read, P3D_LOD.read, P3D_MLOD.read), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - reads from an in-memory buffer with bounds checks; truncation raises P3DEOFError, malformed data P3DError, with
#   the byte offset; counts are checked against the remaining data before anything is allocated;
# - keeps everything A3OB dropped (inactive and unknown TAGGs, face selection bytes, header flags, raw resolution);
# - the face record is always 4 slots of 16 bytes (A3OB read N slots and skipped 16 bytes only for N < 4);
# - checks the payload size of #Mass#, #UVSet# and selections against the LOD instead of trusting it;
# - parses Object Builder's #Selected#, #Lock#, #Hidden# and #Animation# TAGGs (A3OB skipped them).
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Read P3D MLOD files into ``P3DModel``.

``strict=True`` (default) raises ``P3DError`` when a known TAGG (#Mass#, #UVSet#, #Property#, #SharpEdges#, a
selection) has a payload size that does not fit the LOD. ``strict=False`` keeps such a TAGG as an opaque ``RawTagg``
(so the file still round-trips) and records a message in ``P3DModel.warnings``. Framing errors (bad magic, unsupported
versions, truncation, a face with more than 4 sides) always raise.

Object Builder's editor TAGGs (#Selected#, #Lock#, #Hidden#) and #Animation# frames do not matter to the game, so a
payload that does not fit never raises: in both modes it is kept raw with a warning.
"""

from __future__ import annotations

import struct
from array import array

from .. import binary
from ..binary import F32, U32, BinaryEOFError, BinaryReader
from .model import (
    ANIMATION, EDITOR_MASKS, END_OF_FILE, LOD, MAX_FACE_SIDES, P3DEOFError, P3DError, P3DModel, RawTagg, parse_tagg,
)

__all__ = ["read_p3d", "read_p3d_file"]

MLOD_MAGIC = b"MLOD"
MLOD_VERSION = 257
LOD_MAGIC = b"P3DM"
LOD_VERSION = (28, 256)
TAGG_MAGIC = b"TAGG"

_POINT_SIZE = 16   # x, y, z float32 + flags u32
_NORMAL_SIZE = 12  # x, y, z float32
_FACE_FIXED = 72   # sides u32 + 4 slots of (point u32, normal u32, u f32, v f32) + flags u32
_FACE_HEAD = struct.Struct("<I64xI")
_END_OF_FILE = END_OF_FILE.encode("ascii")
#: TAGGs whose malformed payload is kept raw with a warning even in strict mode (editor-only data).
_LENIENT_NAMES = frozenset(EDITOR_MASKS + (ANIMATION,))


def read_p3d(data, strict: bool = True) -> P3DModel:
    """Parse P3D MLOD bytes."""
    reader = BinaryReader(data)
    try:
        return _read_model(reader, strict)
    except BinaryEOFError as ex:
        raise P3DEOFError("P3D data is truncated: %s" % ex) from None


def read_p3d_file(path, strict: bool = True) -> P3DModel:
    with open(path, "rb") as file:
        data = file.read()
    try:
        return read_p3d(data, strict)
    except P3DError as ex:
        raise type(ex)("%s: %s" % (path, ex)) from None


def _read_model(reader: BinaryReader, strict: bool) -> P3DModel:
    magic = bytes(reader.take(4, "MLOD signature"))
    if magic != MLOD_MAGIC:
        raise P3DError("not an MLOD P3D: signature is %r, expected %r" % (magic, MLOD_MAGIC))
    version = reader.u32("MLOD version")
    if version != MLOD_VERSION:
        raise P3DError("unsupported MLOD version %d (only %d is supported)" % (version, MLOD_VERSION))
    count_lods = reader.u32("LOD count")
    # Each LOD needs at least its 28-byte header, "TAGG", an #EndOfFile# record and the resolution.
    if count_lods * 53 > reader.remaining:
        raise P3DEOFError("LOD count %d does not fit in the remaining %d bytes" % (count_lods, reader.remaining))

    model = P3DModel()
    for index in range(count_lods):
        model.lods.append(_read_lod(reader, index, strict, model.warnings))
    model.trailing = bytes(reader.take(reader.remaining, "trailing data"))
    if model.trailing:
        model.warnings.append("%d bytes after the last LOD (kept as P3DModel.trailing)" % len(model.trailing))
    return model


def _read_lod(reader: BinaryReader, index: int, strict: bool, warnings: list) -> LOD:
    start = reader.pos
    where = "LOD %d (offset %d)" % (index, start)

    magic = bytes(reader.take(4, "LOD signature"))
    if magic != LOD_MAGIC:
        raise P3DError("%s: unsupported LOD signature %r (only %r is supported)" % (where, magic, LOD_MAGIC))
    version = reader.u32s(2, "LOD version")
    if version != LOD_VERSION:
        raise P3DError("%s: unsupported LOD version %d.%d (only %d.%d is supported)" % ((where,) + version + LOD_VERSION))
    count_points, count_normals, count_faces, flags = reader.u32s(4, "LOD header")

    lod = LOD()
    lod.flags = flags

    # Points: x, y, z, flags interleaved. Split them with strided array copies (bit-exact, no per-point work).
    block = reader.take(_POINT_SIZE * count_points, "%s points" % where)
    words = binary.array_from_bytes(U32, block)
    lod.point_flags = words[3::4]
    del words[3::4]
    lod.points = binary.array_from_bytes(F32, binary.array_to_bytes(words))

    lod.normals = binary.array_from_bytes(F32, reader.take(_NORMAL_SIZE * count_normals, "%s normals" % where))

    _read_faces(reader, lod, count_faces, where)

    magic = bytes(reader.take(4, "%s TAGG signature" % where))
    if magic != TAGG_MAGIC:
        raise P3DError("%s: invalid TAGG section signature %r at offset %d" % (where, magic, reader.pos - 4))

    lod.taggs = []
    corners = sum(lod.face_sides)
    while True:
        tagg_start = reader.pos
        active = reader.u8("%s TAGG active flag" % where)
        raw_name = reader.asciiz("%s TAGG name" % where)
        length = reader.u32("%s TAGG length" % where)
        if raw_name == _END_OF_FILE:
            if length != 0:
                raise P3DError("%s: #EndOfFile# TAGG at offset %d has length %d, expected 0" % (where, tagg_start, length))
            lod.eof_active = active
            break
        name = binary.decode_string(raw_name)
        payload = reader.take(length, "%s payload of TAGG %r" % (where, name))
        try:
            tagg = parse_tagg(name, active, payload, count_points, count_faces, corners)
        except P3DError as ex:
            message = "%s: TAGG %r at offset %d: %s" % (where, name, tagg_start, ex)
            if strict and name not in _LENIENT_NAMES:
                raise P3DError(message) from None
            warnings.append(message + " (kept as raw data)")
            tagg = RawTagg(name, payload, active, counts=(count_points, count_faces, corners))
        lod.taggs.append(tagg)

    lod.resolution_signature = reader.f32("%s resolution" % where)
    return lod


def _read_faces(reader: BinaryReader, lod: LOD, count_faces: int, where: str) -> None:
    if count_faces * (_FACE_FIXED + 2) > reader.remaining:
        raise P3DEOFError("%s: face count %d does not fit in the remaining %d bytes" % (where, count_faces, reader.remaining))

    data = reader.data
    size = reader.size
    pos = reader.pos
    head = _FACE_HEAD.unpack_from
    find = data.find
    decode = binary.decode_string

    sides = array(U32)
    face_flags = array(U32)
    slots = bytearray()
    textures = []
    materials = []

    for face in range(count_faces):
        if pos + _FACE_FIXED > size:
            raise P3DEOFError("%s: face %d truncated at offset %d" % (where, face, pos))
        count_sides, flag = head(data, pos)
        if count_sides > MAX_FACE_SIDES:
            raise P3DError("%s: face %d at offset %d has %d sides (at most %d)" % (where, face, pos, count_sides, MAX_FACE_SIDES))
        sides.append(count_sides)
        face_flags.append(flag)
        slots += data[pos + 4:pos + 68]
        pos += _FACE_FIXED
        end = find(b"\x00", pos)
        if end < 0:
            raise P3DEOFError("%s: face %d texture path at offset %d is not terminated" % (where, face, pos))
        textures.append(decode(data[pos:end]))
        pos = end + 1
        end = find(b"\x00", pos)
        if end < 0:
            raise P3DEOFError("%s: face %d material path at offset %d is not terminated" % (where, face, pos))
        materials.append(decode(data[pos:end]))
        pos = end + 1
    reader.pos = pos

    # Split the slots (point, normal, u, v) into columns, bit-exact.
    words = binary.array_from_bytes(U32, slots)
    lod.face_sides = sides
    lod.face_flags = face_flags
    lod.face_vertices = words[0::4]
    lod.face_normals = words[1::4]
    del words[0::4]  # -> normal, u, v
    del words[0::3]  # -> u, v
    lod.face_uvs = binary.array_from_bytes(F32, binary.array_to_bytes(words))
    lod.face_textures = textures
    lod.face_materials = materials


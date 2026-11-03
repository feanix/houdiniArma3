# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py (P3D_TAGG.write, P3D_LOD.write, P3D_MLOD.write), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - writes every TAGG of the model in order, whatever its active byte (A3OB silently skipped inactive TAGGs);
# - writes the model's raw fields exactly (flags, unused face slots, raw selection bytes, f32 values bit-exact);
# - validates array and TAGG sizes before writing and raises P3DError naming the LOD and the problem;
# - builds the output in memory and returns bytes; write_p3d_file writes it in one go.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Write ``P3DModel`` to P3D MLOD bytes."""

from __future__ import annotations

import struct
from array import array

from .. import binary
from ..binary import F32, U32
from .model import END_OF_FILE, LOD, P3DError, P3DModel
from .reader import LOD_MAGIC, LOD_VERSION, MLOD_MAGIC, MLOD_VERSION, TAGG_MAGIC

__all__ = ["write_p3d", "write_p3d_file"]

_U32 = struct.Struct("<I")
_EOF_RECORD = binary.encode_asciiz(END_OF_FILE) + _U32.pack(0)


def write_p3d(model: P3DModel) -> bytes:
    """Serialize a model. Raises ``P3DError`` if it has no LODs or a LOD is inconsistent."""
    if not model.lods:
        raise P3DError("cannot write a P3D with no LODs")
    chunks = [MLOD_MAGIC, struct.pack("<II", MLOD_VERSION, len(model.lods))]
    for index, lod in enumerate(model.lods):
        try:
            _write_lod(lod, chunks)
        except (ValueError, TypeError, OverflowError, struct.error) as ex:
            raise P3DError("LOD %d (%s): %s" % (index, _safe_name(lod), ex)) from None
    chunks.append(bytes(model.trailing))
    return b"".join(chunks)


def write_p3d_file(model: P3DModel, path) -> None:
    data = write_p3d(model)
    with open(path, "wb") as file:
        file.write(data)


def _safe_name(lod: LOD) -> str:
    try:
        return lod.name
    except Exception:  # a broken signature must not hide the real error
        return "signature %r" % (lod.resolution_signature,)


def _interleave(columns, count: int, width: int) -> array:
    """Interleave u32 columns (each an array of bit patterns with a given stride) into one array of ``width`` words
    per record. ``columns`` is a list of (source, source_stride, source_offset, destination_offset)."""
    output = array(U32, bytes(4 * width * count))
    for source, stride, offset, destination in columns:
        output[destination::width] = source[offset::stride] if stride != 1 else source
    return output


def _bits(typecode: str, values) -> array:
    """The u32 bit patterns of a float32 or u32 column."""
    values = binary.as_array(typecode, values)
    if typecode == U32:
        return values
    return binary.array_from_bytes(U32, binary.array_to_bytes(values))


def _write_lod(lod: LOD, chunks: list) -> None:
    errors = lod.structure_errors()
    if errors:
        raise P3DError("; ".join(errors))

    count_points = len(lod.point_flags)
    count_faces = len(lod.face_sides)
    chunks.append(LOD_MAGIC)
    chunks.append(struct.pack("<6I", *LOD_VERSION, count_points, len(lod.normals) // 3, count_faces, lod.flags))

    # Points: x, y, z float32 + flags u32.
    point_bits = _bits(F32, lod.points)
    flags = _bits(U32, lod.point_flags)
    chunks.append(binary.array_to_bytes(_interleave(
        [(point_bits, 3, 0, 0), (point_bits, 3, 1, 1), (point_bits, 3, 2, 2), (flags, 1, 0, 3)], count_points, 4)))

    chunks.append(binary.array_to_bytes(binary.as_array(F32, lod.normals)))

    # Faces: the 4 slots of every face as one block, then sliced per face between the variable-length strings.
    uv_bits = _bits(F32, lod.face_uvs)
    slot_block = binary.array_to_bytes(_interleave(
        [(_bits(U32, lod.face_vertices), 1, 0, 0), (_bits(U32, lod.face_normals), 1, 0, 1),
         (uv_bits, 2, 0, 2), (uv_bits, 2, 1, 3)], 4 * count_faces, 4))
    pack = _U32.pack
    encode = binary.encode_asciiz
    sides = lod.face_sides
    face_flags = lod.face_flags
    textures = lod.face_textures
    materials = lod.face_materials
    for face in range(count_faces):
        base = 64 * face
        chunks.append(pack(sides[face]))
        chunks.append(slot_block[base:base + 64])
        chunks.append(pack(face_flags[face]))
        chunks.append(encode(textures[face]))
        chunks.append(encode(materials[face]))

    chunks.append(TAGG_MAGIC)
    for tagg in lod.taggs:
        payload = tagg.payload()
        chunks.append(bytes((tagg.active,)))
        chunks.append(encode(tagg.name))
        chunks.append(pack(len(payload)))
        chunks.append(payload)
    chunks.append(bytes((lod.eof_active,)))
    chunks.append(_EOF_RECORD)
    chunks.append(struct.pack("<f", lod.resolution_signature))

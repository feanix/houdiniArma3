# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The layout record ``a3ob_layout`` (spec 19, D23, task #37). No ``hou`` here.

Object Builder's files hold layout that Houdini geometry cannot: a normal table with repeated entries, old values in the
unused fourth slot of a triangle, sharp pairs in their own order, proxies between the other points and faces, and the
sign of a zero. The importer packs that layout into one small text (``build_record``). The export builds the LOD as
always, and then ``apply_record`` writes the layout back, but only if the positions and topology are still the ones the
record was taken from. Otherwise nothing changes and the caller says so with one info line.

Words used here: *built* is the LOD the exporter made (Houdini order, first-appearance normal table); *file order* is the
order of the source file. ``permutation.Permutation`` converts between them.
"""

import base64
import binascii
import hashlib
import struct
import zlib
from array import array
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from a3ob_core import p3d
from a3ob_core.p3d import model as m1

from .permutation import Permutation, remap_payload

__all__ = ["build_record", "apply_record", "DETAIL"]

#: Name of the detail attribute that holds the record (spec 19.1).
DETAIL = "a3ob_layout"

_MAGIC = b"A3LR"
_VERSION = 1
_ORDER, _NORMALS, _PADS, _SHARP, _ZEROS = 1, 2, 4, 8, 16
_NEG_ZERO = 0x80000000


# -- Small array helpers ----------------------------------------------------------------------------------------------

def _gather(values: array, order: Sequence[int], stride: int = 1) -> array:
    """``values`` taken in the order ``order`` (``stride`` items per element)."""
    out = array(values.typecode)
    if stride == 1:
        out.extend([values[o] for o in order])
    else:
        for o in order:
            out.extend(values[o * stride:(o + 1) * stride])
    return out


def _used(column: array, sides: Sequence[int]) -> array:
    """The used slots of a 4-slot face column, flat in face order."""
    out = array(column.typecode)
    for face, count in enumerate(sides):
        out.extend(column[4 * face:4 * face + count])
    return out


def _bits(values: array) -> array:
    """The same bytes as u32 numbers."""
    out = array("I")
    out.frombytes(values.tobytes())
    return out


def _unsigned_zero(points: array) -> bytes:
    """The bytes of float32 positions with ``-0.0`` turned into ``+0.0``."""
    raw = _bits(points)
    if _NEG_ZERO in raw:
        raw = array("I", [0 if value == _NEG_ZERO else value for value in raw])
    return raw.tobytes()


def digest_of(points: array, sides: array, vertices: array, counts: Tuple[int, int, int]) -> bytes:
    """Spec 19.3: the checksum of positions and topology, all in Houdini order."""
    hasher = hashlib.blake2b(digest_size=16)
    hasher.update(struct.pack("<3I", *counts))
    hasher.update(_unsigned_zero(points))
    hasher.update(array("I", sides).tobytes())
    hasher.update(array("I", vertices).tobytes())
    return hasher.digest()


def _is_permutation(order: Sequence[int], count: int) -> bool:
    return len(order) == count and sorted(order) == list(range(count))


# -- The record --------------------------------------------------------------------------------------------------------

@dataclass
class Record:
    counts: Tuple[int, int, int]
    normal_count: int
    digest: bytes
    point_old: Optional[List[int]] = None
    face_old: Optional[List[int]] = None
    entries: Optional[array] = None
    extras: Optional[List[Tuple[int, bytes]]] = None
    pads: Optional[Tuple[array, array, array, array]] = None
    sharp: Optional[bytes] = None
    zeros: Optional[Set[int]] = None


def _part(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def build_record(lod: m1.LOD, perm: Permutation, sharp: Optional[m1.SharpEdgesTagg] = None) -> Optional[str]:
    """The record text for a LOD as read from the file, or None when this LOD cannot have one.

    ``perm`` is the import permutation (file order to Houdini order); ``sharp`` is the live ``#SharpEdges#`` TAGG."""
    sides = lod.face_sides
    counts = (lod.point_count, lod.face_count, lod.corner_count)
    if any(count not in (3, 4) for count in sides):
        return None
    _, file_vertices = lod.face_corner_indices()
    if perm.is_identity:
        points, order_sides, vertices = lod.points, sides, file_vertices
    else:
        points = _gather(lod.points, perm.point_old, 3)
        order_sides = _gather(sides, perm.face_old)
        point_new = perm.point_new
        vertices = array("I", [point_new[file_vertices[c]] for c in perm.corner_old])
    digest = digest_of(points, order_sides, vertices, counts)
    flags = 0
    body = bytearray()
    if not perm.is_identity:
        flags |= _ORDER
        body += _part(array("I", perm.point_old).tobytes() + array("I", perm.face_old).tobytes())
    entries = lod.face_corner_normal_indices()
    flags |= _NORMALS
    body += _part(_normal_part(lod, entries))
    pad = _pad_part(lod)
    if pad is not None:
        flags |= _PADS
        body += _part(pad)
    if sharp is not None:
        flags |= _SHARP
        body += _part(sharp.payload())
    zeros = [i for i, value in enumerate(_bits(lod.points)) if value == _NEG_ZERO]
    if zeros:
        flags |= _ZEROS
        body += _part(struct.pack("<I", len(zeros)) + array("I", zeros).tobytes())
    head = _MAGIC + struct.pack("<BB4I", _VERSION, flags, *counts, lod.normal_count) + digest
    return base64.b64encode(zlib.compress(head + bytes(body), 6)).decode("ascii")


def _normal_part(lod: m1.LOD, entries: array) -> bytes:
    used = bytearray(lod.normal_count)
    for entry in entries:
        used[entry] = 1
    extras = [e for e in range(lod.normal_count) if not used[e]]
    out = array("I", entries).tobytes() + struct.pack("<I", len(extras))
    for entry in extras:
        out += struct.pack("<I3f", entry, *lod.normals[3 * entry:3 * entry + 3])
    return out


def _triangles(sides: Sequence[int]) -> List[int]:
    return [face for face, count in enumerate(sides) if count == 3]


def _pad_part(lod: m1.LOD) -> Optional[bytes]:
    """The unused fourth slot of every triangle, as four columns; None when all of them are zero."""
    triangles = _triangles(lod.face_sides)
    uvs = _bits(lod.face_uvs)
    columns = (array("I", [lod.face_vertices[4 * f + 3] for f in triangles]),
               array("I", [lod.face_normals[4 * f + 3] for f in triangles]),
               array("I", [uvs[8 * f + 6] for f in triangles]),
               array("I", [uvs[8 * f + 7] for f in triangles]))
    if not any(any(column) for column in columns):
        return None
    return b"".join(column.tobytes() for column in columns)


def _decode(text: str) -> Optional[Record]:
    """The record of ``text``, or None when it is unreadable or has another version."""
    try:
        data = zlib.decompress(base64.b64decode(text.encode("ascii"), validate=True))
        if data[:4] != _MAGIC or data[4] != _VERSION:
            return None
        flags = data[5]
        points, faces, corners, normals = struct.unpack_from("<4I", data, 6)
        record = Record((points, faces, corners), normals, data[22:38])
        position = 38

        def take() -> bytes:
            nonlocal position
            (size,) = struct.unpack_from("<I", data, position)
            chunk = data[position + 4:position + 4 + size]
            if len(chunk) != size:
                raise ValueError("short part")
            position += 4 + size
            return chunk

        if flags & _ORDER:
            chunk = take()
            if len(chunk) != 4 * (points + faces):
                raise ValueError("order size")
            numbers = array("I")
            numbers.frombytes(chunk)
            record.point_old, record.face_old = list(numbers[:points]), list(numbers[points:])
        if flags & _NORMALS:
            chunk = take()
            record.entries = array("I")
            record.entries.frombytes(chunk[:4 * corners])
            (extra_count,) = struct.unpack_from("<I", chunk, 4 * corners)
            if len(chunk) != 4 * corners + 4 + 16 * extra_count:
                raise ValueError("normals size")
            record.extras = []
            for k in range(extra_count):
                base = 4 * corners + 4 + 16 * k
                record.extras.append((struct.unpack_from("<I", chunk, base)[0], chunk[base + 4:base + 16]))
            if len(record.entries) != corners or (corners and max(record.entries) >= normals):
                raise ValueError("normal entry out of range")
        if flags & _PADS:
            chunk = take()
            count = len(chunk) // 16
            if len(chunk) != 16 * count:
                raise ValueError("pads size")
            columns = []
            for k in range(4):
                column = array("I")
                column.frombytes(chunk[4 * count * k:4 * count * (k + 1)])
                columns.append(column)
            record.pads = tuple(columns)
        if flags & _SHARP:
            record.sharp = take()
            if len(record.sharp) % 8:
                raise ValueError("sharp size")
        if flags & _ZEROS:
            chunk = take()
            (count,) = struct.unpack_from("<I", chunk, 0)
            if len(chunk) != 4 + 4 * count:
                raise ValueError("zeros size")
            zeros = array("I")
            zeros.frombytes(chunk[4:])
            record.zeros = set(zeros)
        return record
    except (ValueError, struct.error, zlib.error, binascii.Error, IndexError, UnicodeError):
        return None


# -- Applying it -------------------------------------------------------------------------------------------------------

def apply_record(lod: m1.LOD, text: str) -> Optional[str]:
    """Write the file layout of ``text`` into the built ``lod``. Returns None when it was applied, else the reason it was
    not (the LOD is then unchanged). Spec 19.3 and 19.4."""
    record = _decode(text)
    if record is None:
        return "the record cannot be read"
    counts = (lod.point_count, lod.face_count, lod.corner_count)
    if record.counts != counts:
        return "the point, face or corner count changed"
    if digest_of(lod.points, lod.face_sides, lod.face_corner_indices()[1], counts) != record.digest:
        return "the positions or the topology changed"
    try:
        return _apply(lod, record, counts)
    except (IndexError, ValueError, KeyError, struct.error, p3d.P3DError):
        return "the record does not fit the LOD"


def _apply(lod: m1.LOD, record: Record, counts: Tuple[int, int, int]) -> Optional[str]:
    # Nothing is assigned to ``lod`` before the end, so a failure leaves it as it was.
    built_sides = list(lod.face_sides)
    points, point_flags, sides = lod.points, lod.point_flags, lod.face_sides
    face_vertices, face_normals = lod.face_vertices, lod.face_normals
    face_uvs, face_flags = lod.face_uvs, lod.face_flags
    textures, materials = lod.face_textures, lod.face_materials
    taggs = list(lod.taggs)
    if record.point_old is not None:
        if not (_is_permutation(record.point_old, counts[0]) and _is_permutation(record.face_old, counts[1])):
            return "the recorded order is not a permutation"
        file_sides = [0] * counts[1]
        for new, old in enumerate(record.face_old):
            file_sides[old] = built_sides[new]
        forward = Permutation(record.point_old, record.face_old, file_sides)
        back = Permutation(forward.point_new, forward.face_new, built_sides)  # file order from built order
        to_file = back.point_new
        points = _gather(points, back.point_old, 3)
        point_flags = _gather(point_flags, back.point_old)
        sides = _gather(sides, back.face_old)
        face_vertices = _gather(face_vertices, back.face_old, 4)
        for face, count in enumerate(sides):
            for slot in range(4 * face, 4 * face + count):
                face_vertices[slot] = to_file[face_vertices[slot]]
        face_normals = _gather(face_normals, back.face_old, 4)
        face_uvs = _gather(face_uvs, back.face_old, 8)
        face_flags = _gather(face_flags, back.face_old)
        textures = [textures[o] for o in back.face_old]
        materials = [materials[o] for o in back.face_old]
        taggs = []
        for tagg in lod.taggs:
            if isinstance(tagg, m1.PropertyTagg):
                taggs.append(tagg)
                continue
            payload = remap_payload(tagg, back)
            if payload is None:
                return "a TAGG with point or face numbers cannot follow the order"
            try:
                taggs.append(m1.parse_tagg(tagg.name, tagg.active, payload, *counts))
            except p3d.P3DError:
                taggs.append(m1.RawTagg(tagg.name, payload, tagg.active, counts=counts))
    else:
        points, point_flags = array(points.typecode, points), array(point_flags.typecode, point_flags)
        face_vertices, face_normals = array(face_vertices.typecode, face_vertices), array(face_normals.typecode, face_normals)
        face_uvs = array(face_uvs.typecode, face_uvs)
    normals = lod.normals
    normals_done = _apply_normals(record, lod, sides, face_normals)
    if normals_done is not None:
        normals, face_normals = normals_done
    if record.pads is not None:
        face_uvs = _apply_pads(record, sides, face_vertices, face_normals, face_uvs, normals_done is not None)
    if record.sharp is not None:
        _apply_sharp(record, taggs)
    points = _apply_zeros(record.zeros or set(), points)  # no zeros listed: every zero is +0.0
    lod.points, lod.point_flags, lod.face_sides = points, point_flags, sides
    lod.face_vertices, lod.face_normals, lod.face_uvs = face_vertices, face_normals, face_uvs
    lod.face_flags, lod.face_textures, lod.face_materials = face_flags, textures, materials
    lod.normals, lod.taggs = normals, taggs
    return None


def _apply_normals(record: Record, lod: m1.LOD, sides, face_normals: array):
    """Spec 19.4 step 2: ``(new table, new face normal column)``, or None to keep the built table."""
    if record.entries is None:
        return None
    built = _used(face_normals, sides)
    table = lod.normals.tobytes()
    values = {}
    for entry, index in zip(record.entries, built):
        value = table[12 * index:12 * index + 12]
        if values.setdefault(entry, value) != value:
            return None  # corners that share an entry now have different vectors: the artist edited N
    for entry, value in record.extras:
        if entry in values:
            return None
        values[entry] = value
    if len(values) != record.normal_count:
        return None
    flat = array(lod.normals.typecode)
    flat.frombytes(b"".join(values[e] for e in range(record.normal_count)))
    column = array(face_normals.typecode, bytes(4 * len(face_normals)))
    position = 0
    for face, count in enumerate(sides):
        column[4 * face:4 * face + count] = array(column.typecode, record.entries[position:position + count])
        position += count
    return flat, column


def _apply_pads(record: Record, sides, face_vertices, face_normals, face_uvs: array, with_normals: bool) -> array:
    triangles = _triangles(sides)
    pad_vertex, pad_normal, pad_u, pad_v = record.pads
    if len(triangles) != len(pad_vertex):
        raise ValueError("pad count")
    bits = _bits(face_uvs)
    for k, face in enumerate(triangles):
        face_vertices[4 * face + 3] = pad_vertex[k]
        if with_normals:
            face_normals[4 * face + 3] = pad_normal[k]
        bits[8 * face + 6] = pad_u[k]
        bits[8 * face + 7] = pad_v[k]
    out = array(face_uvs.typecode)
    out.frombytes(bits.tobytes())
    return out


def _normalised_pairs(flat: Sequence[int]) -> Counter:
    return Counter((min(a, b), max(a, b)) for a, b in zip(flat[0::2], flat[1::2]))


def _apply_sharp(record: Record, taggs: list) -> None:
    """Spec 19.4 step 4: the recorded payload replaces the built pairs when they are the same pairs."""
    recorded = array("I")
    recorded.frombytes(record.sharp)
    for tagg in taggs:
        if isinstance(tagg, m1.SharpEdgesTagg) and _normalised_pairs(tagg.edges) == _normalised_pairs(recorded):
            tagg.edges = array(tagg.edges.typecode, recorded)
            return


def _apply_zeros(negative: Set[int], points: array) -> array:
    raw = _bits(points)
    for i, value in enumerate(raw):
        if value & 0x7FFFFFFF == 0:
            raw[i] = _NEG_ZERO if i in negative else 0
    out = array(points.typecode)
    out.frombytes(raw.tobytes())
    return out

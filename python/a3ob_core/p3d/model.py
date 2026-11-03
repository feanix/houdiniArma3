# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py (P3D_TAGG*, P3D_LOD, P3D_MLOD), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - lossless model: raw u32 vertex/face flags, LOD header flags, TAGG active bytes, unknown TAGGs as opaque bytes in
#   their original order, face selection bytes, degenerate sharp edge pairs, the padding slot of triangle faces,
#   bytes after the property terminators and the exact float32 resolution are all kept;
# - geometry is stored as flat ``array`` columns in file coordinates (no Y/Z swap, no normal negation or
#   renormalisation, no UV V flip); conversion belongs to the host application glue;
# - selection weights are kept as the raw bytes and decoded on access;
# - an empty #SharpEdges# TAGG is only written when the model has one;
# - Object Builder's editor TAGGs (#Selected#, #Lock#, #Hidden#) and #Animation# frames are parsed (A3OB dropped
#   them) so that geometry edits resize them; TAGGs that cannot be resized are dropped and recorded;
# - the writer refuses TAGGs that would not read back as written (see LOD.structure_errors);
# - bulk mesh constructor and accessors for host glue (LOD.set_mesh, LOD.face_corner_indices);
# - Blender-only helpers (placeholders, material slots, flag groups, pydata) are not ported.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Data model of P3D MLOD files (MLOD v257, LODs P3DM 28.256).

Format reference: https://community.bistudio.com/wiki/P3D_File_Format_-_MLOD (not without errors).

The raw fields are the source of truth; the semantic methods read and edit them in place, so a model edited through
the API still writes valid P3D and an untouched model writes back byte for byte.

Coordinates are those of the file: (x, y, z) with Y up. UVs are as stored (no V flip). Normals are as stored (Object
Builder stores them negated; A3OB negates them back on import).
"""

from __future__ import annotations

import struct
from array import array
from typing import Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from .. import binary
from ..binary import F32, U32
from . import proxy as proxy_utils
from .lod_types import LodType, decode_signature, encode_signature, format_lod_name

__all__ = [
    "P3DError", "P3DEOFError", "Tagg", "RawTagg", "SharpEdgesTagg", "PropertyTagg", "MassTagg", "UVSetTagg",
    "SelectionTagg", "MaskTagg", "AnimationTagg", "Face", "ProxyRef", "LOD", "P3DModel", "decode_weight",
    "encode_weight", "is_selection_name", "is_typed_tagg_name", "is_count_dependent_tagg_name",
    "tagg_payload_problem", "parse_tagg",
    "SHARP_EDGES", "PROPERTY", "MASS", "UVSET", "SELECTED", "LOCK", "HIDDEN", "ANIMATION", "EDITOR_MASKS",
    "END_OF_FILE", "MAX_FACE_SIDES", "PROPERTY_FIELD_SIZE",
]


class P3DError(ValueError):
    """Malformed, unsupported or inconsistent P3D data."""


class P3DEOFError(P3DError, EOFError):
    """The P3D data ended early."""


SHARP_EDGES = "#SharpEdges#"
PROPERTY = "#Property#"
MASS = "#Mass#"
UVSET = "#UVSet#"
SELECTED = "#Selected#"
LOCK = "#Lock#"
HIDDEN = "#Hidden#"
ANIMATION = "#Animation#"
END_OF_FILE = "#EndOfFile#"

#: Object Builder's editor state TAGGs: one byte per point, then one per face, like a selection (non-zero = selected,
#: locked or hidden). See https://community.bistudio.com/wiki/P3D_File_Format_-_MLOD.
EDITOR_MASKS = (SELECTED, LOCK, HIDDEN)
_FIXED_SIZE_NAMES = frozenset((SHARP_EDGES, PROPERTY))
_COUNT_NAMES = frozenset((MASS, UVSET, ANIMATION) + EDITOR_MASKS)

#: Every face record has room for 4 vertices; triangles leave the last slot unused.
MAX_FACE_SIDES = 4
#: Named property key and value are zero-terminated strings in fixed 64-byte fields.
PROPERTY_FIELD_SIZE = 64


def is_selection_name(name: str) -> bool:
    """A TAGG is a named selection unless its name starts or ends with ``#`` (A3OB's rule)."""
    return not name.startswith("#") and not name.endswith("#")


def is_typed_tagg_name(name: str) -> bool:
    """True if the reader parses a TAGG of this name into a typed class (when its payload fits)."""
    return name in _FIXED_SIZE_NAMES or name in _COUNT_NAMES or is_selection_name(name)


def is_count_dependent_tagg_name(name: str) -> bool:
    """True if the payload size of a TAGG of this name depends on the point, face or corner counts."""
    return name in _COUNT_NAMES or is_selection_name(name)


def decode_weight(value: int) -> float:
    """Selection weight byte to float: 0 = not a member (0.0), 1 = 1.0, otherwise ``(255 - b) / 254``."""
    if value == 0:
        return 0.0
    return (255 - value) / 254


def encode_weight(weight: Optional[float]) -> int:
    """Float weight to selection byte. None or <= 0 means not a member (0); weights >= 1 give 1.

    Small positive weights encode as 255, which is a member with weight 0.0. NaN raises ``ValueError``.
    """
    if weight is None:
        return 0
    if weight != weight:
        raise ValueError("selection weight is NaN")
    if weight <= 0:
        return 0
    if weight >= 1:
        return 1
    return max(1, min(255, int(round(255 - 254 * weight))))


# --------------------------------------------------------------------------------------------------------------------
# TAGGs
# --------------------------------------------------------------------------------------------------------------------

class Tagg:
    """Base class of TAGG records. ``active`` is the raw first byte (1 in files written by Object Builder)."""

    __slots__ = ("name", "active")

    def __init__(self, name: str, active: int = 1):
        self.name = name
        self.active = active

    def payload(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    def __repr__(self) -> str:
        return "<%s %r>" % (type(self).__name__, self.name)


class RawTagg(Tagg):
    """A TAGG kept as opaque bytes: an unknown name, or a known TAGG whose payload did not fit the LOD.

    ``counts`` is set by the reader when it keeps a malformed known TAGG raw (strict=False, or the editor TAGGs):
    the ``(points, faces, corners)`` of the LOD it was read from. Such a TAGG is written back verbatim only while the
    LOD still has those counts (see ``LOD.structure_errors``); geometry edits drop it (see ``LOD.dropped_taggs``).
    """

    __slots__ = ("data", "counts")

    def __init__(self, name: str, data: bytes = b"", active: int = 1,
                 counts: Optional[Tuple[int, int, int]] = None):
        super().__init__(name, active)
        self.data = bytes(data)
        self.counts = counts

    def payload(self) -> bytes:
        return bytes(self.data)


class SharpEdgesTagg(Tagg):
    """#SharpEdges#: pairs of point indices, stored flat (a0, b0, a1, b1, ...). Degenerate pairs are kept."""

    __slots__ = ("edges",)

    def __init__(self, edges: Iterable[int] = (), active: int = 1):
        super().__init__(SHARP_EDGES, active)
        self.edges = array(U32, edges)

    @classmethod
    def from_pairs(cls, pairs: Iterable[Sequence[int]], active: int = 1) -> "SharpEdgesTagg":
        return cls((index for pair in pairs for index in pair), active)

    def pairs(self) -> List[Tuple[int, int]]:
        edges = self.edges
        return list(zip(edges[0::2], edges[1::2]))

    def payload(self) -> bytes:
        edges = binary.as_array(U32, self.edges)
        if len(edges) % 2:
            raise P3DError("#SharpEdges# has an odd number of indices (%d)" % len(edges))
        return binary.array_to_bytes(edges)


def _property_field(text: str, what: str) -> bytes:
    raw = binary.encode_asciiz(text)
    if len(raw) > PROPERTY_FIELD_SIZE:
        raise P3DError("named property %s %r is longer than %d bytes" % (what, text, PROPERTY_FIELD_SIZE - 1))
    return raw.ljust(PROPERTY_FIELD_SIZE, b"\x00")


class PropertyTagg(Tagg):
    """#Property#: a named property (key, value). Bytes after the terminators are kept until key or value change."""

    __slots__ = ("key", "value", "_source")

    def __init__(self, key: str = "", value: str = "", active: int = 1):
        super().__init__(PROPERTY, active)
        self.key = key
        self.value = value
        self._source = None

    @classmethod
    def from_payload(cls, raw: bytes, active: int = 1) -> "PropertyTagg":
        raw = bytes(raw)
        if len(raw) != 2 * PROPERTY_FIELD_SIZE:
            raise P3DError("#Property# payload must be %d bytes, got %d" % (2 * PROPERTY_FIELD_SIZE, len(raw)))
        key_end = raw.find(b"\x00", 0, PROPERTY_FIELD_SIZE)
        value_end = raw.find(b"\x00", PROPERTY_FIELD_SIZE)
        if key_end < 0 or value_end < 0:
            raise P3DError("#Property# field has no NUL terminator")
        output = cls(binary.decode_string(raw[:key_end]),
                     binary.decode_string(raw[PROPERTY_FIELD_SIZE:value_end]), active)
        output._source = (output.key, output.value, raw)
        return output

    def payload(self) -> bytes:
        source = self._source
        if source is not None and source[0] == self.key and source[1] == self.value:
            return source[2]
        return _property_field(self.key, "key") + _property_field(self.value, "value")


class MassTagg(Tagg):
    """#Mass#: one float32 per point."""

    __slots__ = ("masses",)

    def __init__(self, masses: Iterable[float] = (), active: int = 1):
        super().__init__(MASS, active)
        self.masses = array(F32, masses)

    def payload(self) -> bytes:
        return binary.array_to_bytes(binary.as_array(F32, self.masses))


class UVSetTagg(Tagg):
    """#UVSet#: a UV set id and one (u, v) per face corner, in face order, stored flat."""

    __slots__ = ("set_id", "uvs")

    def __init__(self, set_id: int = 0, uvs: Iterable[float] = (), active: int = 1):
        super().__init__(UVSET, active)
        self.set_id = set_id
        self.uvs = array(F32, uvs)

    def pairs(self) -> List[Tuple[float, float]]:
        uvs = self.uvs
        return list(zip(uvs[0::2], uvs[1::2]))

    def payload(self) -> bytes:
        return struct.pack("<I", self.set_id) + binary.array_to_bytes(binary.as_array(F32, self.uvs))


class SelectionTagg(Tagg):
    """A named selection: one weight byte per point, then one per face (see ``decode_weight``)."""

    __slots__ = ("vertex_weights", "face_weights")

    def __init__(self, name: str, vertex_weights: Union[bytes, bytearray, Iterable[int]] = b"",
                 face_weights: Union[bytes, bytearray, Iterable[int]] = b"", active: int = 1):
        super().__init__(name, active)
        self.vertex_weights = bytearray(vertex_weights)
        self.face_weights = bytearray(face_weights)

    # Points
    def weight(self, point: int) -> Optional[float]:
        """Weight of a point, or None if the point is not a member."""
        value = self.vertex_weights[point]
        return None if value == 0 else decode_weight(value)

    def set_weight(self, point: int, weight: Optional[float]) -> None:
        """Set the weight of a point; None or 0 removes it from the selection. NaN raises ``ValueError``.

        The face bytes are not updated; call ``LOD.derive_face_weights`` (or ``derive_all_face_weights``)."""
        self.vertex_weights[point] = encode_weight(weight)

    def members(self) -> List[int]:
        return [i for i, value in enumerate(self.vertex_weights) if value]

    def weights(self) -> Dict[int, float]:
        """``{point: weight}`` for the member points."""
        return {i: decode_weight(value) for i, value in enumerate(self.vertex_weights) if value}

    def weight_list(self) -> List[float]:
        """One float per point, 0.0 for non-members."""
        table = [decode_weight(value) for value in range(256)]
        return [table[value] for value in self.vertex_weights]

    # Faces
    def face_weight(self, face: int) -> Optional[float]:
        value = self.face_weights[face]
        return None if value == 0 else decode_weight(value)

    def set_face_weight(self, face: int, weight: Optional[float]) -> None:
        self.face_weights[face] = encode_weight(weight)

    def face_members(self) -> List[int]:
        return [i for i, value in enumerate(self.face_weights) if value]

    # Proxies
    @property
    def proxy(self) -> Optional[Tuple[str, int]]:
        """``(path, index)`` if this is a proxy selection, else None."""
        return proxy_utils.parse_proxy_name(self.name)

    def payload(self) -> bytes:
        return bytes(self.vertex_weights) + bytes(self.face_weights)


class MaskTagg(Tagg):
    """#Selected#, #Lock# or #Hidden#: Object Builder editor state, one byte per point then one per face (non-zero =
    selected, locked or hidden). Not used by the game; kept so files saved by Object Builder survive edits."""

    __slots__ = ("vertex_bytes", "face_bytes")

    def __init__(self, name: str, vertex_bytes: Union[bytes, bytearray, Iterable[int]] = b"",
                 face_bytes: Union[bytes, bytearray, Iterable[int]] = b"", active: int = 1):
        super().__init__(name, active)
        self.vertex_bytes = bytearray(vertex_bytes)
        self.face_bytes = bytearray(face_bytes)

    def points(self) -> List[int]:
        return [i for i, value in enumerate(self.vertex_bytes) if value]

    def faces(self) -> List[int]:
        return [i for i, value in enumerate(self.face_bytes) if value]

    def payload(self) -> bytes:
        return bytes(self.vertex_bytes) + bytes(self.face_bytes)


class AnimationTagg(Tagg):
    """#Animation#: one frame of Object Builder's (obsolete) vertex animation: a float32 frame time, then x, y, z
    float32 for every point, stored flat. A LOD has one such TAGG per frame."""

    __slots__ = ("_time", "positions")

    def __init__(self, frame_time: float = 0.0, positions: Iterable[float] = (), active: int = 1):
        super().__init__(ANIMATION, active)
        self._time = array(F32, (frame_time,))  # an array keeps the exact float32 bits
        self.positions = array(F32, positions)

    @property
    def frame_time(self) -> float:
        return self._time[0]

    @frame_time.setter
    def frame_time(self, value: float) -> None:
        self._time = array(F32, (value,))

    def payload(self) -> bytes:
        return binary.array_to_bytes(self._time) + binary.array_to_bytes(binary.as_array(F32, self.positions))


def tagg_payload_problem(name: str, payload, count_points: int, count_faces: int, corners: int) -> Optional[str]:
    """Why a TAGG payload cannot be parsed as the typed TAGG its name stands for, or None if it can (or the name is
    unknown, in which case any payload is fine)."""
    length = len(payload)
    if name == SHARP_EDGES:
        if length % 8:
            return "payload length %d is not a multiple of 8" % length
    elif name == PROPERTY:
        if length != 2 * PROPERTY_FIELD_SIZE:
            return "payload length %d, expected %d" % (length, 2 * PROPERTY_FIELD_SIZE)
        raw = bytes(payload)
        if raw.find(b"\x00", 0, PROPERTY_FIELD_SIZE) < 0 or raw.find(b"\x00", PROPERTY_FIELD_SIZE) < 0:
            return "#Property# field has no NUL terminator"
    elif name == MASS:
        if length != 4 * count_points:
            return "payload length %d, expected %d (4 bytes x %d points)" % (length, 4 * count_points, count_points)
    elif name == UVSET:
        if length != 4 + 8 * corners:
            return "payload length %d, expected %d (4 + 8 bytes x %d face corners)" % (length, 4 + 8 * corners, corners)
    elif name == ANIMATION:
        if length != 4 + 12 * count_points:
            return "payload length %d, expected %d (4 + 12 bytes x %d points)" % (
                length, 4 + 12 * count_points, count_points)
    elif name in EDITOR_MASKS or is_selection_name(name):
        if length != count_points + count_faces:
            return "payload length %d, expected %d (%d points + %d faces)" % (
                length, count_points + count_faces, count_points, count_faces)
    return None


def parse_tagg(name: str, active: int, payload, count_points: int, count_faces: int, corners: int) -> Tagg:
    """Build the TAGG object for a name and payload. Unknown names give a ``RawTagg``; a known name whose payload
    does not fit raises ``P3DError``."""
    problem = tagg_payload_problem(name, payload, count_points, count_faces, corners)
    if problem is not None:
        raise P3DError(problem)
    if name == SHARP_EDGES:
        tagg = SharpEdgesTagg(active=active)
        tagg.edges = binary.array_from_bytes(U32, payload)
        return tagg
    if name == PROPERTY:
        return PropertyTagg.from_payload(payload, active)
    if name == MASS:
        tagg = MassTagg(active=active)
        tagg.masses = binary.array_from_bytes(F32, payload)
        return tagg
    if name == UVSET:
        tagg = UVSetTagg(struct.unpack_from("<I", payload)[0], active=active)
        tagg.uvs = binary.array_from_bytes(F32, payload[4:])
        return tagg
    if name == ANIMATION:
        tagg = AnimationTagg(active=active)
        tagg._time = binary.array_from_bytes(F32, payload[:4])
        tagg.positions = binary.array_from_bytes(F32, payload[4:])
        return tagg
    if name in EDITOR_MASKS:
        return MaskTagg(name, payload[:count_points], payload[count_points:], active)
    if is_selection_name(name):
        return SelectionTagg(name, payload[:count_points], payload[count_points:], active)
    return RawTagg(name, payload, active)


#: The name each fixed-name TAGG class must carry.
_FIXED_NAMES = {SharpEdgesTagg: SHARP_EDGES, PropertyTagg: PROPERTY, MassTagg: MASS, UVSetTagg: UVSET,
                AnimationTagg: ANIMATION}


# Order in which A3OB's exporter writes TAGGs; used to place TAGGs created through the API.
def _tagg_rank(tagg: Tagg) -> int:
    if isinstance(tagg, SharpEdgesTagg):
        return 0
    if isinstance(tagg, UVSetTagg):
        return 1
    if isinstance(tagg, PropertyTagg):
        return 2
    if isinstance(tagg, MassTagg):
        return 3
    if isinstance(tagg, SelectionTagg):
        return 4
    return 5


# --------------------------------------------------------------------------------------------------------------------
# LOD
# --------------------------------------------------------------------------------------------------------------------

def _vector3(values: Sequence[float]) -> array:
    vector = array(F32, values)
    if len(vector) != 3:
        raise ValueError("expected 3 coordinates, got %d" % len(vector))
    return vector


def _flat3(values, what: str) -> array:
    """Flat float32 x, y, z values from a flat sequence or a sequence of triples."""
    if isinstance(values, array):
        flat = array(F32, values)
    else:
        values = list(values)
        if values and not isinstance(values[0], (int, float)):
            flat = array(F32)
            for index, triple in enumerate(values):
                triple = tuple(triple)
                if len(triple) != 3:
                    raise ValueError("%s[%d] has %d coordinates, expected 3" % (what, index, len(triple)))
                flat.extend(triple)
        else:
            flat = array(F32, values)
    if len(flat) % 3:
        raise ValueError("%s has %d values, not a multiple of 3" % (what, len(flat)))
    return flat


class Face(NamedTuple):
    """Snapshot of one face (only its used corners)."""

    vertices: Tuple[int, ...]
    normals: Tuple[int, ...]
    uvs: Tuple[Tuple[float, float], ...]
    flags: int
    texture: str
    material: str


class ProxyRef(NamedTuple):
    path: str
    index: int
    selection: SelectionTagg


class LOD:
    """One P3DM LOD.

    Raw columns (the source of truth):

    - ``flags``: LOD header flags (u32, normally 0)
    - ``points``: float32 x, y, z per point, flat; ``point_flags``: u32 per point
    - ``normals``: float32 x, y, z per normal, flat
    - faces: ``face_sides`` (corner count), ``face_vertices``/``face_normals`` (4 u32 slots per face, unused slots
      kept as read), ``face_uvs`` (4 x (u, v) float32 per face), ``face_flags`` (u32), ``face_textures`` and
      ``face_materials`` (str)
    - ``taggs``: TAGG records in file order (without #EndOfFile#); ``eof_active``: active byte of #EndOfFile#
    - ``resolution_signature``: the LOD signature float (always a float32 value)

    ``dropped_taggs`` (not written) lists the TAGGs that geometry edits removed because they could not follow the
    new point/face counts: malformed raw TAGGs on ``add_point``/``add_face``, every count-dependent TAGG on
    ``set_mesh``. Host glue can report them.

    Face selection bytes: ``add_selection`` and ``add_face`` derive them (a face is a member when all its vertices
    are); ``SelectionTagg.set_weight`` and direct column edits do not. Call ``derive_all_face_weights`` after editing
    point weights if the face bytes should follow.
    """

    def __init__(self, lod_type: LodType = LodType.VISUAL, resolution: float = 0.0):
        self.flags = 0
        self.points = array(F32)
        self.point_flags = array(U32)
        self.normals = array(F32)
        self.face_sides = array(U32)
        self.face_vertices = array(U32)
        self.face_normals = array(U32)
        self.face_uvs = array(F32)
        self.face_flags = array(U32)
        self.face_textures: List[str] = []
        self.face_materials: List[str] = []
        self.taggs: List[Tagg] = []
        self.eof_active = 1
        self.resolution_signature = encode_signature(lod_type, resolution)
        self.dropped_taggs: List[Tagg] = []

    def __repr__(self) -> str:
        return "<LOD %s: %d points, %d faces, %d taggs>" % (
            self.name, self.point_count, self.face_count, len(self.taggs))

    # -- Type and resolution -----------------------------------------------------------------------------------------

    @property
    def lod_type(self) -> LodType:
        return decode_signature(self.resolution_signature)[0]

    @property
    def resolution(self) -> float:
        return decode_signature(self.resolution_signature)[1]

    def set_lod_type(self, lod_type: LodType, resolution: float = 0.0) -> None:
        self.resolution_signature = encode_signature(lod_type, resolution)

    @property
    def name(self) -> str:
        return format_lod_name(*decode_signature(self.resolution_signature))

    # -- Counts ------------------------------------------------------------------------------------------------------

    @property
    def point_count(self) -> int:
        return len(self.point_flags)

    @property
    def normal_count(self) -> int:
        return len(self.normals) // 3

    @property
    def face_count(self) -> int:
        return len(self.face_sides)

    @property
    def corner_count(self) -> int:
        """Total number of used face corners (the length of a UV set)."""
        return sum(self.face_sides)

    # -- Points, normals, faces --------------------------------------------------------------------------------------

    def point(self, index: int) -> Tuple[float, float, float]:
        base = 3 * index
        return tuple(self.points[base:base + 3])

    def set_point(self, index: int, position: Sequence[float]) -> None:
        if not 0 <= index < self.point_count:
            raise IndexError("point index %d out of range" % index)
        base = 3 * index
        self.points[base:base + 3] = _vector3(position)

    def add_point(self, position: Sequence[float], flags: int = 0) -> int:
        """Append a point. Count-dependent TAGGs follow: selections and the editor masks get a 0 byte, #Mass# a
        mass of 0, #Animation# frames the point's position. Raw TAGGs whose name has a known layout are parsed first
        (so they can grow) or, if malformed, moved to ``dropped_taggs``. Returns the point index."""
        vector = _vector3(position)
        index = self.point_count
        self.points.extend(vector)
        self.point_flags.append(flags)
        pending_raw = False
        for tagg in self.taggs:
            if isinstance(tagg, SelectionTagg):
                tagg.vertex_weights.append(0)
            elif isinstance(tagg, MassTagg):
                tagg.masses.append(0.0)
            elif isinstance(tagg, MaskTagg):
                tagg.vertex_bytes.append(0)
            elif isinstance(tagg, AnimationTagg):
                tagg.positions.extend(vector)
            elif type(tagg) is RawTagg and is_count_dependent_tagg_name(tagg.name):
                pending_raw = True
        if pending_raw:
            self._settle_raw_taggs((index, self.face_count, self.corner_count),
                                   lambda tagg: self._grow_for_point(tagg, vector))
        return index

    @staticmethod
    def _grow_for_point(tagg: Tagg, vector: array) -> None:
        if isinstance(tagg, SelectionTagg):
            tagg.vertex_weights.append(0)
        elif isinstance(tagg, MassTagg):
            tagg.masses.append(0.0)
        elif isinstance(tagg, MaskTagg):
            tagg.vertex_bytes.append(0)
        elif isinstance(tagg, AnimationTagg):
            tagg.positions.extend(vector)

    def _settle_raw_taggs(self, old_counts: Tuple[int, int, int], grow) -> None:
        """After a geometry edit: parse each raw TAGG whose size depends on the counts with the counts from before
        the edit, then ``grow`` it like the typed TAGGs; a raw TAGG that does not parse cannot be resized and is moved
        to ``dropped_taggs``."""
        settled = []
        for tagg in self.taggs:
            if type(tagg) is RawTagg and is_count_dependent_tagg_name(tagg.name):
                try:
                    tagg = parse_tagg(tagg.name, tagg.active, tagg.data, *old_counts)
                except P3DError:
                    self.dropped_taggs.append(tagg)
                    continue
                grow(tagg)
            settled.append(tagg)
        self.taggs = settled

    def normal(self, index: int) -> Tuple[float, float, float]:
        base = 3 * index
        return tuple(self.normals[base:base + 3])

    def add_normal(self, normal: Sequence[float]) -> int:
        index = self.normal_count
        self.normals.extend(_vector3(normal))
        return index

    def face(self, index: int) -> Face:
        sides = self.face_sides[index]
        base = 4 * index
        uvs = self.face_uvs[2 * base:2 * base + 2 * sides]
        return Face(
            tuple(self.face_vertices[base:base + sides]),
            tuple(self.face_normals[base:base + sides]),
            tuple(zip(uvs[0::2], uvs[1::2])),
            self.face_flags[index],
            self.face_textures[index],
            self.face_materials[index],
        )

    def faces(self) -> Iterator[Face]:
        for index in range(self.face_count):
            yield self.face(index)

    def add_face(self, vertices: Sequence[int], normals: Optional[Sequence[int]] = None,
                 uvs: Optional[Sequence[Sequence[float]]] = None, flags: int = 0, texture: str = "",
                 material: str = "") -> int:
        """Append a triangle or quad. Returns the face index.

        Count-dependent TAGGs follow: each selection gets a face byte derived from the vertices (1 when all of them
        are members, else 0, A3OB's export rule), the editor masks get a 0 byte and UV set TAGGs get the face's
        corners (``uvs`` for set 0, zeros for the others). Raw TAGGs are settled as in ``add_point``."""
        vertices = list(vertices)
        sides = len(vertices)
        if not 3 <= sides <= MAX_FACE_SIDES:
            raise ValueError("faces must have 3 or 4 vertices, got %d" % sides)
        normals = list(normals) if normals is not None else [0] * sides
        uv_pairs = [tuple(uv) for uv in uvs] if uvs is not None else [(0.0, 0.0)] * sides
        if len(normals) != sides or len(uv_pairs) != sides:
            raise ValueError("a face needs as many normals and UVs as vertices")
        flat_uvs = [value for uv in uv_pairs for value in uv]
        padding = MAX_FACE_SIDES - sides

        index = self.face_count
        self.face_sides.append(sides)
        self.face_vertices.extend(vertices + [0] * padding)
        self.face_normals.extend(normals + [0] * padding)
        self.face_uvs.extend(array(F32, flat_uvs + [0.0] * (2 * padding)))
        self.face_flags.append(flags)
        self.face_textures.append(texture)
        self.face_materials.append(material)
        pending_raw = False
        grow = self._face_grower(vertices, flat_uvs)
        # Selections inlined (the hot case); see _face_grower for the same rule.
        count_points = self.point_count
        in_range = all(0 <= v < count_points for v in vertices)
        highest = max(vertices)
        v0, v1, v2 = vertices[0], vertices[1], vertices[2]
        v3 = vertices[3] if sides == 4 else v0
        for tagg in self.taggs:
            if isinstance(tagg, SelectionTagg):
                weights = tagg.vertex_weights
                member = (in_range and len(weights) > highest
                          and weights[v0] and weights[v1] and weights[v2] and weights[v3])
                tagg.face_weights.append(1 if member else 0)
            elif type(tagg) is RawTagg:
                if is_count_dependent_tagg_name(tagg.name):
                    pending_raw = True
            else:
                grow(tagg)
        if pending_raw:
            self._settle_raw_taggs((self.point_count, index, self.corner_count - sides), grow)
        return index

    def _face_grower(self, vertices: List[int], flat_uvs: List[float]):
        """The function that appends one face to a typed TAGG (hot loop: no generator per selection)."""
        sides = len(vertices)
        count_points = self.point_count
        in_range = all(0 <= v < count_points for v in vertices)
        highest = max(vertices)
        v0, v1, v2 = vertices[0], vertices[1], vertices[2]
        v3 = vertices[3] if sides == 4 else v0
        uvs_set0 = array(F32, flat_uvs)
        uvs_other = array(F32, bytes(8 * sides))

        def grow(tagg: Tagg) -> None:
            if isinstance(tagg, SelectionTagg):
                weights = tagg.vertex_weights
                member = (in_range and len(weights) > highest
                          and weights[v0] and weights[v1] and weights[v2] and weights[v3])
                tagg.face_weights.append(1 if member else 0)
            elif isinstance(tagg, UVSetTagg):
                tagg.uvs.extend(uvs_set0 if tagg.set_id == 0 else uvs_other)
            elif isinstance(tagg, MaskTagg):
                tagg.face_bytes.append(0)

        return grow

    def set_mesh(self, points: Sequence, polygons: Sequence[Sequence[int]], normals: Optional[Sequence] = None,
                 normal_indices: Optional[Sequence[int]] = None, uvs: Optional[Sequence[float]] = None,
                 point_flags: Optional[Sequence[int]] = None, face_flags: Optional[Sequence[int]] = None,
                 textures: Optional[Sequence[str]] = None, materials: Optional[Sequence[str]] = None) -> None:
        """Replace the whole mesh in one pass from variable-length polygons (the shape of ``hou.Geometry`` data).

        - ``points``: flat x, y, z values or (x, y, z) triples; ``point_flags``: one u32 per point (default 0)
        - ``polygons``: point indices per face, 3 or 4 each, in file corner order
        - ``normals``: the normal table, flat or triples (default empty)
        - ``normal_indices``: one normal index per corner, flat in face order. Default: corner ``i`` uses normal
          ``i`` when there is one normal per corner, the corner's point index when there is one normal per point,
          0 when there are no normals; any other normal count needs explicit indices
        - ``uvs``: flat (u, v) per corner in face order, stored in the face records (default zeros). This does not
          create a #UVSet# TAGG; call ``set_uv_set`` for that
        - ``face_flags``, ``textures``, ``materials``: one per face (default 0, "", "")

        The unused 4th slot of a triangle is zero. Every TAGG that refers to points, faces or corners (selections,
        #Mass#, #UVSet#, #SharpEdges#, the editor masks, #Animation# and raw TAGGs with those names) is removed and
        appended to ``dropped_taggs``; properties and unknown TAGGs are kept. Raises ``ValueError`` (leaving the LOD
        unchanged) when the inputs are inconsistent; point indices are not range-checked (see ``check``).
        """
        try:
            self._set_mesh(points, polygons, normals, normal_indices, uvs, point_flags, face_flags, textures,
                           materials)
        except (OverflowError, TypeError) as ex:  # negative or non-integer indices and flags
            raise ValueError("invalid mesh data: %s" % ex) from None

    def _set_mesh(self, points, polygons, normals, normal_indices, uvs, point_flags, face_flags, textures,
                  materials) -> None:
        flat_points = _flat3(points, "points")
        count_points = len(flat_points) // 3
        flat_normals = _flat3(normals if normals is not None else (), "normals")
        count_normals = len(flat_normals) // 3

        count_faces = len(polygons)
        sides = array(U32)
        vertex_slots = array(U32)
        corner_points = []
        for index, polygon in enumerate(polygons):
            polygon = list(polygon)
            count = len(polygon)
            if not 3 <= count <= MAX_FACE_SIDES:
                raise ValueError("polygon %d has %d vertices; faces must have 3 or 4" % (index, count))
            try:
                vertex_slots.extend(polygon)
            except (OverflowError, TypeError) as ex:
                raise ValueError("polygon %d has an invalid point index: %s" % (index, ex)) from None
            sides.append(count)
            corner_points.extend(polygon)
            if count == 3:
                vertex_slots.append(0)
        corners = len(corner_points)

        if normal_indices is None:
            if count_normals == 0:
                corner_normals = [0] * corners
            elif count_normals == corners:
                corner_normals = range(corners)
            elif count_normals == count_points:
                corner_normals = corner_points
            else:
                raise ValueError("%d normals match neither the %d corners nor the %d points; pass normal_indices"
                                 % (count_normals, corners, count_points))
        else:
            corner_normals = normal_indices
            if len(corner_normals) != corners:
                raise ValueError("need one normal index per corner (%d), got %d" % (corners, len(corner_normals)))
        corner_uvs = array(F32, uvs) if uvs is not None else array(F32, bytes(8 * corners))
        if len(corner_uvs) != 2 * corners:
            raise ValueError("need 2 UV values per corner (%d), got %d" % (2 * corners, len(corner_uvs)))

        normal_slots = array(U32)
        uv_slots = array(F32)
        position = 0
        for count in sides:
            normal_slots.extend(corner_normals[position:position + count])
            uv_slots.extend(corner_uvs[2 * position:2 * (position + count)])
            if count == 3:
                normal_slots.append(0)
                uv_slots.extend((0.0, 0.0))
            position += count

        new_point_flags = array(U32, point_flags) if point_flags is not None else array(U32, bytes(4 * count_points))
        new_face_flags = array(U32, face_flags) if face_flags is not None else array(U32, bytes(4 * count_faces))
        new_textures = list(textures) if textures is not None else [""] * count_faces
        new_materials = list(materials) if materials is not None else [""] * count_faces
        if len(new_point_flags) != count_points:
            raise ValueError("need one point flag per point (%d), got %d" % (count_points, len(new_point_flags)))
        for what, values in (("face flag", new_face_flags), ("texture", new_textures), ("material", new_materials)):
            if len(values) != count_faces:
                raise ValueError("need one %s per face (%d), got %d" % (what, count_faces, len(values)))

        self.points = flat_points
        self.point_flags = new_point_flags
        self.normals = flat_normals
        self.face_sides = sides
        self.face_vertices = vertex_slots
        self.face_normals = normal_slots
        self.face_uvs = uv_slots
        self.face_flags = new_face_flags
        self.face_textures = new_textures
        self.face_materials = new_materials
        kept = []
        for tagg in self.taggs:
            if tagg.name == SHARP_EDGES or is_count_dependent_tagg_name(tagg.name):
                self.dropped_taggs.append(tagg)
            else:
                kept.append(tagg)
        self.taggs = kept

    def _used_slots(self, column: array) -> array:
        """The used corner slots of a 4-slot face column, flat in face order."""
        sides = self.face_sides
        count = len(sides)
        if sides.count(4) == count:
            return array(column.typecode, column)
        output = array(column.typecode, column)
        if sides.count(3) == count:
            del output[3::4]
            return output
        output = array(column.typecode)
        for index, side_count in enumerate(sides):
            base = 4 * index
            output.extend(column[base:base + side_count])
        return output

    def face_corner_indices(self) -> Tuple[array, array]:
        """``(sides, point indices)``: the corner count of each face and the used point indices, flat in face order
        (the input of ``hou.Geometry.createPolygons`` once split by ``sides``). Unused triangle slots are left out."""
        return array(U32, self.face_sides), self._used_slots(self.face_vertices)

    def face_corner_normal_indices(self) -> array:
        """The normal index of every used corner, flat in face order (parallel to ``face_corner_indices``)."""
        return self._used_slots(self.face_normals)

    def corner_uvs(self) -> array:
        """UVs embedded in the face records, one (u, v) per used corner, flat (UV set 0 as the faces store it)."""
        output = array(F32)
        uvs = self.face_uvs
        for index, sides in enumerate(self.face_sides):
            base = 8 * index
            output.extend(uvs[base:base + 2 * sides])
        return output

    # -- TAGG helpers ------------------------------------------------------------------------------------------------

    def taggs_of(self, kind: type) -> list:
        return [tagg for tagg in self.taggs if isinstance(tagg, kind)]

    def _first(self, kind: type):
        for tagg in self.taggs:
            if isinstance(tagg, kind):
                return tagg
        return None

    def insert_tagg(self, tagg: Tagg) -> Tagg:
        """Insert a TAGG after the last TAGG of the same or an earlier kind in A3OB's export order (sharp edges,
        UV sets, properties, mass, selections, others)."""
        rank = _tagg_rank(tagg)
        position = 0
        for index, other in enumerate(self.taggs):
            if _tagg_rank(other) <= rank:
                position = index + 1
        self.taggs.insert(position, tagg)
        return tagg

    # -- Named selections --------------------------------------------------------------------------------------------

    def selections(self) -> List[SelectionTagg]:
        return self.taggs_of(SelectionTagg)

    def selection_names(self) -> List[str]:
        return [tagg.name for tagg in self.taggs if isinstance(tagg, SelectionTagg)]

    def selection(self, name: str) -> Optional[SelectionTagg]:
        """Find a selection by name: exact match first, then case-insensitive."""
        folded = None
        lower = name.casefold()
        for tagg in self.taggs:
            if isinstance(tagg, SelectionTagg):
                if tagg.name == name:
                    return tagg
                if folded is None and tagg.name.casefold() == lower:
                    folded = tagg
        return folded

    def add_selection(self, name: str, weights: Optional[Mapping[int, float]] = None,
                      derive_faces: bool = True) -> SelectionTagg:
        """Create a selection from ``{point: weight}``. With ``derive_faces``, a face is a member when all its
        vertices are (A3OB's export rule)."""
        if not name:
            raise ValueError("selection names cannot be empty")
        if not is_selection_name(name):
            raise ValueError("selection names cannot start or end with '#': %r" % name)
        if self.selection(name) is not None:
            raise ValueError("selection %r already exists" % name)
        tagg = SelectionTagg(name, bytearray(self.point_count), bytearray(self.face_count))
        for point, weight in (weights or {}).items():
            tagg.set_weight(point, weight)
        if derive_faces:
            self.derive_face_weights(tagg)
        return self.insert_tagg(tagg)

    def remove_selection(self, name: str) -> bool:
        tagg = self.selection(name)
        if tagg is None:
            return False
        self.taggs.remove(tagg)
        return True

    def derive_face_weights(self, selection: SelectionTagg) -> None:
        """Set each face byte to 1 if all the face's vertices are members, else 0 (A3OB's export rule). Faces that
        reference a missing point get 0."""
        members = selection.vertex_weights
        count = len(members)
        verts = self.face_vertices
        output = bytearray(self.face_count)
        for index, sides in enumerate(self.face_sides):
            base = 4 * index
            corners = verts[base:base + sides]
            if sides and all(v < count and members[v] for v in corners):
                output[index] = 1
        selection.face_weights = output

    def derive_all_face_weights(self) -> None:
        """``derive_face_weights`` for every selection, e.g. after editing point weights with ``set_weight``."""
        for tagg in self.taggs:
            if isinstance(tagg, SelectionTagg):
                self.derive_face_weights(tagg)

    # -- Named properties --------------------------------------------------------------------------------------------

    def properties(self) -> List[Tuple[str, str]]:
        return [(tagg.key, tagg.value) for tagg in self.taggs if isinstance(tagg, PropertyTagg)]

    def get_property(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Value of the first property whose key matches (case-insensitive)."""
        lower = key.casefold()
        for tagg in self.taggs:
            if isinstance(tagg, PropertyTagg) and tagg.key.casefold() == lower:
                return tagg.value
        return default

    def set_property(self, key: str, value: str) -> PropertyTagg:
        """Set the value of the first property with this key (case-insensitive), or add a new property."""
        _property_field(key, "key")
        _property_field(value, "value")
        lower = key.casefold()
        for tagg in self.taggs:
            if isinstance(tagg, PropertyTagg) and tagg.key.casefold() == lower:
                tagg.value = value
                return tagg
        return self.insert_tagg(PropertyTagg(key, value))

    def remove_property(self, key: str) -> int:
        """Remove every property with this key (case-insensitive); return how many were removed."""
        lower = key.casefold()
        before = len(self.taggs)
        self.taggs = [t for t in self.taggs if not (isinstance(t, PropertyTagg) and t.key.casefold() == lower)]
        return before - len(self.taggs)

    # -- Mass --------------------------------------------------------------------------------------------------------

    @property
    def masses(self) -> Optional[array]:
        """Per-point masses from #Mass#, or None if the LOD has none."""
        tagg = self._first(MassTagg)
        return None if tagg is None else tagg.masses

    def set_masses(self, values: Optional[Iterable[float]]) -> None:
        """Replace the per-point masses; None removes #Mass#."""
        tagg = self._first(MassTagg)
        if values is None:
            self.taggs = [t for t in self.taggs if not isinstance(t, MassTagg)]
            return
        masses = array(F32, values)
        if len(masses) != self.point_count:
            raise ValueError("need one mass per point (%d), got %d" % (self.point_count, len(masses)))
        if tagg is None:
            self.insert_tagg(MassTagg(masses))
        else:
            tagg.masses = masses

    @property
    def total_mass(self) -> float:
        masses = self.masses
        return 0.0 if masses is None else float(sum(masses))

    # -- UV sets -----------------------------------------------------------------------------------------------------

    def uv_set_ids(self) -> List[int]:
        return [tagg.set_id for tagg in self.taggs if isinstance(tagg, UVSetTagg)]

    def uv_set(self, set_id: int = 0) -> Optional[array]:
        """Flat (u, v) per corner. A #UVSet# TAGG wins; set 0 falls back to the UVs in the face records."""
        for tagg in self.taggs:
            if isinstance(tagg, UVSetTagg) and tagg.set_id == set_id:
                return tagg.uvs
        if set_id == 0:
            return self.corner_uvs()
        return None

    def set_uv_set(self, set_id: int, uvs: Iterable[float]) -> UVSetTagg:
        """Create or replace a #UVSet# TAGG (flat u, v per used corner). Does not touch the face records."""
        values = array(F32, uvs)
        if len(values) != 2 * self.corner_count:
            raise ValueError("UV set needs %d values (2 per corner), got %d" % (2 * self.corner_count, len(values)))
        for tagg in self.taggs:
            if isinstance(tagg, UVSetTagg) and tagg.set_id == set_id:
                tagg.uvs = values
                return tagg
        return self.insert_tagg(UVSetTagg(set_id, values))

    def remove_uv_set(self, set_id: int) -> int:
        before = len(self.taggs)
        self.taggs = [t for t in self.taggs if not (isinstance(t, UVSetTagg) and t.set_id == set_id)]
        return before - len(self.taggs)

    # -- Sharp edges -------------------------------------------------------------------------------------------------

    def sharp_edges(self, include_degenerate: bool = False) -> List[Tuple[int, int]]:
        """Point index pairs from #SharpEdges# (empty if there is none). Pairs (a, a) are skipped unless asked."""
        tagg = self._first(SharpEdgesTagg)
        if tagg is None:
            return []
        pairs = tagg.pairs()
        if include_degenerate:
            return pairs
        return [pair for pair in pairs if pair[0] != pair[1]]

    def set_sharp_edges(self, pairs: Iterable[Sequence[int]]) -> SharpEdgesTagg:
        """Create or replace #SharpEdges#. An empty list keeps an empty TAGG; use ``remove_sharp_edges`` to drop it."""
        new = SharpEdgesTagg.from_pairs(pairs)
        tagg = self._first(SharpEdgesTagg)
        if tagg is None:
            return self.insert_tagg(new)
        tagg.edges = new.edges
        return tagg

    def remove_sharp_edges(self) -> None:
        self.taggs = [t for t in self.taggs if not isinstance(t, SharpEdgesTagg)]

    # -- Proxies -----------------------------------------------------------------------------------------------------

    def proxies(self) -> List[ProxyRef]:
        output = []
        for tagg in self.taggs:
            if isinstance(tagg, SelectionTagg):
                parsed = tagg.proxy
                if parsed is not None:
                    output.append(ProxyRef(parsed[0], parsed[1], tagg))
        return output

    def proxy_points(self, selection: SelectionTagg) -> Optional[Tuple[Tuple[float, float, float], ...]]:
        """The three points of a proxy triangle: the first triangle whose vertices are all members, else the
        member points if there are exactly three. None if neither applies."""
        members = selection.vertex_weights
        count = min(len(members), self.point_count)
        verts = self.face_vertices
        for index, sides in enumerate(self.face_sides):
            if sides != 3:
                continue
            corners = verts[4 * index:4 * index + 3]
            if all(v < count and members[v] for v in corners):
                return tuple(self.point(v) for v in corners)
        indices = selection.members()
        if len(indices) == 3:
            return tuple(self.point(v) for v in indices)
        return None

    def proxy_frame(self, selection: SelectionTagg) -> Optional[proxy_utils.ProxyFrame]:
        points = self.proxy_points(selection)
        if points is None:
            return None
        return proxy_utils.proxy_frame(*points)

    # -- Consistency -------------------------------------------------------------------------------------------------

    def structure_errors(self) -> List[str]:
        """Inconsistencies that make the LOD unwritable: array lengths, TAGG sizes, and TAGGs that would not read
        back as written (an #EndOfFile# TAGG, a typed TAGG under a name that reads as another kind, a raw TAGG
        under a known name whose payload the reader rejects).

        A raw TAGG that a lenient read kept because its payload did not fit (``RawTagg.counts`` set) is accepted
        while the LOD has the counts it was read with, so lenient round trips stay byte-identical."""
        errors = []
        count_points = len(self.point_flags)
        count_faces = len(self.face_sides)
        if len(self.points) != 3 * count_points:
            errors.append("points has %d values, expected %d (3 per point flag)" % (len(self.points), 3 * count_points))
        if len(self.normals) % 3:
            errors.append("normals has %d values, not a multiple of 3" % len(self.normals))
        for column, width in (("face_vertices", 4), ("face_normals", 4), ("face_uvs", 8)):
            if len(getattr(self, column)) != width * count_faces:
                errors.append("%s has %d values, expected %d" % (column, len(getattr(self, column)), width * count_faces))
        for column in ("face_flags", "face_textures", "face_materials"):
            if len(getattr(self, column)) != count_faces:
                errors.append("%s has %d entries, expected %d" % (column, len(getattr(self, column)), count_faces))
        bad_sides = [s for s in self.face_sides if s > MAX_FACE_SIDES]
        if bad_sides:
            errors.append("faces with more than %d sides: %r" % (MAX_FACE_SIDES, bad_sides[:5]))
        corners = sum(self.face_sides)
        counts = (count_points, count_faces, corners)
        for tagg in self.taggs:
            name = tagg.name
            fixed_name = _FIXED_NAMES.get(type(tagg))
            if name == END_OF_FILE:
                errors.append("a TAGG cannot be named %s (it ends the TAGG list)" % END_OF_FILE)
            elif fixed_name is not None and name != fixed_name:
                errors.append("%s is named %r, expected %r" % (type(tagg).__name__, name, fixed_name))
            if isinstance(tagg, SelectionTagg):
                if not is_selection_name(name):
                    errors.append("selection name %r starts or ends with '#' (it would read back as another TAGG)"
                                  % name)
                if len(tagg.vertex_weights) != count_points or len(tagg.face_weights) != count_faces:
                    errors.append("selection %r has %d/%d weights, expected %d/%d" % (
                        name, len(tagg.vertex_weights), len(tagg.face_weights), count_points, count_faces))
            elif isinstance(tagg, MaskTagg):
                if name not in EDITOR_MASKS:
                    errors.append("MaskTagg is named %r, expected one of %r" % (name, EDITOR_MASKS))
                if len(tagg.vertex_bytes) != count_points or len(tagg.face_bytes) != count_faces:
                    errors.append("%s has %d/%d bytes, expected %d/%d" % (
                        name, len(tagg.vertex_bytes), len(tagg.face_bytes), count_points, count_faces))
            elif isinstance(tagg, MassTagg):
                if len(tagg.masses) != count_points:
                    errors.append("#Mass# has %d values, expected %d" % (len(tagg.masses), count_points))
            elif isinstance(tagg, UVSetTagg):
                if len(tagg.uvs) != 2 * corners:
                    errors.append("#UVSet# %d has %d values, expected %d" % (tagg.set_id, len(tagg.uvs), 2 * corners))
            elif isinstance(tagg, AnimationTagg):
                if len(tagg.positions) != 3 * count_points:
                    errors.append("#Animation# frame has %d values, expected %d (3 per point)" % (
                        len(tagg.positions), 3 * count_points))
            elif isinstance(tagg, SharpEdgesTagg):
                if len(tagg.edges) % 2:
                    errors.append("#SharpEdges# has an odd number of indices")
            elif isinstance(tagg, RawTagg) and is_typed_tagg_name(name):
                problem = tagg_payload_problem(name, tagg.data, *counts)
                kept_from_read = tagg.counts is not None and (name in _FIXED_SIZE_NAMES or tagg.counts == counts)
                if problem is not None and not kept_from_read:
                    errors.append("raw TAGG %r would not read back: %s" % (name, problem))
            if not 0 <= tagg.active <= 255:
                errors.append("TAGG %r active byte out of range: %r" % (tagg.name, tagg.active))
        return errors

    def check(self) -> List[str]:
        """``structure_errors``, then problems that do not stop writing but that Object Builder, Binarize or the
        glue will not handle: out-of-range point/normal indices in faces and sharp edges, faces with fewer than 3
        sides, duplicate #UVSet# ids, selection names that differ only by case (``selection`` finds only the first)
        and proxy selections without a triangle."""
        errors = self.structure_errors()
        if errors:
            return errors
        count_points = self.point_count
        count_normals = self.normal_count
        for index, sides in enumerate(self.face_sides):
            base = 4 * index
            if sides < 3:
                errors.append("face %d has %d sides (at least 3 needed)" % (index, sides))
            if any(v >= count_points for v in self.face_vertices[base:base + sides]):
                errors.append("face %d references a missing point" % index)
            if any(n >= count_normals for n in self.face_normals[base:base + sides]):
                errors.append("face %d references a missing normal" % index)
        for a, b in self.sharp_edges(include_degenerate=True):
            if a >= count_points or b >= count_points:
                errors.append("sharp edge (%d, %d) references a missing point" % (a, b))

        seen_ids = set()
        for set_id in self.uv_set_ids():
            if set_id in seen_ids:
                errors.append("#UVSet# id %d appears more than once" % set_id)
            seen_ids.add(set_id)

        seen_names = {}
        for name in self.selection_names():
            folded = name.casefold()
            if folded in seen_names:
                errors.append("selection %r has the same name as %r up to case" % (name, seen_names[folded]))
            else:
                seen_names[folded] = name

        for ref in self.proxies():
            if self.proxy_points(ref.selection) is None:
                errors.append("proxy selection %r has no triangle (no triangle face of member points and not "
                              "exactly 3 member points)" % ref.selection.name)
        return errors


# --------------------------------------------------------------------------------------------------------------------
# File
# --------------------------------------------------------------------------------------------------------------------

class P3DModel:
    """An MLOD P3D file: a list of LODs.

    ``trailing`` holds any bytes after the last LOD (none in files written by Object Builder) so they round-trip.
    ``warnings`` collects messages from a non-strict read.
    """

    def __init__(self, lods: Optional[Iterable[LOD]] = None):
        self.lods: List[LOD] = list(lods or [])
        self.trailing = b""
        self.warnings: List[str] = []

    def __repr__(self) -> str:
        return "<P3DModel %d LODs>" % len(self.lods)

    @classmethod
    def from_bytes(cls, data: bytes, strict: bool = True) -> "P3DModel":
        from .reader import read_p3d
        return read_p3d(data, strict=strict)

    @classmethod
    def read_file(cls, path, strict: bool = True) -> "P3DModel":
        from .reader import read_p3d_file
        return read_p3d_file(path, strict=strict)

    def to_bytes(self) -> bytes:
        from .writer import write_p3d
        return write_p3d(self)

    def write_file(self, path) -> None:
        from .writer import write_p3d_file
        write_p3d_file(self, path)

    def find_lod(self, lod_type: LodType, resolution: float = 0.0) -> Optional[LOD]:
        signature = encode_signature(lod_type, resolution)
        for lod in self.lods:
            if lod.resolution_signature == signature:
                return lod
        return None

    def duplicate_lods(self) -> List[int]:
        """Indices of LODs whose signature already appeared earlier in the list."""
        seen = set()
        duplicates = []
        for index, lod in enumerate(self.lods):
            if lod.resolution_signature in seen:
                duplicates.append(index)
            else:
                seen.add(lod.resolution_signature)
        return duplicates

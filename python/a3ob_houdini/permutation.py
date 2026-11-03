# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The point, face and corner permutation of an import, and the remap of index-bound TAGG payloads. No ``hou`` here.

Why it exists (spec 13.6, finding F-4 of task #29). The importer turns each packed proxy into a packed prim. The
export writes the mesh first and the proxies last. So a file whose proxies are not the last points and faces comes
back in another order. A TAGG that stores point or face numbers (an editor mask, a sharp edge pair, a mass per point)
must follow that move, or it lands on other points.

The one rule: **the order in Houdini is the order the export writes.** The importer builds a ``Permutation`` from
the file order to that order and remaps every index-bound record through it. The export then writes the records
unchanged. After the export the records refer to the same points and faces as in the source file.

Words used here:

- *old* is the index in the source file; *new* is the index in the order the export writes (mesh points ascending,
  then three points per proxy in proxy face order, each proxy's points ascending).
- ``point_old[new] == old``. ``new_point(old)`` is the inverse. The same for faces and corners.
- A corner is one vertex of a face, numbered in file face order and file corner order (spec 4.3). Corners move with
  their face, in blocks of ``sides``.

Task #37 (D23, byte-identical layout record) keeps this permutation to restore the file order. It needs only
``point_old`` and ``face_old``; the inverse and the corner order are derived.
"""

import struct
from typing import List, Optional, Sequence

from a3ob_core.p3d import model as m1

__all__ = ["Permutation", "HIDE", "remap_payload"]

#: Object Builder's own "hide" state. The reader keeps it as an unknown TAGG. In the Samples it is always
#: ``points + faces`` bytes long, so it is treated as one more editor mask (one byte per point, then one per face).
HIDE = "#Hide#"


class Permutation:
    """How the points and faces of one LOD move between the file order and the export order."""

    def __init__(self, point_old: Sequence[int], face_old: Sequence[int], face_sides: Sequence[int]):
        #: ``point_old[new]``: the file index of the point that is number ``new`` in the export order.
        self.point_old: List[int] = list(point_old)
        #: ``face_old[new]``: the file index of the face that is number ``new`` in the export order.
        self.face_old: List[int] = list(face_old)
        #: Sides of every face, by file index.
        self.face_sides: List[int] = list(face_sides)
        self._point_new: Optional[List[int]] = None
        self._face_new: Optional[List[int]] = None
        self._corner_old: Optional[List[int]] = None

    @classmethod
    def for_import(cls, mesh_points: Sequence[int], mesh_faces: Sequence[int], proxies, face_sides: Sequence[int]):
        """The permutation of an import: the mesh points and faces in file order, then each packed proxy (in proxy face
        order) with its points ascending. ``proxies`` has ``points`` (ascending file indices) and ``face``."""
        point_old = list(mesh_points)
        face_old = list(mesh_faces)
        for proxy in proxies:
            point_old.extend(proxy.points)
            face_old.append(proxy.face)
        return cls(point_old, face_old, face_sides)

    @classmethod
    def identity(cls, point_count: int, face_sides: Sequence[int]):
        return cls(range(point_count), range(len(face_sides)), face_sides)

    @property
    def point_count(self) -> int:
        return len(self.point_old)

    @property
    def face_count(self) -> int:
        return len(self.face_old)

    @property
    def corner_count(self) -> int:
        return sum(self.face_sides)

    @property
    def is_identity(self) -> bool:
        return self.point_old == list(range(len(self.point_old))) and self.face_old == list(range(len(self.face_old)))

    @staticmethod
    def _inverse(order: Sequence[int]) -> List[int]:
        inverse = [0] * len(order)
        for new, old in enumerate(order):
            inverse[old] = new
        return inverse

    @property
    def point_new(self) -> List[int]:
        """``point_new[old]``: the export-order number of a file point."""
        if self._point_new is None:
            self._point_new = self._inverse(self.point_old)
        return self._point_new

    @property
    def face_new(self) -> List[int]:
        if self._face_new is None:
            self._face_new = self._inverse(self.face_old)
        return self._face_new

    @property
    def corner_old(self) -> List[int]:
        """``corner_old[new]``: the file corner behind export corner ``new``. Corners follow their face."""
        if self._corner_old is None:
            start = []
            total = 0
            for sides in self.face_sides:
                start.append(total)
                total += sides
            order: List[int] = []
            for old_face in self.face_old:
                order.extend(range(start[old_face], start[old_face] + self.face_sides[old_face]))
            self._corner_old = order
        return self._corner_old

    def new_point(self, old: int) -> int:
        """The export-order number of a file point. A number outside the LOD (a damaged file) is returned as it is."""
        return self.point_new[old] if 0 <= old < len(self.point_old) else old

    # -- Reordering per-element data ----------------------------------------------------------------------------

    @staticmethod
    def _take(values, order: Sequence[int], stride: int):
        if stride == 1:
            return bytes(values[o] for o in order) if isinstance(values, (bytes, bytearray)) else [values[o] for o in order]
        return b"".join(bytes(values[o * stride:(o + 1) * stride]) for o in order)

    def points(self, values, stride: int = 1):
        """Per-point data from file order to export order (``stride`` bytes or items per point)."""
        return self._take(values, self.point_old, stride)

    def faces(self, values, stride: int = 1):
        return self._take(values, self.face_old, stride)

    def corners(self, values, stride: int = 1):
        return self._take(values, self.corner_old, stride)


def _masklike(payload: bytes, perm: Permutation) -> Optional[bytes]:
    """One byte per point, then one per face (selection, editor masks, ``#Hide#``)."""
    if len(payload) != perm.point_count + perm.face_count:
        return None
    split = perm.point_count
    return bytes(perm.points(payload[:split])) + bytes(perm.faces(payload[split:]))


def remap_payload(tagg: m1.Tagg, perm: Permutation) -> Optional[bytes]:
    """The payload of ``tagg`` rewritten for the export order, or None when this TAGG cannot be remapped.

    ``None`` means: the structure of the record is unknown (an unknown name, a known name with a payload that did not
    fit), or it has the wrong size for the LOD, or a sharp pair points outside the LOD. The caller drops it with a
    warning. A ``#Property#`` is not index-bound and comes back unchanged.
    """
    payload = tagg.payload()
    if isinstance(tagg, m1.PropertyTagg):
        return payload
    if isinstance(tagg, (m1.SelectionTagg, m1.MaskTagg)) or (isinstance(tagg, m1.RawTagg) and tagg.name == HIDE):
        return _masklike(payload, perm)
    if isinstance(tagg, m1.MassTagg):
        return bytes(perm.points(payload, 4)) if len(payload) == 4 * perm.point_count else None
    if isinstance(tagg, m1.AnimationTagg):
        if len(payload) != 4 + 12 * perm.point_count:
            return None
        return payload[:4] + perm.points(payload[4:], 12)
    if isinstance(tagg, m1.UVSetTagg):
        if len(payload) != 4 + 8 * perm.corner_count:
            return None
        return payload[:4] + perm.corners(payload[4:], 8)
    if isinstance(tagg, m1.SharpEdgesTagg):
        if len(payload) % 8:
            return None
        flat = struct.unpack("<%dI" % (len(payload) // 4), payload)
        if any(index >= perm.point_count for index in flat):
            return None
        return struct.pack("<%dI" % len(flat), *(perm.point_new[index] for index in flat))
    return None

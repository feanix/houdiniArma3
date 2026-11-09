# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/data.py, v2.5.1 (9f2b8bc): the flag tables ``flags_vertex_*``, ``flags_face_*`` and the
# single flags are copied here as masks and value lists. A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden
# (original ArmaToolbox).
"""Spec 5 "Interpolation hazard" and 17.1: warn about flag values that Object Builder does not know. No ``hou`` here.

Flags are raw bitfields (spec 5). Subdivide, Resample and similar nodes average integer attributes into numbers that
mean nothing, and nothing else would tell the artist. A flag value is known when every field of it holds a value from
A3OB's tables. The list is the same for every LOD type, because A3OB's tables are. A face flag may use any of the user
bits (``0xfe000000``).
"""

from typing import List, Tuple

from .. import messages
from .data import LodData

__all__ = ["unknown_vertex_flags", "unknown_face_flags", "check_flags"]

#: (mask, allowed values within the mask). ``None`` for the values means any value is allowed.
_VERTEX_FIELDS = (
    (0x0000000F, (0x0, 0x1, 0x2, 0x4, 0x8)),             # surface
    (0x00003000, (0x0, 0x1000, 0x2000)),                 # fog
    (0x00000100, (0x0, 0x100)),                          # decal
    (0x000000F0, (0x0, 0x10, 0x20, 0x40, 0x80)),         # lighting
    (0x06000000, (0x0, 0x04000000, 0x02000000)),         # normals
    (0x01000000, (0x0, 0x01000000)),                     # hidden
)
_FACE_FIELDS = (
    (0x003000A0, (0x0, 0x20, 0x80, 0x100000, 0x200000)),  # lighting
    (0x00000300, None),                                           # z bias
    (0x00000010, None),                                           # no shadow
    (0x01000000, None),                                           # merging
    (0x00003000, (0x0, 0x1000, 0x2000)),                          # 0x1000, 0x2000 (D24/D25: Bohemia's Samples use them)
    (0xFE000000, None),                                           # user bits
)


def _unknown(value: int, fields) -> bool:
    value &= 0xFFFFFFFF
    known = 0
    for mask, allowed in fields:
        known |= mask
        if allowed is not None and (value & mask) not in allowed:
            return True
    return bool(value & ~known)


def unknown_vertex_flags(value: int) -> bool:
    """True if a vertex flag value has a field that is not in A3OB's tables."""
    return _unknown(value, _VERTEX_FIELDS)


def unknown_face_flags(value: int) -> bool:
    """True if a face flag value has a field that is not in A3OB's tables."""
    return _unknown(value, _FACE_FIELDS)


def _text(found: List[Tuple[int, int]]) -> str:
    shown = ", ".join("%d (0x%x)" % (number, value & 0xFFFFFFFF) for number, value in found[:3])
    more = len(found) - 3
    return shown + (" and %d more" % more if more > 0 else "")


def check_flags(data: LodData, report: messages.Report) -> None:
    """One warning per LOD for the points and one for the polygons with an unknown flag value."""
    label = data.label
    mesh = data.mesh
    points = [(number, value) for number, value in enumerate(mesh.point_flags or ())
              if number not in mesh.pack_points and unknown_vertex_flags(value)]
    if points:
        report.warning(messages.format_problem(
            label, "%d points have a3ob_vertex_flags that Object Builder does not know (points %s)" % (
                len(points), _text(points)),
            "Nodes that interpolate integers, such as Subdivide or Resample, mix flag bits. Set the flags again"))
    faces = [(poly.number, poly.flags) for poly in mesh.polys if unknown_face_flags(poly.flags)]
    if faces:
        report.warning(messages.format_problem(
            label, "%d polygons have a3ob_face_flags that Object Builder does not know (prims %s)" % (
                len(faces), _text(faces)),
            "Nodes that interpolate integers mix flag bits. Set the flags again"))

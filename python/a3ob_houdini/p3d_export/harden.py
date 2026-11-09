# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py and utilities/validator.py, v2.5.1 (9f2b8bc): the sharp-edge rule of
# ``ValidatorLODShadow.is_sharp`` (validator.py:338-352) is not checked here but enforced, and the sharp edges and
# normals of a fully flat shaded mesh follow ``process_tagg_sharp`` (export_p3d.py:475-496). A3OB: MrClock (present
# add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""Task #48: Shadow Volume (and Underground) LODs are always written with hard edges. No ``hou`` here.

The one place for this rule: ``harden_lod`` runs at the start of ``lod_build.build_lod``, so the P3D Export node, the
Validate node, Quick Export and the batch convert all get it. It works on a copy of the plain LOD data; the Houdini
geometry is never touched.

What A3OB writes for a fully sharp mesh (every face flat shaded):

* normals: Blender gives a flat face the face normal at each of its corners, and ``process_normals`` writes those
  (export_p3d.py:405-421, ``mesh_static_normals_iterator``). So every corner of a polygon has the polygon's normal.
* ``#SharpEdges#``: ``process_tagg_sharp`` (export_p3d.py:475-491) writes the edges of the flat faces that are contiguous
  (shared by two faces). So every edge shared by two polygons is a pair.

A polygon that already has face normals is left as it is (the file keeps its exact bytes), and so are the sharp edges
that are already there: only the missing edges are added, after the existing ones. A LOD that needs nothing changes
nothing and gets no message. A LOD that needs a change gets one info line.
"""

import dataclasses
import math
from typing import Dict, List, Tuple

from a3ob_core.p3d.lod_types import LodType

from .. import messages
from .data import LodData, MeshData, Poly
from .normals import _normalized, face_normal
from .type_rules import SHADOW_TYPES

__all__ = ["HARDEN_TYPES", "HARDEN_NOTE", "harden_lod"]

#: The LOD types that are always written with hard edges (the old rule T2-SHARP covered the same types).
HARDEN_TYPES = SHADOW_TYPES | {LodType.UNDERGROUND}
#: The ending of the info line; Validate counts the lines that have it.
HARDEN_NOTE = "all edges written as sharp (shadow LODs need hard edges)"
#: A smooth polygon: a vertex normal that is further from the face normal than this (a dot product).
SMOOTH_DOT = 0.9999


def _is_smooth(mesh: MeshData, poly: Poly) -> bool:
    """True when the vertex normals differ from the face normal, or there are none."""
    if poly.normals is None:
        return True
    x, y, z = face_normal([mesh.points[p] for p in poly.points])
    length = math.sqrt(x * x + y * y + z * z)
    if length == 0.0:
        return False
    face = (x / length, y / length, z / length)
    for normal in poly.normals:
        size = math.sqrt(normal[0] * normal[0] + normal[1] * normal[1] + normal[2] * normal[2])
        if size == 0.0 or (normal[0] * face[0] + normal[1] * face[1] + normal[2] * face[2]) / size < SMOOTH_DOT:
            return True
    return False


def _manifold_edges(polys: List[Poly]) -> Dict[Tuple[int, int], Tuple[int, int]]:
    """Every edge shared by exactly two polygons: undirected edge -> the directed pair of its first polygon."""
    users: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for poly in polys:
        count = len(poly.points)
        for k in range(count):
            a, b = poly.points[k], poly.points[(k + 1) % count]
            if a != b:
                users.setdefault((min(a, b), max(a, b)), []).append((a, b))
    return {edge: found[0] for edge, found in users.items() if len(found) == 2}


def harden_lod(data: LodData, report: messages.Report) -> LodData:
    """``data`` for a LOD that is written with hard edges only. Other LOD types, and LODs that are already fully sharp,
    come back unchanged (the same object)."""
    if data.details.lod_type not in HARDEN_TYPES:
        return data
    mesh = data.mesh
    written = [poly for poly in mesh.polys if 3 <= len(poly.points) <= 4]
    smooth = {number for number, poly in enumerate(mesh.polys) if 3 <= len(poly.points) <= 4 and _is_smooth(mesh, poly)}
    have = {(min(a, b), max(a, b)) for a, b in (mesh.sharp or ())}
    missing = [pair for edge, pair in _manifold_edges(written).items() if edge not in have]
    if not smooth and not missing:
        return data
    polys = list(mesh.polys)
    for number in smooth:
        poly = polys[number]
        normal = _normalized(face_normal([mesh.points[p] for p in poly.points]))
        polys[number] = dataclasses.replace(poly, normals=[normal] * len(poly.points))
    sharp = list(mesh.sharp or ()) + missing
    report.info(messages.format_problem(data.label, HARDEN_NOTE))
    return dataclasses.replace(data, mesh=dataclasses.replace(mesh, polys=polys, sharp=sharp))

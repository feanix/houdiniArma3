# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Task #49 (D28): triangulate the polygons that break the export rules, inside the export.

The one place for this step. ``collect_lods`` calls ``triangulate_lod`` on every LOD geometry before the plain-data
read, so the P3D Export node, Validate, Quick Export and Batch folder convert all get it, and ``harden_lod`` (task #48)
sees the final faces. ``polygons_to_triangulate`` decides on plain data (no ``hou``); ``triangulate_lod`` runs Houdini's
own Divide verb on a copy, so the geometry on the node is not changed.

Which polygons (the same tests as the checks, so a model that passes is untouched):
- more than 4 points, in any LOD: the ``kept_polygons`` error of ``mesh_build`` (P3D faces have 3 or 4 points);
- a quad that is not convex, in a Geometry-family or Shadow Volume LOD: rule T2-TRI of D24;
- any quad, in an Underground LOD: rule T2-TRI of D24.
Quads are left alone while the T2-TRI rule is switched off, because then nothing checks them.
"""

from typing import Dict, List

from a3ob_core.p3d.lod_types import LodType

from .. import messages
from .data import LodData
from .options import ExportOptions
from .type_rules import GEOMETRY_TYPES, SHADOW_TYPES, _Context, _quad_is_convex

__all__ = ["polygons_to_triangulate", "triangulate_lod", "triangulated_warning", "REASON_LONG", "REASON_QUAD"]

REASON_LONG = "more than 4 points"
REASON_QUAD = "quads that are not allowed"


def polygons_to_triangulate(data: LodData, options: ExportOptions) -> Dict[str, List[int]]:
    """``{reason: [primitive numbers]}`` of the polygons that the rules reject. Empty when everything passes."""
    lod_type = data.details.lod_type
    check_quads = "T2-TRI" not in options.rules_off and (
        lod_type in GEOMETRY_TYPES or lod_type in SHADOW_TYPES or lod_type == LodType.UNDERGROUND)
    context = _Context(data, options) if check_quads else None
    long_: List[int] = []
    quads: List[int] = []
    for poly in data.mesh.polys:
        count = len(poly.points)
        if count > 4:
            long_.append(poly.number)
        elif count == 4 and check_quads:
            if lod_type == LodType.UNDERGROUND or not _quad_is_convex(context, poly):
                quads.append(poly.number)
    found: Dict[str, List[int]] = {}
    if long_:
        found[REASON_LONG] = long_
    if quads:
        found[REASON_QUAD] = quads
    return found


def triangulated_warning(label: str, found: Dict[str, List[int]], lod_type: int) -> str:
    """The warning of criterion 3: ``<LOD>: 8 polygons triangulated (more than 4 points: 8). Add a Divide ...``."""
    parts = []
    for reason, numbers in found.items():
        if reason == REASON_QUAD:
            reason = "quads in an Underground LOD" if lod_type == LodType.UNDERGROUND else "quads that are not convex"
        parts.append("%s: %d" % (reason, len(numbers)))
    total = sum(len(numbers) for numbers in found.values())
    return messages.format_problem(
        label, "%d polygons triangulated (%s)" % (total, ", ".join(parts)),
        "Add a Divide on this LOD's branch to control the result yourself")


def triangulate_lod(geo, data: LodData, options: ExportOptions, report: messages.Report):
    """``(new_geo, found)``. ``data`` is the plain-data read of ``geo``. ``new_geo`` is a divided copy, or None when the
    option is off or nothing breaks the rules (then ``geo`` is used as it is). A warning is reported when it changed."""
    import hou
    if not options.triangulate:
        return None, {}
    found = polygons_to_triangulate(data, options)
    if not found:
        return None, {}
    wanted = sorted({number for numbers in found.values() for number in numbers})
    verb = hou.sopNodeTypeCategory().nodeVerb("divide")
    verb.setParms({"group": " ".join(str(number) for number in wanted), "convex": 1, "usemaxsides": 1, "numsides": 3,
                   "avoidsmallangles": 1})
    result = hou.Geometry()
    verb.execute(result, [geo])
    report.warning(triangulated_warning(data.label, found, data.details.lod_type))
    return result, found

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): A3OB scans the scene for visible objects with ``is_a3_lod``;
# here the exporter takes explicit geometries (spec 3.2). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren"
# Frieden (original ArmaToolbox).
"""Spec 16 step 1: collect the LODs of one or more SOP geometries. Needs ``hou``.

- A geometry with the detail attribute ``a3ob_lod_type`` is one LOD; its packed prims are proxies.
- Otherwise every prim must be packed and hold a LOD. For each LOD prim the determinant of ``fullTransform()`` is read
  first, then the prim is opened with the ``unpack`` verb, which bakes the transform once (the exporter never applies
  it again). A negative determinant makes the face builder reverse every polygon.
- PackedFragment prims that share one source detail (equal ``geometryid``) are one LOD.
"""

from typing import List, Optional, Sequence

import hou

from .. import messages
from . import transforms
from .collect_plan import LOD, classify, decode_trailing, group_prims
from .data import Collection, LodData
from .read_geo import read_details, read_mesh, read_proxies

__all__ = ["collect", "unpack_lod"]

TYPE_ATTRIB = "a3ob_lod_type"
TRAILING_ATTRIB = "a3ob_file_trailing"


def collect(geometries: Sequence["hou.Geometry"], report: messages.Report) -> Collection:
    """The LODs of ``geometries`` in order (spec 3.2). Collection errors go to ``report``; check ``has_errors``."""
    result = Collection()
    for number, geo in enumerate(geometries):
        _collect_geometry(geo, number, report, result)
    _collect_trailing(geometries, report, result)
    return result


def unpack_lod(prims: Sequence["hou.Prim"]) -> "hou.Geometry":
    """Open LOD prims with the ``unpack`` verb: the transform is baked, nested packed prims stay packed (spec 3.2)."""
    first = prims[0].geometry()
    wanted = {prim.number() for prim in prims}
    single = hou.Geometry()
    single.merge(first)
    single.deletePrims([prim for prim in single.prims() if prim.number() not in wanted], keep_points=False)
    result = hou.Geometry()
    hou.sopNodeTypeCategory().nodeVerb("unpack").execute(result, [single])
    return result


def _collect_geometry(geo, number: int, report: messages.Report, result: Collection) -> None:
    prims = geo.prims()
    packed = sum(1 for prim in prims if isinstance(prim, hou.PackedPrim))
    kind, problem = classify(geo.findGlobalAttrib(TYPE_ATTRIB) is not None, len(prims), packed, len(geo.points()),
                             geo.findPrimAttrib(TYPE_ATTRIB) is not None)
    where = "input %d" % (number + 1)
    if not kind:
        report.error(messages.format_problem(None, "%s: %s" % (where, problem)))
        return
    if kind == LOD:
        _add_lod(result, geo, False, report)
        return
    infos = [(prim.intrinsicValue("typename") == "PackedFragment", prim.intrinsicValue("geometryid")) for prim in prims]
    for group in group_prims(infos):
        _collect_packed_lod([prims[i] for i in group], where, report, result)


def _collect_packed_lod(prims: List["hou.Prim"], where: str, report: messages.Report, result: Collection) -> None:
    first = prims[0]
    matrix = tuple(first.fullTransform().asTuple())
    mirrored = transforms.det3(matrix) < 0.0
    content = unpack_lod(prims)
    if content.findGlobalAttrib(TYPE_ATTRIB) is None:
        report.error(messages.format_problem(
            None, "%s: packed prim %d has no LOD data (no a3ob_lod_type) inside" % (where, first.number()),
            "Only packed LODs can be in a P3D geometry. Delete it, or make it a LOD"))
        return
    data = _add_lod(result, content, mirrored, report)
    label = data.label
    if len(prims) > 1:
        report.warning(messages.format_problem(
            label, "%d PackedFragment prims of one source were collected as one LOD" % len(prims),
            "Their point and polygon order may differ from the source. Pack without Pack by Name to keep it"))
    if mirrored:
        report.warning(messages.format_problem(
            label, "the packed LOD prim is mirrored (its transform has a negative determinant)",
            "The transform is baked into the points once and the polygon winding is reversed"))
    elif not transforms.is_identity(matrix):
        report.warning(messages.format_problem(
            label, "the packed LOD prim has a transform that is not the identity",
            "It is baked into the points once. Apply it before the export if you do not want that"))


def _add_lod(result: Collection, content, mirrored: bool, report: messages.Report) -> LodData:
    index = len(result.lods)
    details = read_details(content)
    probe = LodData(index, None, details)  # only for the label
    label = probe.label
    mesh = read_mesh(content, label, report)
    data = LodData(index, mesh, details, read_proxies(content, label, report), mirrored)
    result.lods.append(data)
    return data


def _collect_trailing(geometries, report: messages.Report, result: Collection) -> None:
    """Spec 3.3: trailing bytes are written only for a single input that carries ``a3ob_file_trailing``."""
    holders = [geo for geo in geometries if geo.findGlobalAttrib(TRAILING_ATTRIB) is not None]
    if not holders:
        return
    if len(geometries) != 1:
        report.warning(messages.format_problem(
            None, "the trailing file bytes (a3ob_file_trailing) are dropped because there is more than one input",
            "They are kept only when one P3D geometry is exported"))
        return
    data: Optional[bytes] = decode_trailing(holders[0].attribValue(TRAILING_ATTRIB))
    if data is None:
        report.error(messages.format_problem(None, "a3ob_file_trailing is not valid base64", "Delete the attribute"))
    else:
        result.trailing = data

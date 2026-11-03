# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Helpers for the two shapes of A3OB geometry (spec section 3.2), used by Extract LOD and Assemble LODs.

- A LOD geometry has the detail attribute ``a3ob_lod_type``. The whole geometry is one LOD.
- A P3D geometry has only packed prims, and each one holds a LOD (its content has ``a3ob_lod_type``).

Needs ``hou`` (imported where it is used, so the module can be imported under plain Python).
"""

from typing import List, Optional

from a3ob_core import p3d

__all__ = [
    "LOD", "P3D", "EMPTY", "OTHER", "classify", "lod_content", "only_prim", "lod_name_of", "prim_name", "detail_int",
    "copy_detail_attributes", "is_identity",
]

#: Shapes ``classify`` returns.
LOD = "lod"
P3D = "p3d"
EMPTY = "empty"
OTHER = "other"

TYPE_ATTRIB = "a3ob_lod_type"
RESOLUTION_ATTRIB = "a3ob_lod_resolution"
SIGNATURE_ATTRIB = "a3ob_lod_signature"


def detail_int(geo, name: str) -> Optional[int]:
    """The value of an integer detail attribute, or ``None`` when the attribute is missing."""
    if geo.findGlobalAttrib(name) is None:
        return None
    return int(geo.attribValue(name))


def lod_content(prim):
    """The geometry inside a packed prim, or ``None`` if the prim is not packed.

    A PackedGeometry holds it directly. Other kinds (PackedDisk, ...) are opened with the Unpack verb, as spec
    section 3.2 says."""
    import hou
    if not isinstance(prim, hou.PackedPrim):
        return None
    if isinstance(prim, hou.PackedGeometry):
        return prim.getEmbeddedGeometry()
    single = only_prim(prim)
    result = hou.Geometry()
    hou.sopNodeTypeCategory().nodeVerb("unpack").execute(result, [single])
    return result


def only_prim(prim):
    """A new geometry that holds a copy of just this prim (its point goes with it)."""
    import hou
    single = hou.Geometry()
    single.merge(prim.geometry())
    single.deletePrims([other for other in single.prims() if other.number() != prim.number()], keep_points=False)
    return single


def classify(geo) -> str:
    """``LOD``, ``P3D``, ``EMPTY`` (nothing at all) or ``OTHER`` (anything else)."""
    if geo.findGlobalAttrib(TYPE_ATTRIB) is not None:
        return LOD
    prims = geo.prims()
    if not prims:
        return EMPTY if not geo.points() else OTHER
    for prim in prims:
        content = lod_content(prim)
        if content is None or content.findGlobalAttrib(TYPE_ATTRIB) is None:
            return OTHER
    return P3D


def lod_name_of(content) -> str:
    """The A3OB display name of a LOD geometry, from its type and resolution (ux-tools.md 3.2)."""
    lod_type = detail_int(content, TYPE_ATTRIB)
    resolution = content.attribValue(RESOLUTION_ATTRIB) if content.findGlobalAttrib(RESOLUTION_ATTRIB) else 0.0
    try:
        return p3d.format_lod_name(lod_type, resolution)
    except ValueError:  # a type number that is not a LodType
        return "LOD type %s" % lod_type


def prim_name(prim, content) -> str:
    """The outer ``name`` of a LOD prim, or the name made from its content when the prim has none."""
    name = prim.attribValue("name") if prim.geometry().findPrimAttrib("name") is not None else ""
    return name or lod_name_of(content)


def copy_detail_attributes(source, target) -> None:
    """Copy every detail attribute of ``source`` that ``target`` does not have yet.

    ``Geometry.merge`` already does this when the target has no detail attributes; this makes it certain."""
    import hou
    for attrib in source.globalAttribs():
        name = attrib.name()
        if target.findGlobalAttrib(name) is not None:
            continue
        value = source.attribValue(name)
        data = attrib.dataType()
        if attrib.isArrayType():
            target.addArrayAttrib(hou.attribType.Global, name, data)
        else:
            zero = {hou.attribData.Int: 0, hou.attribData.Float: 0.0, hou.attribData.String: ""}[data]
            target.addAttrib(hou.attribType.Global, name, zero if attrib.size() == 1 else (zero,) * attrib.size())
        target.setGlobalAttribValue(name, list(value) if attrib.isArrayType() else value)


def is_identity(matrix, tolerance: float = 1e-9) -> bool:
    """True if a ``hou.Matrix4`` is the identity."""
    import hou
    values: List[float] = list(matrix.asTuple())
    wanted: List[float] = list(hou.Matrix4(1).asTuple())
    return all(abs(a - b) <= tolerance for a, b in zip(values, wanted))

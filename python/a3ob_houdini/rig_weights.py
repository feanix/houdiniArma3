# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Bone weights in Houdini geometry, for Weights Convert and Weight Cleanup (character-rigging.md 4.2 to 4.4, task #62).

Two forms hold the same weights:

- **Selections** (the form the P3D importer makes, data model 7.2): a point group per selection, named
  ``hou.text.encodeAttrib(name)``, and a point float ``a3ob_w_<group>`` (default 1.0, made only when some member's weight
  is not 1). A byte-255 member has weight 0.0, which means "no weight".
- **Capture** (KineFX): the point attribute ``boneCapture`` with the detail ``boneCapture_pCaptPath`` (joint names) and
  the hidden ``boneCapture_pCaptData`` (see ``a3ob_core.rig.weights.inverse_rest_floats``). The Capture Attribute Pack
  and Unpack verbs convert between the packed attribute and the ``boneCapture_index`` / ``boneCapture_data`` arrays.

Only bone selections (names that match a skeleton bone ignoring case) are read or changed. Every other group stays.
"""

import dataclasses
from typing import Dict, List, Optional

import numpy as np

from a3ob_core.rig import CAPT_FLOATS, capture_lists, inverse_rest_floats, match_bone_names, matrix_from_capture

from . import geo_edit, names
from .errors import UserError
from .messages import format_problem, list_elements

try:
    import hou
except ImportError:  # the pure parts are also read under plain Python
    hou = None

__all__ = ["Skeleton", "BoneSelections", "read_skeleton", "read_selections", "write_selections", "write_capture",
           "read_capture", "TAGG_ORDER", "DISCRETE_ATTRIB", "PART_ATTRIB", "PART_SKELETON", "PART_BODY",
           "PART_LANDMARKS", "write_parts", "read_part"]

TAGG_ORDER = "a3ob_tagg_order"
DISCRETE_ATTRIB = "a3ob_rig_discrete"
#: The prim string of the packed primitives of a Rig Fit output (D40): which result each one holds.
PART_ATTRIB = "a3ob_rig_part"
PART_SKELETON = "fitted_skeleton"
PART_BODY = "fitted_body"
PART_LANDMARKS = "landmarks"
CAPTURE = "boneCapture"
CAPTURE_PATH = "boneCapture_pCaptPath"
CAPTURE_DATA = "boneCapture_pCaptData"


@dataclasses.dataclass
class Skeleton:
    names: List[str]
    positions: List[tuple]
    #: The 9-float ``transform`` of each joint, or None when the points have none.
    rotations: List[Optional[tuple]]
    #: The detail ``a3ob_rig_discrete`` (1 or 0), or None when the skeleton does not carry it.
    discrete: Optional[int]
    #: The parent joint number of each joint (-1 for a root), from the polylines that run parent to child.
    parents: List[int] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BoneSelections:
    """The bone selections of a geometry: ``weights`` is points x bones, ``groups`` maps a bone number to the Houdini
    group name that holds it, ``zero_members`` counts the members with weight 0.0 (bytes 255), ``other`` the groups
    that are not bone selections."""
    weights: np.ndarray
    groups: Dict[int, str]
    zero_members: int
    other: List[str]


def read_skeleton(node_name: str, geo) -> Skeleton:
    """The joints of a skeleton geometry (the Rig Skeleton output): ``name``, ``P`` and ``transform`` per point."""
    if geo is None:
        raise UserError(format_problem(node_name, "the skeleton input is not connected",
                                       "Connect a Rig Skeleton to input 2"))
    attrib = geo.findPointAttrib("name")
    if attrib is None or not geo.points():
        raise UserError(format_problem(node_name, "input 2 is not a skeleton (its points have no name)",
                                       "Connect a Rig Skeleton to input 2"))
    transform = geo.findPointAttrib("transform")
    joints = geo.points()
    rotations = [tuple(p.attribValue("transform")) if transform is not None and transform.size() == 9 else None
                 for p in joints]
    discrete = geo_edit.read_detail(geo, DISCRETE_ATTRIB)
    parents = [-1] * len(joints)
    for prim in geo.prims():
        if len(prim.vertices()) == 2:
            parents[prim.vertex(1).point().number()] = prim.vertex(0).point().number()
    return Skeleton([p.attribValue("name") for p in joints], [tuple(p.position()) for p in joints], rotations,
                    None if discrete is None else int(discrete), parents)


def read_selections(node_name: str, geo, bones) -> BoneSelections:
    """Read the bone selections into a weight matrix. A selection that matches no bone is left out (and listed in
    ``other``). Two selections that differ only in case and match one bone are an error."""
    groups = {names.decode_group_name(g.name()): g.name() for g in geo.pointGroups()}
    matches, clashes = match_bone_names(bones, list(groups))
    if clashes:
        raise UserError(format_problem(node_name, "the selections %s and %s differ only in case and are the same bone" % clashes[0],
                                       "Delete or rename one of them"))
    count = len(geo.points())
    weights = np.zeros((count, len(bones)))
    found: Dict[int, str] = {}
    zero = 0
    for bone, selection in matches.items():
        group = groups[selection]
        members = np.array([p.number() for p in geo.findPointGroup(group).points()], dtype=np.int64)
        found[bone] = group
        if not len(members):
            continue
        attrib = names.point_weight_attrib(group)
        if geo.findPointAttrib(attrib) is not None:
            values = np.array(geo.pointFloatAttribValues(attrib), dtype=np.float64)[members]
        else:
            values = np.ones(len(members))
        zero += int(np.count_nonzero(values <= 0))
        weights[members, bone] = np.maximum(values, 0.0)
    matched = set(found.values())
    other = [name for name, group in groups.items() if group not in matched]
    return BoneSelections(weights, found, zero, other)


def _spelling(bone: str, existing: Optional[str], keep: bool) -> str:
    """The Houdini group name a bone selection is written to."""
    if keep and existing is not None:
        return existing
    return names.encode_group_name(bone)


def write_selections(geo, bones, weights: np.ndarray, groups: Dict[int, str], columns=None, keep_spelling: bool = False
                     ) -> int:
    """Write ``weights`` (points x bones) into the bone selections of ``geo``. Returns the number of selections renamed.

    ``columns`` limits it to some bones (default: all). A bone without weights and without a group is not made. A
    group keeps its place; a weight attribute is made only when some member's weight is not 1. With ``keep_spelling`` an
    existing group keeps its name; otherwise it gets the bone's spelling from the skeleton (a prim group of the same
    name, its ``a3ob_fw_`` weights and the ``a3ob_tagg_order`` hint move with it)."""
    points = geo.points()
    renamed = 0
    for bone in (range(len(bones)) if columns is None else columns):
        members = np.nonzero(weights[:, bone] > 0)[0]
        old = groups.get(bone)
        if old is None and not len(members):
            continue
        new = _spelling(bones[bone], old, keep_spelling)
        if old is not None and old != new:
            _rename_selection(geo, old, new)
            renamed += 1
        group = geo.findPointGroup(new) or geo.createPointGroup(new)
        group.clear()
        group.add([points[i] for i in members])
        attrib = names.point_weight_attrib(new)
        values = np.ones(len(points))
        values[members] = weights[members, bone]
        if geo.findPointAttrib(attrib) is None:
            if not len(members) or bool(np.all(values[members] == 1.0)):
                continue
            geo.addAttrib(hou.attribType.Point, attrib, 1.0)
        geo.setPointFloatAttribValues(attrib, [float(v) for v in values])
    return renamed


def _rename_selection(geo, old: str, new: str) -> None:
    """Move a selection to another group name: the point group (emptied here, filled by the caller), its weight attribute,
    the prim group and ``a3ob_fw_`` of the same name, and the name in ``a3ob_tagg_order``."""
    group = geo.findPointGroup(old)
    if group is not None:
        group.destroy()
    attrib = geo.findPointAttrib(names.point_weight_attrib(old))
    if attrib is not None:
        attrib.destroy()
    prim_group = geo.findPrimGroup(old)
    if prim_group is not None:
        target = geo.findPrimGroup(new) or geo.createPrimGroup(new)
        target.add(prim_group.prims())
        face = geo.findPrimAttrib(names.prim_weight_attrib(old))
        if face is not None:
            values = geo.primFloatAttribValues(face.name())
            geo.addAttrib(hou.attribType.Prim, names.prim_weight_attrib(new), 1.0)
            geo.setPrimFloatAttribValues(names.prim_weight_attrib(new), values)
            face.destroy()
        prim_group.destroy()
    order = geo_edit.read_detail(geo, TAGG_ORDER)
    if order:
        old_name, new_name = names.decode_group_name(old), names.decode_group_name(new)
        geo_edit.set_detail_array(geo, TAGG_ORDER, "string", [new_name if item == old_name else item for item in order])


def write_capture(geo, skeleton: Skeleton, weights: np.ndarray) -> None:
    """Write ``weights`` as KineFX capture weights for ``skeleton``: ``boneCapture`` and its details. The regions are the
    joints in point order. An older ``boneCapture`` is replaced."""
    for name in (CAPTURE, "boneCapture_index", "boneCapture_data"):
        attrib = geo.findPointAttrib(name)
        if attrib is not None:
            attrib.destroy()
    for name in (CAPTURE_PATH, CAPTURE_DATA, "pCaptFrame", "pCaptSkelRoot"):
        geo_edit.remove_detail(geo, name)
    indices, data = capture_lists(weights)
    geo.addArrayAttrib(hou.attribType.Point, "boneCapture_index", hou.attribData.Int, 1)
    geo.addArrayAttrib(hou.attribType.Point, "boneCapture_data", hou.attribData.Float, 1)
    for point, listed, values in zip(geo.points(), indices, data):
        point.setAttribValue("boneCapture_index", tuple(listed))
        point.setAttribValue("boneCapture_data", tuple(values))
    floats: List[float] = []
    for position, rotation in zip(skeleton.positions, skeleton.rotations):
        floats.extend(inverse_rest_floats(position, rotation))
    geo_edit.set_detail_array(geo, CAPTURE_PATH, "string", skeleton.names)
    # The data is an array of 20-float tuples, one per region (a flat array of size 1 is not read by the pack verb).
    geo.addArrayAttrib(hou.attribType.Global, CAPTURE_DATA, hou.attribData.Float, CAPT_FLOATS)
    geo.setGlobalAttribValue(CAPTURE_DATA, floats)
    geo_edit.set_detail(geo, "pCaptFrame", 0.0)
    geo_edit.set_detail(geo, "pCaptSkelRoot", "")
    packed = hou.Geometry()
    hou.sopNodeTypeCategory().nodeVerb("captureattribpack").execute(packed, [geo])
    geo.copy(packed)


def read_capture(node_name: str, geo, bones):
    """``(weights, covered)``: the weight matrix (points x bones) of the capture weights of ``geo`` and the numbers of the
    bones that have a region. The regions are matched to the bones ignoring case; a region that is not a bone is an
    error."""
    if geo.findPointAttrib(CAPTURE) is None:
        raise UserError(format_problem(node_name, "the geometry has no capture weights (boneCapture)",
                                       "Use Selections to Capture, or a Joint Capture node, first"))
    unpacked = hou.Geometry()
    hou.sopNodeTypeCategory().nodeVerb("captureattribunpack").execute(unpacked, [geo])
    regions = list(geo_edit.read_detail(unpacked, CAPTURE_PATH, ()))
    index = {}
    for number, bone in enumerate(bones):
        index.setdefault(bone.lower(), number)
    target = [index.get(region.rsplit("/", 1)[-1].lower()) for region in regions]
    missing = [region for region, bone in zip(regions, target) if bone is None]
    if missing:
        raise UserError(format_problem(node_name, "the capture has %s that are not bones of the skeleton" % list_elements(
            "regions", missing), "Connect the skeleton the capture was made for to input 2"))
    count = len(unpacked.points())
    indices = [tuple(p.attribValue("boneCapture_index")) for p in unpacked.points()]
    data = [tuple(p.attribValue("boneCapture_data")) for p in unpacked.points()]
    regional = matrix_from_capture(indices, data, len(regions))
    weights = np.zeros((count, len(bones)))
    for region, bone in enumerate(target):
        weights[:, bone] += regional[:, region]
    return weights, sorted(set(target))


def write_parts(geo, parts, hidden=()) -> None:
    """Make ``geo`` hold the geometries of ``parts`` (a dict, name to ``hou.Geometry``) as packed primitives, each with
    the prim string ``a3ob_rig_part`` set to its name. The parts in ``hidden`` are not drawn in the viewport; they stay in
    the data."""
    geo.clear()
    geo.addAttrib(hou.attribType.Prim, PART_ATTRIB, "")
    for name, part in parts.items():
        prim = geo.createPackedGeometry(part)
        prim.setAttribValue(PART_ATTRIB, name)
        if name in hidden:
            prim.setIntrinsicValue("viewportlod", "hidden")


def read_part(node_name: str, geo, part: str, label: str = "input 2"):
    """The geometry of the part ``part`` of a Rig Fit output. A geometry without ``a3ob_rig_part`` is not a Rig Fit output
    and is returned as it is (a plain source body keeps working)."""
    if geo is None or geo.findPrimAttrib(PART_ATTRIB) is None:
        return geo
    for prim in geo.prims():
        if isinstance(prim, hou.PackedPrim) and prim.attribValue(PART_ATTRIB) == part:
            return prim.getEmbeddedGeometry()
    raise UserError(format_problem(node_name, "%s has no part %s" % (label, part), "Connect the output of Rig Fit"))

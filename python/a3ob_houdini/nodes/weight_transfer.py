# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no weight transfer. The method is Abdrashitov et al., "Robust Skin Weights Transfer via Weight Inpainting",
# SIGGRAPH Asia 2023 Technical Communications; the maths is in ``a3ob_core.rig.transfer``.
"""The Weight Transfer node (docs/design/character-rigging.md 2.1 to 2.5 and 6.2, tasks #65 and #73, D41): copies the bone
weights of a source body (for example Bohemia's sample character) onto the body of the user's mesh.

Input 1 is the user mesh, input 2 the source body with bone selections (a Rig Fit output is accepted: its fitted sample
body is used), input 3 the skeleton (it gives the bone names and the spelling of the new selections). The one group,
Body, holds the tight body-conforming polygons (skin, undergarments, shoes); empty means every point. The new weights are
written as bone selections (point groups with ``a3ob_w_*``) in the ``model.cfg`` spelling; points outside the group keep
the weights they have. Accessories (helmet, pouches, straps) are weighted by the Accessory Weights node after this one;
run Weight Cleanup last.

The points get ``a3ob_rig_origin`` (1 copied, 2 filled in); points outside the group keep the value they had.
"""

import numpy as np

from a3ob_core.rig import ORIGIN_NONE, Settings, transfer_body

from .. import kept_result, rig_weights
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms
from .weight_cleanup import _proxy_points

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Weight Transfer"
ORIGIN_ATTRIB = "a3ob_rig_origin"

SPEC = NodeSpec(
    kind="weight_transfer",
    label="Weight Transfer",
    summary="Copies the bone weights of a source body (the sample character) onto the body of your mesh. Accessories "
            "are weighted by Accessory Weights.",
    inputs=(3, 3),
    input_labels=("Your mesh", "Source body", "Skeleton"),
    input_help=("The mesh that gets the weights. It should lie on top of the source body (fit it first).",
                "The body with bone weights: bone selections (point groups with a3ob_w weights), for example the sample "
                "character from Extract LOD. The output of Rig Fit goes here as it is: its fitted sample body is used.",
                "The skeleton, for example from Rig Skeleton. Its joint names decide which selections are bones and how "
                "they are spelled."),
    parms=(
        Parm("body", "Body", "group", "",
             "The tight body-conforming polygons: skin, undergarments, shoes. A point group or a primitive group "
             "(its primitives' points count). They get their weights from the source "
             "body. Empty means every point. Points outside the group keep the weights they have (none on a fresh "
             "mesh): weight them with Accessory Weights.", group_type="points"),
        Parm("distance", "Match Distance", "float", 0.05,
             "How far from the source body a point may be and still copy its weights, as a share of the size of your "
             "mesh (the diagonal of its bounding box). Points that are farther are filled in from their neighbours.",
             minimum=0.0),
        Parm("angle", "Match Angle", "float", 35.0,
             "How much the surface direction may differ, in degrees, for a point to copy weights. A point that faces "
             "another way (a sleeve against the torso) is filled in instead.", minimum=0.0, maximum=180.0),
        Parm("minpiece", "Smallest Piece", "int", 10,
             "Loose pieces of the source body with fewer points than this are not used (the sample's proxy "
             "triangles).", minimum=1),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values)
    if kept_result.replay_geometry(node, report, key):  # nothing changed since the last cook: the kept result (task #97)
        return
    skeleton = rig_weights.read_skeleton(NAME, node.inputGeometry(2))
    source = node.inputGeometry(1)
    if source is None:
        raise UserError(format_problem(NAME, "the source body is not connected", "Connect the source body to input 2"))
    source = rig_weights.read_part(NAME, source, rig_weights.PART_BODY)  # a Rig Fit output gives its fitted sample body
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    bones = skeleton.names
    src_found = rig_weights.read_selections(NAME, source, bones)
    if not src_found.groups:
        raise UserError(format_problem(NAME, "no selection of the source body is named like a joint of the skeleton",
                                       "Connect a body with bone weights to input 2 and its skeleton to input 3"))
    src_P, src_tris = rig_weights.read_mesh(source)
    dst_P, dst_tris = rig_weights.read_mesh(geo)
    if not len(src_tris):
        raise UserError(format_problem(NAME, "the source body has no polygons", "Use a body with polygons on input 2"))
    if not len(dst_tris):
        raise UserError(format_problem(NAME, "your mesh has no polygons", "Use a mesh with polygons on input 1"))
    count = len(dst_P)

    skip = np.zeros(count, dtype=bool)  # proxy points: not skin
    skip[sorted(_proxy_points(geo))] = True
    if values["body"].strip():
        body = rig_weights.group_mask(NAME, geo, values["body"], "Body", report)
        if not body.any():
            raise UserError(format_problem(NAME, "the Body group %r has no point" % values["body"],
                                           "Name a point or primitive group of your mesh, or leave Body empty for every point"))
    else:
        body = np.ones(count, dtype=bool)
    body &= ~skip

    settings = Settings(distance=values["distance"], angle=values["angle"], minpiece=values["minpiece"])
    try:
        result = transfer_body(src_P, src_tris, src_found.weights, dst_P, dst_tris, body, settings)
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem))) from None

    existing = rig_weights.read_selections(NAME, geo, bones)
    weights = existing.weights.copy()
    touched = result.origin != ORIGIN_NONE
    weights[touched] = result.weights[touched]
    columns = [b for b in range(len(bones)) if b in existing.groups or (weights[:, b] > 0).any()]
    rig_weights.write_selections(geo, bones, weights, existing.groups, columns, False)
    rig_weights.write_point_int(geo, ORIGIN_ATTRIB, result.origin, 0, touched)
    _report(report, result, count, body, skip)
    kept_result.keep_geometry(node, report, key)


def _report(report: Report, result, count: int, body, skip) -> None:
    info = result.info
    line = "%d body points. First pass: %d matched, %d inpainted" % (int(body.sum()), info["matched"], info["filled"])
    if info["fallback"]:
        line += ", %d with no match anywhere near took their closest source weights" % info["fallback"]
    report.info(line + ".")
    report.info("Rejected as matches: %d too far, %d facing another way, %d on an unweighted source point."
                % (info["rejected_distance"], info["rejected_normal"], info["rejected_unweighted"]))
    left = count - int(body.sum()) - int(skip.sum())
    if left:
        report.info("%d points are outside the Body group and keep the weights they had." % left)


BUTTONS = {}
MENUS = {}

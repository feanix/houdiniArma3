# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no weight transfer. The method is Abdrashitov et al., "Robust Skin Weights Transfer via Weight Inpainting",
# SIGGRAPH Asia 2023 Technical Communications; the maths is in ``a3ob_core.rig.transfer``.
"""The Weight Transfer node (docs/design/character-rigging.md 2.1 to 2.5 and 6.2, task #65): copies the bone weights of
a source body (for example Bohemia's sample character) onto the user's mesh.

Input 1 is the user mesh, input 2 the source body with bone selections, input 3 the skeleton (it gives the bone names,
the spelling of the new selections and the bone chain for the twist merge). The new weights are written as bone
selections (point groups with ``a3ob_w_*``) in the ``model.cfg`` spelling; run Weight Cleanup after it. Layers:

- first pass: closest point match, then inpainting of the points without a match;
- rigid group: every connected piece gets one bone or one fixed blend;
- non-rigid group: per point from the first pass, smoothed along the piece.

The points get ``a3ob_rig_origin`` (0 no weight, 1 copied, 2 filled in, 3 rigid piece, 4 non-rigid piece) and
``a3ob_rig_piece`` (the piece number, -1 outside the pieces). A point string ``a3ob_rig_bone`` on a rigid piece names its
bone and wins over the choice.
"""

import numpy as np

from a3ob_core.rig import ORIGIN_NONE, Settings, transfer_layers, twist_targets

from .. import rig_weights
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms
from .weight_cleanup import _proxy_points

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Weight Transfer"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"
BONE_ATTRIB = "a3ob_rig_bone"

SPEC = NodeSpec(
    kind="weight_transfer",
    label="Weight Transfer",
    summary="Copies the bone weights of a source body (the sample character) onto your mesh, with layers for rigid and "
            "loose parts.",
    inputs=(3, 3),
    input_labels=("Your mesh", "Source body", "Skeleton"),
    input_help=("The mesh that gets the weights. It should lie on top of the source body (fit it first).",
                "The body with bone weights: bone selections (point groups with a3ob_w weights), for example the sample "
                "character from Extract LOD.",
                "The skeleton, for example from Rig Skeleton. Its joint names decide which selections are bones and how "
                "they are spelled; its hierarchy says where a twist bone's weight goes."),
    parms=(
        Parm("firstpass", "First Pass", "group", "",
             "The base mesh (for example the uniform). It gets its weights straight from the source body. Empty means "
             "every point that is in neither of the two groups below.", group_type="points"),
        Parm("rigid", "Rigid Parts", "group", "",
             "Parts that must not stretch (a helmet, a pouch). Each connected piece gets one bone, or one fixed blend, "
             "from the first-pass points it sits on.", group_type="points"),
        Parm("nonrigid", "Loose Parts", "group", "",
             "Parts that bend (a bandolier, a cable). Each point takes the weights of the first pass under it, and the "
             "weights are smoothed along the piece.", group_type="points"),
        Parm("distance", "Match Distance", "float", 0.05,
             "How far from the source body a point may be and still copy its weights, as a share of the size of your "
             "mesh (the diagonal of its bounding box). Points that are farther are filled in from their neighbours.",
             minimum=0.0),
        Parm("angle", "Match Angle", "float", 35.0,
             "How much the surface direction may differ, in degrees, for a point to copy weights. A point that faces "
             "another way (a sleeve against the torso) is filled in instead.", minimum=0.0, maximum=180.0),
        Parm("rigid_mode", "Rigid Parts Use", "menu", "dominant", "How a rigid piece is weighted.", items=(
            ("dominant", "Dominant bone"),
            ("blend", "Fixed blend"),
        ), item_help=(
            ("dominant", "The whole piece follows the one bone with the most weight under it (weight 1)."),
            ("blend", "Every point gets the same mix of bones, the average under the piece."),
        )),
        Parm("merge_twist", "Merge Twist Bones", "toggle", True,
             "For a rigid piece, the weight of a twist bone (a name ending in Roll) counts for the next bone down the "
             "chain that is not a twist, so a piece on the elbow follows the forearm. Only used with Dominant bone."),
        Parm("smooth", "Smoothing Passes", "int", 8,
             "How many times the weights of a loose part are averaged with their neighbours along the piece.",
             minimum=0, maximum=100),
        Parm("minpiece", "Smallest Piece", "int", 10,
             "A piece with fewer points than this is ignored: it gets no weights from the rigid or loose layer. Loose "
             "pieces of the source body this small (the sample's proxy triangles) are not used either.",
             minimum=1),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    skeleton = rig_weights.read_skeleton(NAME, node.inputGeometry(2))
    source = node.inputGeometry(1)
    if source is None:
        raise UserError(format_problem(NAME, "the source body is not connected", "Connect the source body to input 2"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    bones = skeleton.names
    src_found = rig_weights.read_selections(NAME, source, bones)
    if not src_found.groups:
        raise UserError(format_problem(NAME, "no selection of the source body is named like a joint of the skeleton",
                                       "Connect a body with bone weights to input 2 and its skeleton to input 3"))
    src_P, src_tris = _mesh(source)
    dst_P, dst_tris = _mesh(geo)
    if not len(src_tris):
        raise UserError(format_problem(NAME, "the source body has no polygons", "Use a body with polygons on input 2"))
    if not len(dst_tris):
        raise UserError(format_problem(NAME, "your mesh has no polygons", "Use a mesh with polygons on input 1"))
    count = len(dst_P)

    proxy = _proxy_points(geo)
    skip = np.zeros(count, dtype=bool)
    skip[sorted(proxy)] = True
    rigid = _group(geo, values["rigid"], "Rigid Parts") & ~skip
    nonrigid = _group(geo, values["nonrigid"], "Loose Parts") & ~rigid & ~skip
    if values["firstpass"].strip():
        first = _group(geo, values["firstpass"], "First Pass")
    else:
        first = np.ones(count, dtype=bool)
    first &= ~(rigid | nonrigid) & ~skip
    if not (first | rigid | nonrigid).any():
        raise UserError(format_problem(NAME, "no point is in a group", "Check the First Pass, Rigid Parts and Loose Parts parameters"))

    override = _override(geo, bones, report)
    settings = Settings(distance=values["distance"], angle=values["angle"], rigid_mode=values["rigid_mode"],
                        merge_twist=values["merge_twist"], smooth=values["smooth"], minpiece=values["minpiece"])
    twist = twist_targets(bones, skeleton.parents) if skeleton.parents else None
    try:
        result = transfer_layers(src_P, src_tris, src_found.weights, dst_P, dst_tris, first, rigid, nonrigid, twist,
                                 override, settings)
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem))) from None

    existing = rig_weights.read_selections(NAME, geo, bones)
    weights = existing.weights.copy()
    touched = result.origin != ORIGIN_NONE
    weights[touched] = result.weights[touched]
    columns = [b for b in range(len(bones)) if b in existing.groups or (weights[:, b] > 0).any()]
    rig_weights.write_selections(geo, bones, weights, existing.groups, columns, False)
    _write_int(geo, ORIGIN_ATTRIB, result.origin, 0)
    _write_int(geo, PIECE_ATTRIB, result.piece, -1)
    _report(report, result, bones, count, skip)


def _mesh(geo):
    """``(positions (n, 3), triangles (m, 3))`` of the polygons of a geometry (quads and larger are fans)."""
    positions = np.array(geo.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3)
    tris = []
    for prim in geo.prims():
        if isinstance(prim, hou.PackedPrim) or not isinstance(prim, hou.Polygon) or not prim.isClosed():
            continue
        ring = [v.point().number() for v in prim.vertices()]
        for k in range(1, len(ring) - 1):
            tris.append((ring[0], ring[k], ring[k + 1]))
    return positions, np.array(tris, dtype=np.int64).reshape(-1, 3)


def _group(geo, text: str, label: str) -> np.ndarray:
    """The points of a group parameter as a boolean mask (an empty text is no point)."""
    mask = np.zeros(len(geo.points()), dtype=bool)
    if not text.strip():
        return mask
    try:
        mask[[p.number() for p in geo.globPoints(text)]] = True
    except hou.OperationFailed:
        raise UserError(format_problem(NAME, "the group %r of %s cannot be read" % (text, label),
                                       "Use a point group name or point numbers")) from None
    return mask


def _override(geo, bones, report: Report):
    """The bone number named by ``a3ob_rig_bone`` for each point (-1 for none), or None when there is no attribute."""
    attrib = geo.findPointAttrib(BONE_ATTRIB)
    if attrib is None or attrib.dataType() != hou.attribData.String:
        return None
    index = {}
    for number, bone in enumerate(bones):
        index.setdefault(bone.lower(), number)
    out, unknown = [], set()
    for text in geo.pointStringAttribValues(BONE_ATTRIB):
        text = text.strip()
        if not text:
            out.append(-1)
        elif text.lower() in index:
            out.append(index[text.lower()])
        else:
            out.append(-1)
            unknown.add(text)
    if unknown:
        report.warning(format_problem(NAME, "%s in %s are not bones of the skeleton and were ignored" % (
            list_elements("names", sorted(unknown)), BONE_ATTRIB), "Use the bone names of the skeleton"))
    return np.array(out, dtype=np.int64)


def _write_int(geo, name: str, values, default: int) -> None:
    if geo.findPointAttrib(name) is None:
        geo.addAttrib(hou.attribType.Point, name, default)
    geo.setPointIntAttribValues(name, [int(v) for v in values])


def _report(report: Report, result, bones, count: int, skip) -> None:
    info = result.info
    line = "%d points. First pass: %d matched, %d inpainted" % (count - int(skip.sum()), info["matched"], info["filled"])
    if info["fallback"]:
        line += ", %d with no match anywhere near took their closest source weights" % info["fallback"]
    rigid = [p for p in result.pieces if p.layer == "rigid"]
    nonrigid = [p for p in result.pieces if p.layer == "nonrigid"]
    line += ". Rigid parts: %d piece%s. Loose parts: %d piece%s." % (
        len(rigid), "" if len(rigid) == 1 else "s", len(nonrigid), "" if len(nonrigid) == 1 else "s")
    report.info(line)
    report.info("Rejected as matches: %d too far, %d facing another way, %d on an unweighted source point."
                % (info["rejected_distance"], info["rejected_normal"], info["rejected_unweighted"]))
    for piece in rigid[:5]:
        if piece.how == "blend":
            report.info("Rigid piece %d: %d points, one fixed blend." % (piece.number, piece.points))
        elif piece.how != "none":
            report.info("Rigid piece %d: %d points, bone %s%s." % (piece.number, piece.points, bones[piece.bone],
                                                                   " (from a3ob_rig_bone)" if piece.how == "override" else ""))
    if len(rigid) > 5:
        report.info("and %d more rigid pieces." % (len(rigid) - 5))
    if info["ignored_pieces"]:
        report.info("%d pieces with fewer than the smallest piece size were ignored." % info["ignored_pieces"])
    if info["far_points"]:
        report.info("%d points of rigid pieces were too far from the first pass to count for the average."
                    % info["far_points"])
    if info["unweighted_pieces"]:
        report.warning(format_problem(NAME, "%d pieces found no first-pass weights under them" % info["unweighted_pieces"],
                                      "Move the piece onto the body or put the first pass under it"))
    missing = int(((result.origin == ORIGIN_NONE) & ~skip).sum())
    if missing:
        report.info("%d points are in no group and keep the weights they had." % missing)


BUTTONS = {}
MENUS = {}

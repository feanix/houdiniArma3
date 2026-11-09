# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no weight transfer. The rigid and non-rigid layers are written for this port (docs/design/character-rigging.md
# 2.5, 6.2); the first pass they build on is Abdrashitov et al., SIGGRAPH Asia 2023 (see ``a3ob_core.rig.transfer``).
"""The Accessory Weights node (docs/design/character-rigging.md 2.5 and 6.2, task #73, D41): weights the accessories of a
character (helmet, pouches, straps) from points that already have weights.

Input 1 is the mesh. One mesh holds both groups: the Target Group (the accessory points to weight) and the Source Group
(the points that give the weights: the body, or another accessory). Input 2 is the skeleton (Rig Skeleton, or the Rig Fit
output: its fitted skeleton is used). Modes:

- Rigid: each connected piece of the target gets the area-weighted average of the source weights near it, then one bone
  (Auto: the dominant bone, a twist bone counting for the next bone down the chain), one fixed blend, or the bone you pick.
- Non-rigid: each point takes the source weights under it, smoothed along the piece.

Chain: Weight Transfer (body), then one Accessory Weights per accessory, then Weight Cleanup. The points get
``a3ob_rig_origin`` (3 rigid piece, 4 non-rigid piece) and ``a3ob_rig_piece`` (the piece number, counted on after the pieces
of earlier nodes); points outside the target keep the values they had.
"""

import numpy as np

from a3ob_core.rig import ORIGIN_NONE, Settings, accessory_layer, twist_targets

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

NAME = "Accessory Weights"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"

SPEC = NodeSpec(
    kind="accessory_weights",
    label="Accessory Weights",
    summary="Weights accessories (a helmet, pouches, a strap) from points that already have weights: the body or another "
            "accessory.",
    inputs=(2, 2),
    input_labels=("Your mesh", "Skeleton"),
    input_help=("The mesh with the weighted body (from Weight Transfer) and the accessories. Both groups below are point "
                "groups of this mesh.",
                "The skeleton, for example from Rig Skeleton; the output of Rig Fit goes here as it is (its fitted "
                "skeleton is used). Its joint names decide which selections are bones and how they are spelled; its "
                "hierarchy says where a twist bone's weight goes."),
    parms=(
        Parm("target", "Target Group", "group", "",
             "The accessory points to weight. Required. A point group or a primitive group (its primitives' points "
             "count). Each connected piece of the group is one piece, or each name when Pieces From is Names.",
             group_type="points"),
        Parm("source", "Source Group", "group", "",
             "The points that give the weights (a point group or a primitive group): the body, or another accessory that already has weights. Points of "
             "it with no bone weight are ignored, with a warning. Not used when Rigid uses a picked bone.",
             group_type="points"),
        Parm("mode", "Mode", "menu", "rigid", "How the accessory is weighted.", items=(
            ("rigid", "Rigid"),
            ("nonrigid", "Non-rigid"),
        ), item_help=(
            ("rigid", "Every connected piece of the target follows one bone or one fixed blend (a helmet, a pouch)."),
            ("nonrigid", "Each point takes the weights of the source under it, smoothed along the piece (a bandolier, "
                         "a cable)."),
        )),
        Parm("use", "Rigid Uses", "menu", "auto", "How a rigid piece is weighted.", items=(
            ("auto", "Auto"),
            ("blend", "Fixed blend"),
            ("bone", "Bone"),
        ), item_help=(
            ("auto", "The whole piece follows the one bone with the most source weight under it (weight 1)."),
            ("blend", "Every point gets the same mix of bones, the average of the source under the piece."),
            ("bone", "Every piece follows the bone you pick below."),
        ), show_when=(("mode", ("rigid",)),)),
        Parm("bone", "Bone", "menu", "head", "The bone every rigid piece follows. The menu lists the bones of the "
             "skeleton on input 2.", menu_action="bones", show_when=(("mode", ("rigid",)), ("use", ("bone",)))),
        Parm("merge_twist", "Merge Twist Bones", "toggle", True,
             "The weight of a twist bone (a name ending in Roll) counts for the next bone down the chain that is not a "
             "twist, so a piece on the elbow follows the forearm.",
             show_when=(("mode", ("rigid",)), ("use", ("auto",)))),
        Parm("piecesfrom", "Pieces From", "menu", "names",
             "How a rigid target is cut into pieces. Non-rigid mode always uses connected pieces.", items=(
                 ("names", "Names"),
                 ("connected", "Connected Pieces"),
             ), item_help=(
                 ("names", "Polygons with the same name are one piece, even when the parts do not touch (a helmet, "
                           "its visor and its bolts). Polygons with no name, or a mesh without the name attribute, "
                           "use connected pieces, and so does a mesh where every polygon has the same name (the node "
                           "warns about that: give the parts their own names). A point "
                           "shared by two names goes to the name with most of its polygons."),
                 ("connected", "Every connected piece is weighted on its own."),
             ), show_when=(("mode", ("rigid",)),)),
        Parm("nameattr", "Name Attribute", "string", "name",
             "The primitive string attribute that holds the piece names. FBX, Alembic and most modelling tools write "
             "'name'.", show_when=(("mode", ("rigid",)), ("piecesfrom", ("names",)))),
        Parm("distance", "Match Distance", "float", 0.05,
             "How far from the source a target point may be and still count, as a share of the size of the target "
             "and source groups together (the diagonal of their bounding box). A point with no source this close takes the nearest weighted "
             "source point, with a warning.", minimum=0.0),
        Parm("angle", "Match Angle", "float", 35.0,
             "How much the surface direction may differ, in degrees, for a point to copy weights. A point that faces "
             "another way is filled in from its neighbours on the piece.", minimum=0.0, maximum=180.0,
             show_when=(("mode", ("nonrigid",)),)),
        Parm("smooth", "Smoothing Passes", "int", 8,
             "How many times the weights are averaged with their neighbours along the piece.", minimum=0, maximum=100,
             show_when=(("mode", ("nonrigid",)),)),
        Parm("minpiece", "Smallest Piece", "int", 10,
             "A piece of the target with fewer points than this is ignored: it gets no weights. A named piece counts "
             "all its parts together.", minimum=1),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values)
    if kept_result.replay_geometry(node, report, key):  # nothing changed since the last cook: the kept result (task #97)
        return
    skeleton = rig_weights.read_skeleton(
        NAME, rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_SKELETON, "input 2"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    bones = skeleton.names
    P, tris = rig_weights.read_mesh(geo)
    if not len(tris):
        raise UserError(format_problem(NAME, "your mesh has no polygons", "Use a mesh with polygons on input 1"))
    count = len(P)
    rigid = values["mode"] == "rigid"
    rigid_mode = {"auto": "dominant", "blend": "blend", "bone": "bone"}[values["use"]] if rigid else "dominant"

    proxy = np.zeros(count, dtype=bool)  # proxy points: not skin
    proxy[sorted(_proxy_points(geo))] = True
    target = _required_group(geo, values["target"], "Target Group", report) & ~proxy
    need_source = not (rigid and rigid_mode == "bone")
    source = _required_group(geo, values["source"], "Source Group", report) if need_source else np.zeros(count, dtype=bool)
    source &= ~proxy
    both = int((source & target).sum())
    source &= ~target
    existing = rig_weights.read_selections(NAME, geo, bones)
    weighted = existing.weights.sum(axis=1) > 0
    unweighted = int((source & ~weighted).sum())
    source &= weighted

    bone = -1
    if rigid_mode == "bone":
        bone = _bone_number(values["bone"], bones)
    if need_source and not source.any():
        raise UserError(format_problem(NAME, "no point of the Source Group has a bone weight",
                                       "Weight the source first (Weight Transfer or an earlier Accessory Weights) and "
                                       "check the skeleton on input 2"))
    settings = Settings(distance=values["distance"], angle=values["angle"], rigid_mode=rigid_mode,
                        merge_twist=values["merge_twist"], smooth=values["smooth"], minpiece=values["minpiece"])
    twist = twist_targets(bones, skeleton.parents) if skeleton.parents else None
    tri_names = None
    if rigid and values["piecesfrom"] == "names":
        tri_names = _triangle_names(geo, values["nameattr"].strip(), report)
    try:
        result = accessory_layer(P, tris, existing.weights, source, target, "rigid" if rigid else "nonrigid", twist, bone,
                                 settings, tri_names)
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem))) from None

    weights = existing.weights.copy()
    touched = result.origin != ORIGIN_NONE
    weights[touched] = result.weights[touched]
    columns = [b for b in range(len(bones)) if b in existing.groups or (weights[:, b] > 0).any()]
    rig_weights.write_selections(geo, bones, weights, existing.groups, columns, False)
    offset = 0
    if geo.findPointAttrib(PIECE_ATTRIB) is not None:
        offset = max(0, int(max(geo.pointIntAttribValues(PIECE_ATTRIB), default=-1)) + 1)
    rig_weights.write_point_int(geo, ORIGIN_ATTRIB, result.origin, 0, touched)
    rig_weights.write_point_int(geo, PIECE_ATTRIB, np.where(result.piece >= 0, result.piece + offset, -1), -1,
                                result.piece >= 0)
    _report(report, result, bones, int(target.sum()), offset, both, unweighted)
    kept_result.keep_geometry(node, report, key)


def _triangle_names(geo, attrib: str, report: Report):
    """The piece name of each triangle, in the order of ``rig_weights.read_mesh`` (same polygons, same fans). None when
    the primitive string attribute is missing, no polygon has a name, or all polygons share one name (connected pieces
    then; an importer that names the whole mesh must not glue every accessory into one piece)."""
    found = geo.findPrimAttrib(attrib) if attrib else None
    if found is None or found.dataType() != hou.attribData.String:
        report.info("No primitive string attribute %r: rigid pieces are the connected pieces." % attrib)
        return None
    _, counts = rig_weights.prim_rings(geo)
    closed = rig_weights.closed_polygons(geo, len(counts))
    names = np.repeat(np.array(geo.primStringAttribValues(attrib), dtype=object),
                      np.where(closed, np.maximum(counts - 2, 0), 0)).tolist()  # one name per triangle of read_mesh
    if not any(names):
        report.info("The attribute %r has no name: rigid pieces are the connected pieces." % attrib)
        return None
    if len(set(names)) == 1:  # one name on every polygon (a whole-mesh import name) says nothing about parts
        report.warning(format_problem(NAME, "every polygon has %s = %r, so the names are ignored and connected pieces are used"
                                      % (attrib, names[0]),
                                      "Give the parts their own names in %s to keep each part whole" % attrib))
        return None
    return names


def _required_group(geo, text: str, label: str, report=None) -> np.ndarray:
    """A group parameter as a mask; an empty text or a group with no point is an error that names the field."""
    if not text.strip():
        raise UserError(format_problem(NAME, "%s is empty" % label,
                                       "Fill in %s with a point or primitive group (use the select button)" % label))
    mask = rig_weights.group_mask(NAME, geo, text, label, report)
    if not mask.any():
        raise UserError(format_problem(NAME, "%s %r has no point" % (label, text),
                                       "Name a point or primitive group of your mesh in %s" % label))
    return mask


def _bone_number(text: str, bones) -> int:
    for number, name in enumerate(bones):
        if name.lower() == text.strip().lower():
            return number
    raise UserError(format_problem(NAME, "Bone %r is not a bone of the skeleton" % text,
                                   "Pick a bone from the Bone menu" if text.strip() else "Pick a bone in the Bone field"))


def _menu_bones(kwargs) -> list:
    """The Bone menu: the joint names of the skeleton on input 2 (a Rig Fit output gives its fitted skeleton)."""
    inputs = kwargs["node"].inputs()
    if len(inputs) < 2 or inputs[1] is None:
        return []
    geo = rig_weights.read_part(NAME, inputs[1].geometry(), rig_weights.PART_SKELETON, "input 2")
    if geo is None or geo.findPointAttrib("name") is None:
        return []
    return [(p.attribValue("name"), p.attribValue("name")) for p in geo.points()]


def _report(report: Report, result, bones, count: int, offset: int, both: int, unweighted: int) -> None:
    info = result.info
    rigid = [p for p in result.pieces if p.layer == "rigid"]
    nonrigid = [p for p in result.pieces if p.layer == "nonrigid"]
    if rigid:
        report.info("%d target points. Rigid: %d piece%s." % (count, len(rigid), "" if len(rigid) == 1 else "s"))
    else:
        report.info("%d target points. Non-rigid: %d piece%s; %d matched, %d filled in." % (
            count, len(nonrigid), "" if len(nonrigid) == 1 else "s", info["matched"], info["filled"]))
    shown = 12 if any(p.name for p in rigid) else 5
    for piece in rigid[:shown]:
        number = piece.name or str(piece.number + offset)  # a named piece is told by its name
        if piece.how == "blend":
            report.info("Rigid piece %s: %d points, one fixed blend." % (number, piece.points))
        elif piece.how != "none":
            report.info("Rigid piece %s: %d points, bone %s%s." % (number, piece.points, bones[piece.bone],
                                                                   " (picked)" if piece.how == "chosen" else ""))
    if len(rigid) > shown:
        report.info("and %d more rigid pieces." % (len(rigid) - shown))
    if info["ignored_pieces"]:
        report.info("%d pieces with fewer than the smallest piece size were ignored." % info["ignored_pieces"])
    if info["far_points"]:
        report.info("%d points of rigid pieces were too far from the source to count for the average." % info["far_points"])
    if both:
        report.info("%d points are in both groups; they are weighted as target." % both)
    if unweighted:
        report.warning(format_problem(NAME, "%d points of the Source Group have no bone weight and were ignored" % unweighted,
                                      "Weight them first, or take them out of the Source Group"))
    if info["fallback"]:
        report.warning(format_problem(NAME, "%d target points have no source point within the distance limit and took the "
                                            "nearest weighted source point" % info["fallback"],
                                      "Move the accessory onto its source, or raise Match Distance"))
    if info["unweighted_pieces"]:
        report.warning(format_problem(NAME, "%d pieces found no source weights under them" % info["unweighted_pieces"],
                                      "Move the piece onto its source or change the Source Group"))


BUTTONS = {}
MENUS = {"bones": _menu_bones}

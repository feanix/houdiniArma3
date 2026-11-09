# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no unpose; the maths is ``a3ob_core.rig.unpose``.
"""The Unpose node (docs/design/character-rigging.md 2.6 and 6.2, task #67): moves the weighted mesh from the pose of the
fitted skeleton back onto the Arma rest pose.

Input 1 is the mesh with cleaned weights (Weight Cleanup), input 2 the output of Rig Fit (its ``fitted_skeleton``,
``fitted_body`` and ``rest_body`` parts are used, D40), input 3 the Arma skeleton (Rig Skeleton). The weights are not
changed (D37 Q13).

Method **Cage** (default, D44, task #80): the body and the accessories that bend move by a cage, the maths of the Point
Deform SOP as in the advisor's prototype /obj/unpose_proto (ported to numpy: Houdini has no verb for that SOP, so it
cannot run from this Python SOP; the port is checked against the real node) with the rest lattice = Rig Fit's ``fitted_body`` (the sample
body wrapped onto your body) and the deformed lattice = ``rest_body`` (the sample body in the Arma rest pose). Both have
their proxy markers removed the same way, so their points match one to one. Rest body: a part of the Rig Fit output rather
than a fourth input, because the part always comes from the same sample body as the fitted one, so the point order cannot
be wrong, and the node needs no more wires. Rigid pieces (``a3ob_rig_piece`` and ``a3ob_rig_origin`` of Accessory Weights
in Rigid mode, D41, and islands outside the Body Group that follow one bone only, such as a helmet) are then moved by one
rigid transform each (rotation and shift, no size): the one that fits the places the cage gives their points best, so the
piece stays on the body where the cage puts it (the helmet follows the head) and none of its edges changes.

The other points outside the Body Group (clothes that bend, loose gear: task #82) are not bent by the cage. Each keeps its
offset from its closest point on the wrapped body (``fitted_body``) and moves with that place of the surface to the same place
of ``rest_body`` (``cage.surface_follow``), turned by the rotation the cage has there; the moves of the loose points within 50 cm
are averaged, so that a piece that sits tight on a surface that is stretched (hands, collar) is not stretched with it.

**One-bone pieces at their joint** (task #91). The sample body is headless, so the cage above its neck only extrapolates the
move of its top ring: a helmet came out 11.5 cm low and 9.6 cm forward of the joint it turns about. A rigid piece that follows
one bone and stands beyond the cage (median distance to the wrapped body over 10 cm) is put at the joint instead: it keeps the
offset to the joint of its bone that it has in the fitted pose (``cage.anchor_pieces``); the head, held by a shift, takes the
move of the head joint. The body on the stem of that bone (head, neck1, neck, spine3, spine2; ``cage.stem_share``) is drawn to
where it would be if it moved with the piece, so the neck still meets the helmet and the shoulders stay with the cage. Task #98:
the fade down the stem is ``Neck Fade Length`` long (default 30 cm), and within 4 cm sideways of the stem the front-back offset
counts 0.4 times (``cage.STEM_DEPTH``), because the stem lies deep in the body (neck edges p99 2.0 to 1.5 times their posed
length on the owner scene; docs/design/character-rigging.md).

Two repairs on the cage result (task #84). **Fingers Keep Their Shape**: the cage lets every lattice point within 10 cm pull a
point, so in a hand the lattice of a neighbouring finger squeezed and widened the owner's fingers (middle segment 2 thickness /
width 1.05 to 0.67 on the owner scene). Each finger segment is moved as a rigid piece instead (``cage.keep_finger_shape``): the
rigid transform that takes the segment of the wrapped body to the segment of the sample, blended by the mesh's own bone weights
(the palm follows the cage). **Soles Keep Their Tilt**: the points of the wrapped foot correspond to the sample's with a roll of
3 to 4 degrees about the foot although both soles lie flat, so the cage rolled the feet onto their outer edges by 5 to 6 degrees;
each foot is turned back about its forward axis by the roll that was added (``cage.keep_sole_tilt``).

Method **Bones** (the old one): each point moves by the inverse of its blended bone matrix, from the fitted skeleton to the
Arma one; a rigid piece by one matrix. Neither method changes the overall size.
"""

import numpy as np

from a3ob_core.rig.cage import STEM_DEPTH
from a3ob_core.rig import (ORIGIN_RIGID, anchor_pieces, beyond_cage, bone_displacements, bone_moves, carry_body, finger_columns,
                           hold_orientation, keep_finger_shape, keep_sole_tilt, piece_bones, place_pieces, point_deform,
                           single_bone_pieces, stem_share, surface_follow, unpose_points)

from .. import kept_result, rig_weights
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms
from .weight_cleanup import _proxy_points

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Unpose"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"

SPEC = NodeSpec(
    kind="unpose",
    label="Unpose",
    summary="Moves your weighted mesh from the pose of the fitted skeleton back onto the Arma rest pose, by a cage "
            "from your body to the sample body.",
    inputs=(3, 3),
    input_labels=("Your mesh", "Fitted skeleton", "Arma skeleton"),
    input_help=("Your mesh with its cleaned bone weights, for example from Weight Cleanup.",
                "The output of Rig Fit as it is (its fitted skeleton, fitted body and rest body are used).",
                "The Arma skeleton, for example from Rig Skeleton. It must have the same bones as the fitted one."),
    parms=(
        Parm("method", "Method", "menu", "cage",
             "How the points move back to the Arma rest pose. Neither method changes the overall size.",
             items=(("cage", "Cage"), ("bones", "Bones")),
             item_help=(("cage", "Move the points by a cage: the sample body wrapped onto your body (fitted body of Rig Fit) "
                                 "is taken to the sample body in the Arma rest pose (Point Deform). It follows your body's "
                                 "proportions. Rigid pieces then move by one rigid transform each, and loose clothes and gear "
                                 "(points outside the Body Group) are carried by the wrapped body's surface at their own "
                                 "distance."),
                        ("bones", "Move each point by the inverse of its blended bone matrix, from the fitted skeleton to the "
                                  "Arma one. Fast, but it cannot express that your body has other proportions than the "
                                  "sample (thighs can telescope, hands crumple)."))),
        Parm("bodygroup", "Body Group", "group", "body",
             "Cage method: the point group or primitive group of your mesh that is the bare body, the one Rig Fit wrapped onto. Its points "
             "always follow the cage. A piece outside it whose points follow one bone only (a helmet) is moved as a rigid "
             "piece; other points outside it (clothes that bend) keep their distance from the closest point of the "
             "wrapped body and move with that place of its surface. Empty means the whole mesh is body.",
             group_type="points"),
        Parm("rigid_pieces", "Rigid Pieces Keep Their Shape", "toggle", True,
             "Move all the points of a rigid piece (made by Accessory Weights in Rigid mode, or an island outside the Body "
             "Group that follows one bone only) with one rigid transform, so the piece keeps its shape. A piece of one bone "
             "that stands beyond the cage (a helmet above the neck of the headless sample) is put at the joint of its bone: "
             "it keeps its offset to the joint, and the neck is carried along. Off: every point follows the cage (or its "
             "own weights in the Bones method), which can stretch a rigid piece."),
        Parm("keephead", "Head Keeps Its Tilt", "toggle", True,
             "Cage method: move the head (the points that follow the head bone, with the rigid pieces that follow it, such as a "
             "helmet) by a shift only, so it keeps the tilt it has in your mesh. The cage would turn it with the neck of the sample "
             "body, which is not your head's tilt (the sample body has no head to say what that is). The shift is the one the "
             "cage gives the base of the head; for a head beyond the cage (nothing of the sample there to follow) it is the "
             "move of the head joint, so the head keeps its offset to the joint, and the neck is carried along with it. Off: "
             "the head follows the cage."),
        Parm("keepfingers", "Fingers Keep Their Shape", "toggle", True,
             "Cage method: move each finger segment (the points that follow a finger bone) by one rigid transform, the one that "
             "takes the segment of the wrapped body to the segment of the sample body, and blend the segments by your mesh's bone "
             "weights (the palm follows the cage). The cage alone lets the lattice of the neighbouring fingers pull a finger "
             "(fingers of your mesh are packed, the sample's are coarse), which flattens and widens them. Off: the fingers follow "
             "the cage."),
        Parm("keepsoles", "Soles Keep Their Tilt", "toggle", True,
             "Cage method: turn each foot back about its forward axis by the sideways roll that the cage added, so the sole "
             "keeps the sideways tilt it has in your mesh (the cage rolled the feet onto their outer edges by 5 to 6 degrees, "
             "though the wrapped and the sample soles are both flat). Pitch and toe-out are not changed. Off: the feet follow "
             "the cage."),
        Parm("neckfade", "Neck Fade Length (cm)", "float", 30.0,
             "Cage method: how far down the neck and chest the body is carried along with the head. The head is put at its joint, "
             "and the neck that leaves it moves with it. From the neck joint downwards the carrying fades out over this "
             "length, and the neck and chest are stretched there to meet the cage again. A longer fade spreads the stretch "
             "over more of the neck and chest; too long a fade carries the chest and the base of the shoulders and makes "
             "the stretch worse beside the neck. On the owner scene 20 to 30 cm worked best (the old fade was 24 cm). "
             "Shoulders, arms and the rest of the body do not follow the neck, whatever the length.",
             minimum=1.0, maximum=100.0),
        Parm("group", "Group", "group", "",
             "The points to unpose. Empty means all points. A point group or a primitive group (its primitives' "
             "points count) works. The menu lists your point and primitive groups of the input and shows the "
             "P3D name of a selection; picking adds or removes the group. The arrow button picks points in the "
             "viewport. You can also type group names, patterns or point numbers (for example 0-3).",
             group_type="points"),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values)
    if kept_result.replay_geometry(node, report, key):  # nothing changed since the last cook: the kept result (task #97)
        return
    if node.inputGeometry(1) is None or node.inputGeometry(2) is None:
        raise UserError(format_problem(NAME, "an input is not connected", "Connect the output of Rig Fit to input 2 and "
                                       "the Arma skeleton to input 3"))
    fitted = rig_weights.read_skeleton(
        NAME, rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_SKELETON, "input 2"))
    arma = rig_weights.read_skeleton(NAME, node.inputGeometry(2))
    names = arma.names
    if [n.lower() for n in fitted.names] != [n.lower() for n in names]:
        raise UserError(format_problem(NAME, "the fitted skeleton (input 2) and the Arma skeleton (input 3) do not have the "
                                       "same bones", "Connect the Rig Fit that was made from this Arma skeleton"))
    if len(names) > 1 and not any(p >= 0 for p in arma.parents):
        raise UserError(format_problem(NAME, "the Arma skeleton has no bone lines between its joints",
                                       "Connect a Rig Skeleton to input 3"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    found = rig_weights.read_selections(NAME, geo, names)
    if not found.groups:
        report.warning(format_problem(NAME, "no selection of the mesh is named like a joint of the skeleton",
                                      "Check that input 3 is the skeleton of this character, and weight the mesh first"))
    count = len(geo.points())
    positions = np.array(geo.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3)
    active = rig_weights.group_mask(NAME, geo, values["group"], "Group", report) if values["group"].strip() else np.ones(count, bool)
    proxy = np.zeros(count, dtype=bool)  # proxy points (used only by packed prims) are not part of the mesh
    proxy[sorted(_proxy_points(geo))] = True
    active &= ~proxy

    piece = _rigid_pieces(geo, values["rigid_pieces"], count)
    weights = found.weights
    weighted = weights.sum(axis=1) > 0
    index = np.nonzero(active)[0]
    moved = positions.copy()
    moves = None
    if values["method"] == "cage":
        if len(index):
            if values["rigid_pieces"]:
                piece = _add_islands(geo, values["bodygroup"], weights, piece, active, count)
            caged, rest, deformed, polys, lattice_weights = _cage(geo, node, positions, names)
            placed = place_pieces(positions, caged, np.where(active, piece, -1))
            fitted_joints, arma_joints = np.array(fitted.positions), np.array(arma.positions)
            anchored, turns, joint_move = {}, {}, None
            if values["rigid_pieces"]:  # a piece that follows one bone keeps its offset to that bone's joint (task #91)
                # the head's piece (the helmet) always keeps its offset; any other piece only when it is beyond the cage (task #99)
                head_column = [n.lower() for n in names].index("head") if "head" in [n.lower() for n in names] else -1
                anchored = {number: bone for number, bone in piece_bones(weights, piece, active).items()
                            if bone == head_column or beyond_cage(positions[active & (piece == number)], rest)}
                placed, turns = anchor_pieces(positions, placed, np.where(active, piece, -1), anchored, fitted_joints, arma_joints)
            loose = active & (piece < 0) & ~_body_mask(geo, values["bodygroup"], count, report)
            if loose.any():  # clothes and loose gear: carried by the body surface at their own distance (task #82)
                placed[loose] = surface_follow(positions[loose], rest, deformed, polys)
            followers = active & ~loose & (piece < 0)
            moved_fingers = followers & False
            if values["keepfingers"]:
                columns = finger_columns(names)
                shaped = keep_finger_shape(positions, placed, weights, columns, rest, deformed, lattice_weights,
                                          _polygon_edges(geo))
                moved_fingers = followers & (weights[:, columns].sum(axis=1) > 0) if columns else followers & False
                placed[moved_fingers] = shaped[moved_fingers]
                if moved_fingers.any():
                    report.info("Fingers: %d points moved segment by segment (each finger keeps its shape)." % int(moved_fingers.sum()))
            if values["keepsoles"]:
                _keep_soles(report, names, positions, placed, weights, followers & _body_mask(geo, values["bodygroup"], count),
                            followers)
            held = np.zeros(count, dtype=bool)
            if values["keephead"]:
                held = _head_points(names, weights, active & ~loose, piece)
                if held.any():
                    column = [n.lower() for n in names].index("head")
                    # beyond the cage (a headless sample): the move of the head's joint, so the head keeps its offset to it (task #91)
                    head_piece = column in anchored.values()  # a helmet: it keeps its joint offset whatever the cage says (task #99)
                    joint_move = arma_joints[column] - fitted_joints[column] if head_piece or beyond_cage(positions[held], rest) else None
                    placed = hold_orientation(positions, placed, held, _seam_points(geo, held, active), joint_move)
                    report.info("Head: %d points keep their tilt (moved by a shift only%s)." % (
                        int(held.sum()), ", the move of the head joint" if joint_move is not None else ""))
            fade = values["neckfade"] / 100.0  # metres
            groups = []  # the places the body would have if it moved with the pieces put at their joints (task #91)
            lower = [n.lower() for n in names]
            if joint_move is not None:  # the head: a shift
                bone = lower.index("head")
                groups.append((stem_share(positions, fitted_joints, arma.parents, bone, fade=fade, depth=STEM_DEPTH), positions + joint_move))
            for number, bone in anchored.items():
                if not (held & (piece == number)).any():
                    target = arma_joints[bone] + (positions - fitted_joints[bone]) @ turns[number].T
                    groups.append((stem_share(positions, fitted_joints, arma.parents, bone, fade=fade, depth=STEM_DEPTH), target))
            if groups:
                placed = carry_body(placed, followers & ~held & ~moved_fingers, groups)
                report.info("Body on the stem of %d piece group%s put at the joint is carried along with it." % (
                    len(groups), "" if len(groups) == 1 else "s"))
            moved[index] = placed[index]
            geo.setPointFloatAttribValues("P", [float(v) for v in moved.reshape(-1)])
    else:
        moves = bone_moves(arma.parents, np.array(arma.positions), np.array(fitted.positions))
        if len(index):
            moved[index] = unpose_points(positions[index], weights[index], moves, piece[index])
            geo.setPointFloatAttribValues("P", [float(v) for v in moved.reshape(-1)])
    _report(report, names, positions, moved, weights, index, piece, int((active & ~weighted).sum()), moves)
    kept_result.keep_geometry(node, report, key)


def _body_mask(geo, group: str, count: int, report=None) -> np.ndarray:
    return rig_weights.group_mask(NAME, geo, group, "Body Group", report) if group.strip() else np.ones(count, bool)


def _keep_soles(report: Report, names, positions: np.ndarray, placed: np.ndarray, weights: np.ndarray, body: np.ndarray,
                followers: np.ndarray) -> None:
    """Turn each foot back by the roll the cage added (``keep_sole_tilt``). ``body`` are the body points that follow the cage
    (the feet are found on them), ``followers`` all the points that may be turned."""
    lower = [n.lower() for n in names]
    total = weights.sum(axis=1)
    for side, tag in ((1, "left"), (-1, "right")):
        columns = [lower.index(tag + bone) for bone in ("foot", "toebase") if tag + bone in lower]
        if not columns:
            continue
        share = np.where(total > 0, weights[:, columns].sum(axis=1) / np.maximum(total, 1e-12), 0.0)
        share = np.where(followers, share, 0.0)
        turned = keep_sole_tilt(positions, placed, share, side, body)
        if not np.array_equal(turned, placed):
            report.info("Soles: the %s foot turned back about its forward axis (the cage had rolled it)." % tag)
            placed[:] = turned


def _cage(geo, node, positions: np.ndarray, names=()):
    """``(caged, rest lattice, deformed lattice, lattice polygons, lattice weights)``: every point of ``geo`` moved by the cage
    (rest lattice ``fitted_body``, deformed lattice ``rest_body``, both without proxy markers so the point order matches; the
    maths of the prototype's Point Deform node, ``point_deform``), the two lattices for the loose accessories, and the bone
    weights of the rest lattice points (None when it has none) for the finger segments."""
    rest = rig_weights.without_markers(rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_BODY, "input 2"))
    deformed = rig_weights.without_markers(
        rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_REST_BODY, "input 2"))
    if len(rest.points()) != len(deformed.points()):
        raise UserError(format_problem(NAME, "the fitted body (%d points) and the rest body (%d points) of Rig Fit do not "
                                       "match" % (len(rest.points()), len(deformed.points())),
                                       "Cook Rig Fit again; use the Rig Fit output as it is"))
    polys = rig_weights.polygon_rings(rest)
    lattice = np.array(rest.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3)
    target = np.array(deformed.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3)
    found = rig_weights.read_selections(NAME, rest, names) if len(names) else None
    lattice_weights = found.weights if found is not None and found.groups else None
    return point_deform(positions, lattice, target, polys), lattice, target, polys, lattice_weights


def _polygon_edges(geo) -> np.ndarray:
    """The edges (point number pairs, (m, 2)) of the polygons of ``geo``; a packed primitive has none."""
    return rig_weights.polygon_edges(geo)


def _add_islands(geo, group: str, weights: np.ndarray, piece: np.ndarray, active: np.ndarray, count: int) -> np.ndarray:
    """``piece`` plus a new piece for each island outside the body group whose points follow one bone only."""
    body = _body_mask(geo, group, count)
    first = int(piece.max()) + 1 if (piece >= 0).any() else 0
    found = single_bone_pieces(_polygon_edges(geo), weights, body | (piece >= 0) | ~active, first)
    return np.where(found >= 0, found, piece)


#: A point belongs to the head when at least this share of its weight is on the head bone.
HEAD_SHARE = 0.5


def _head_points(names, weights: np.ndarray, active: np.ndarray, piece: np.ndarray) -> np.ndarray:
    """The points that keep the head's tilt (a bool per point): those whose weight is mostly on the ``head`` bone, and every
    point of a rigid piece whose weight is mostly on it (a helmet). Nothing when the skeleton has no head bone."""
    bones = [i for i, name in enumerate(names) if name.lower() == "head"]
    total = weights.sum(axis=1)
    held = np.zeros(len(weights), dtype=bool)
    if not bones:
        return held
    share = np.where(total > 0, weights[:, bones[0]] / np.maximum(total, 1e-12), 0.0)
    held = active & (share >= HEAD_SHARE)
    for number in np.unique(piece[active & (piece >= 0)]):
        members = active & (piece == number)
        if float(share[members].mean()) >= HEAD_SHARE:
            held |= members
    return held


def _seam_points(geo, held: np.ndarray, active: np.ndarray) -> np.ndarray:
    """The held points that share an edge with a point that is not held (the base of the head)."""
    edges = _polygon_edges(geo)
    seam = np.zeros(len(held), dtype=bool)
    if len(edges):
        a, b = edges[:, 0], edges[:, 1]
        across = (held[a] != held[b]) & active[a] & active[b]
        seam[a[across & held[a]]] = True
        seam[b[across & held[b]]] = True
    return seam


def _rigid_pieces(geo, wanted: bool, count: int) -> np.ndarray:
    """The rigid piece number of every point (-1 for none): the points Accessory Weights made in Rigid mode."""
    piece = -np.ones(count, dtype=np.int64)
    if wanted and geo.findPointAttrib(PIECE_ATTRIB) is not None and geo.findPointAttrib(ORIGIN_ATTRIB) is not None:
        numbers = np.array(geo.pointIntAttribValues(PIECE_ATTRIB), dtype=np.int64)
        origin = np.array(geo.pointIntAttribValues(ORIGIN_ATTRIB), dtype=np.int64)
        rigid = (origin == ORIGIN_RIGID) & (numbers >= 0)
        piece[rigid] = numbers[rigid]
    return piece


def _report(report: Report, names, before, after, weights, index, piece, unweighted: int, moves) -> None:
    """``moves`` is None for the Cage method."""
    if not len(index):
        report.warning(format_problem(NAME, "no point to unpose", "Check the Group field"))
        return
    shift = np.linalg.norm(after[index] - before[index], axis=1)
    cage = moves is None
    worst = int(index[int(shift.argmax())])
    report.info("%d points moved back to the Arma rest pose: %.2f cm on average, %.2f cm at most (point %d)." % (
        len(index), 100 * float(shift.mean()), 100 * float(shift.max()), worst))
    top = bone_displacements(before[index], after[index], weights[index], len(names))
    if top:
        report.info("Bones whose points moved most (mean): %s." % ", ".join(
            "%s %.2f cm (%d points)" % (names[bone], 100 * mean, points) for bone, mean, points in top))
    pieces = len(np.unique(piece[index][piece[index] >= 0]))
    if pieces:
        report.info("%d rigid piece%s moved with one %s each." % (
            pieces, "" if pieces == 1 else "s", "rigid transform" if cage else "matrix"))
    if unweighted and not cage:
        report.warning(format_problem(NAME, "%d points have no bone weight and followed the closest weighted point" % unweighted,
                                      "Weight them first, or take them out of the Group"))
    odd = [] if cage else [names[j] for j in range(len(names)) if not 0.5 <= moves.along[j] <= 2.0]
    if odd:
        report.warning(format_problem(NAME, "%s change length by more than a factor of 2 from the Arma skeleton" % list_elements(
            "bones", odd), "Check the landmarks of Rig Fit"))


BUTTONS = {}
MENUS = {}

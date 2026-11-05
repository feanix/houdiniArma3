# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no unpose; the maths is ``a3ob_core.rig.unpose``.
"""The Unpose node (docs/design/character-rigging.md 2.6 and 6.2, task #67): moves the weighted mesh from the pose of the
fitted skeleton back onto the Arma rest pose.

Input 1 is the mesh with cleaned weights (Weight Cleanup), input 2 the output of Rig Fit (its ``fitted_skeleton`` part is
used, D40), input 3 the Arma skeleton (Rig Skeleton). Each point moves by the inverse of its blended bone matrix, from the
fitted skeleton to the Arma one. The points of a rigid piece (``a3ob_rig_piece`` and ``a3ob_rig_origin`` of Accessory
Weights in Rigid mode, D41) move by one matrix per piece. The weights are not changed (D37 Q13).
"""

import numpy as np

from a3ob_core.rig import ORIGIN_RIGID, bone_displacements, bone_moves, unpose_points

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

NAME = "Unpose"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"

SPEC = NodeSpec(
    kind="unpose",
    label="Unpose",
    summary="Moves your weighted mesh from the pose of the fitted skeleton back onto the Arma rest pose.",
    inputs=(3, 3),
    input_labels=("Your mesh", "Fitted skeleton", "Arma skeleton"),
    input_help=("Your mesh with its cleaned bone weights, for example from Weight Cleanup.",
                "The output of Rig Fit as it is (its fitted skeleton is used).",
                "The Arma skeleton, for example from Rig Skeleton. It must have the same bones as the fitted one."),
    parms=(
        Parm("rigid_pieces", "Rigid Pieces Keep Their Shape", "toggle", True,
             "Move all the points of a rigid piece (made by Accessory Weights in Rigid mode) with one matrix, "
             "so the piece keeps its shape. Off: every point uses its own weights, which can stretch a rigid piece "
             "when its bone bends."),
        Parm("group", "Group", "group", "",
             "The points to unpose. Empty means all points. The menu lists the point groups of the input and shows the "
             "P3D name of a selection; picking adds or removes the group. The arrow button picks points in the "
             "viewport. You can also type group names, patterns or point numbers (for example 0-3).",
             group_type="points"),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
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
    active = rig_weights.group_mask(NAME, geo, values["group"], "Group") if values["group"].strip() else np.ones(count, bool)
    proxy = np.zeros(count, dtype=bool)  # proxy points (used only by packed prims) are not part of the mesh
    proxy[sorted(_proxy_points(geo))] = True
    active &= ~proxy

    piece = _rigid_pieces(geo, values["rigid_pieces"], count)
    moves = bone_moves(arma.parents, np.array(arma.positions), np.array(fitted.positions))
    weights = found.weights
    weighted = weights.sum(axis=1) > 0
    index = np.nonzero(active)[0]
    moved = positions.copy()
    if len(index):
        moved[index] = unpose_points(positions[index], weights[index], moves, piece[index])
        geo.setPointFloatAttribValues("P", [float(v) for v in moved.reshape(-1)])
    _report(report, names, positions, moved, weights, index, piece, int((active & ~weighted).sum()), moves)


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
    if not len(index):
        report.warning(format_problem(NAME, "no point to unpose", "Check the Group field"))
        return
    shift = np.linalg.norm(after[index] - before[index], axis=1)
    worst = int(index[int(shift.argmax())])
    report.info("%d points moved back to the Arma rest pose: %.2f cm on average, %.2f cm at most (point %d)." % (
        len(index), 100 * float(shift.mean()), 100 * float(shift.max()), worst))
    top = bone_displacements(before[index], after[index], weights[index], len(names))
    if top:
        report.info("Bones whose points moved most (mean): %s." % ", ".join(
            "%s %.2f cm (%d points)" % (names[bone], 100 * mean, points) for bone, mean, points in top))
    pieces = len(np.unique(piece[index][piece[index] >= 0]))
    if pieces:
        report.info("%d rigid piece%s moved with one matrix each." % (pieces, "" if pieces == 1 else "s"))
    if unweighted:
        report.warning(format_problem(NAME, "%d points have no bone weight and followed the closest weighted point" % unweighted,
                                      "Weight them first, or take them out of the Group"))
    odd = [names[j] for j in range(len(names)) if not 0.5 <= moves.along[j] <= 2.0]
    if odd:
        report.warning(format_problem(NAME, "%s change length by more than a factor of 2 from the Arma skeleton" % list_elements(
            "bones", odd), "Check the landmarks of Rig Fit"))


BUTTONS = {}
MENUS = {}

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the maths is in ``a3ob_core.rig.landmarks`` and ``a3ob_core.rig.fit``.
"""The Rig Fit node (docs/design/character-rigging.md 1.5, 2.6 and 6.2, task #66): fits the Arma skeleton and the sample
body to the user's mesh.

Input 1 is the user's mesh in an A-pose (Y up, facing +Z like the sample), input 2 the Arma skeleton (Rig Skeleton), input 3
the sample body with bone weights. The landmarks come from the Rig Landmarks node in front of this one (hand-placed
positions, task #74, D42), from a guess, or both. Then the skeleton is fitted down the hierarchy and the sample body is
posed with it. The Arma skeleton itself is never changed (D37 Q7): the fitted skeleton ``A'`` is a working copy, for Weight Transfer and Unpose.

The one output holds all three results as packed primitives (D40; a Python SOP writes only output 0). The prim string
``a3ob_rig_part`` names each: ``fitted_skeleton``, ``fitted_body``, ``landmarks``. The Show menu only hides parts in the
viewport. ``rig_weights.read_part`` takes a part out again.
"""

import numpy as np

from a3ob_core.rig import (BONE_OF, LANDMARK_NAMES, body_scale, facing_sign, fit_skeleton, joint_targets, skin_points,
                           snap_to_surface, symmetrize)

from .. import landmark_io, rig_weights
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Rig Fit"
#: A fit that leaves a joint farther than this from its landmark (metres) is warned about (design test 2.4).
WARN_DISTANCE = 0.01

SPEC = NodeSpec(
    kind="rig_fit",
    label="Rig Fit",
    summary="Fits the Arma skeleton and the sample body to your mesh, from landmarks that you set or that are guessed.",
    inputs=(3, 3),
    input_labels=("Your mesh", "Skeleton", "Sample body"),
    input_help=("Your character in an A-pose, standing upright (Y up) and facing +Z like the sample. It may have "
                "landmark positions from a Rig Landmarks node in front of this one (see Landmarks).",
                "The Arma skeleton, for example from Rig Skeleton. It is not changed.",
                "The sample body with its bone weights, for example Resolution 1 of the sample character from Extract "
                "LOD. Its bones must be the ones of the skeleton."),
    parms=(
        Parm("show", "Show", "menu", "all",
             "Which results are drawn in the viewport. The node always gives all three results as one geometry, three "
             "packed primitives named by the prim string a3ob_rig_part. This menu only hides the others from view; the "
             "output is the same for every choice. Weight Transfer takes the fitted sample body from it by itself.",
             items=(("all", "All"), ("skeleton", "Fitted skeleton"), ("body", "Fitted sample body"),
                    ("landmarks", "Landmarks")),
             item_help=(("all", "Draw the fitted skeleton, the fitted sample body and the landmarks."),
                        ("skeleton", "Draw the skeleton moved into your mesh (fitted_skeleton, a working copy, A'). Its "
                                     "joints are at your landmarks and its transform is a pure rotation "
                                     "(no scale) that aims the X axis down the bone to its child, with Y toward the "
                                     "front (+Z)."),
                        ("body", "Draw the sample body, posed and sized to match your mesh (fitted_body, S'). It keeps "
                                 "its bone weights."),
                        ("landmarks", "Draw the landmarks that were used, as points with a name (landmarks)."))),
        Parm("landmarks", "Landmarks", "menu", "both",
             "Where the landmarks come from: the chin, the groin and the shoulder, elbow, wrist, hip, knee, ankle, middle "
             "finger and thumb of each side. The Rig Landmarks node in front of this one holds the positions you placed (its markers are "
             "dragged in the viewport). Left is the +X side.",
             items=(("guess", "Auto-guess"), ("landmarks", "From Rig Landmarks"), ("both", "Both")),
             item_help=(("guess", "Guess all landmarks from the shape of your mesh. Rig Landmarks is ignored."),
                        ("landmarks", "Use only the positions of Rig Landmarks. All of them must be set."),
                        ("both", "Use the positions that are set in Rig Landmarks; guess the others. A position you "
                                 "set always wins over the guess."))),
        Parm("snap", "Snap to Surface", "toggle", True,
             "Move the chin and the groin onto the closest point of your mesh. The joints lie inside the body and are "
             "never moved."),
        Parm("symmetry", "Symmetry", "toggle", True,
             "Make the left and right landmarks mirror each other. When both sides of a pair are set in Rig Landmarks they "
             "are averaged; when only one side is, the other side is its mirror image."),
        Parm("refine", "Refine", "toggle", False,
             "Not available yet: the sample body is only posed and sized, not bent to your mesh. Switching it on adds a "
             "warning and changes nothing."),
        Parm("scale", "Scale", "menu", "bone", "How the length of each bone is fitted.", items=(
            ("bone", "Per bone length"),
            ("body", "Whole body"),
        ), item_help=(
            ("bone", "Each bone gets the length that puts its end joint on the landmark."),
            ("body", "One size for the whole skeleton. The joints are turned towards the landmarks but not moved onto "
                     "them, so the quality shows how far off they are."),
        )),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    mesh_in = node.inputGeometry(0)
    sample_in = node.inputGeometry(2)
    if mesh_in is None or sample_in is None or node.inputGeometry(1) is None:
        raise UserError(format_problem(NAME, "an input is not connected", "Connect your mesh to input 1, the skeleton "
                                       "to input 2 and the sample body to input 3"))
    side = landmark_io.sample_side(NAME, node.inputGeometry(1), sample_in)
    skeleton, index, rest, template = side.skeleton, side.index, side.rest, side.template
    positions_s, tris_s, found = side.positions, side.tris, side.found

    positions_in, tris_in = rig_weights.read_mesh(mesh_in)
    if not len(tris_in):
        raise UserError(format_problem(NAME, "your mesh has no polygons", "Use a mesh with polygons on input 1"))
    skin_in = positions_in[np.unique(tris_in)]

    user, source = _landmarks(mesh_in, values["landmarks"], skin_in, side, report)
    truth = dict(template.truth)
    if values["snap"]:  # the sample's crotch is snapped the same way, so the two stay comparable
        user = snap_to_surface(user, positions_in, tris_in)
        truth.update(snap_to_surface({"lm_groin": truth["lm_groin"]}, positions_s, tris_s))
    if values["symmetry"]:  # after the snap: the points on the middle line end up on the middle plane
        user = symmetrize(user, [name for name, how in source.items() if how == "set"])
    if facing_sign(skin_in) != facing_sign(side.skin):
        report.warning(format_problem(NAME, "the feet of your mesh point the other way than the sample's (the sample faces +Z)",
                                      "Turn the mesh to face +Z, or left and right will be swapped"))

    try:
        targets = joint_targets(index, rest, user, truth, BONE_OF)
        fitted = fit_skeleton(skeleton.parents, rest, targets, values["scale"], body_scale(user, truth))
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem), "Check the landmarks")) from None

    parts = {rig_weights.PART_SKELETON: _skeleton_geo(node.inputGeometry(1), fitted),
             rig_weights.PART_BODY: _body_geo(sample_in, skin_points(positions_s, found.weights, rest, fitted)),
             rig_weights.PART_LANDMARKS: _landmarks_geo(user)}
    wanted = {"skeleton": rig_weights.PART_SKELETON, "body": rig_weights.PART_BODY, "landmarks": rig_weights.PART_LANDMARKS}
    hidden = [name for name in parts if values["show"] != "all" and name != wanted[values["show"]]]
    rig_weights.write_parts(node.geometry(), parts, hidden)
    _report(report, values, user, source, skeleton, fitted)


def _landmarks(mesh, mode: str, skin: np.ndarray, side, report: Report):
    """``(positions, source)``: the landmark positions and, for each, ``"set"`` (from Rig Landmarks) or ``"guess"``."""
    placed = landmark_io.read_landmarks(mesh)
    if mode == "guess":
        if placed:
            report.info("%d landmark position%s of Rig Landmarks ignored (Landmarks is set to Auto-guess)." % (
                len(placed), "" if len(placed) == 1 else "s"))
        placed = {}
    elif mode == "landmarks":
        absent = [name for name in LANDMARK_NAMES if name not in placed]
        if absent:
            raise UserError(format_problem(NAME, "the landmark %s not set" % list_elements("positions", absent),
                                           "Set them in Rig Landmarks (Guess Landmarks fills all), or set Landmarks to "
                                           "Both or Auto-guess"))
    positions = dict(placed)
    source = {name: "set" for name in placed}
    if len(placed) < len(LANDMARK_NAMES):
        guessed, notes = landmark_io.guess_for_mesh(skin, side, placed)
        for name in LANDMARK_NAMES:
            if name not in positions:
                positions[name] = guessed[name]
                source[name] = "guess"
                if name in notes:
                    report.warning(format_problem(NAME, "%s: %s" % (name, notes[name]), "Set the landmark in Rig Landmarks"))
    return positions, source


def _skeleton_geo(skeleton_in, fitted):
    """A' : the input skeleton with fitted ``P`` and ``transform`` (a pure rotation aimed down the bone, as a row-vector 3x3)."""
    geo = hou.Geometry()
    geo.copy(skeleton_in)
    geo.setPointFloatAttribValues("P", [float(v) for v in fitted.positions.reshape(-1)])
    if geo.findPointAttrib("transform") is None:
        geo.addAttrib(hou.attribType.Point, "transform", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    geo.setPointFloatAttribValues("transform", [float(v) for v in np.transpose(fitted.frames, (0, 2, 1)).reshape(-1)])
    return geo


def _body_geo(sample_in, points: np.ndarray):
    """S': the sample body, posed. Everything else, the bone selections and ``a3ob_w_*`` included, stays."""
    geo = hou.Geometry()
    geo.copy(sample_in)
    geo.setPointFloatAttribValues("P", [float(v) for v in points.reshape(-1)])
    return geo


def _landmarks_geo(user):
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Point, "name", "")
    names = [name for name in LANDMARK_NAMES if name in user]
    geo.createPoints([tuple(float(v) for v in user[name]) for name in names])
    geo.setPointStringAttribValues("name", names)
    return geo


def _report(report: Report, values, user, source, skeleton, fitted) -> None:
    placed = sum(1 for how in source.values() if how == "set")
    report.info("%d landmarks: %d from Rig Landmarks, %d guessed. Scale: %s." % (
        len(user), placed, len(user) - placed, "per bone length" if values["scale"] == "bone" else "whole body"))
    if fitted.residual:
        worst = sorted(fitted.residual.items(), key=lambda item: -item[1])
        mean = float(np.mean(list(fitted.residual.values())))
        big = worst[0][1]
        report.info("Fit quality: the %d target joints are %.1f cm from their landmarks on average and %.1f cm at most (%s)." % (
            len(worst), 100 * mean, 100 * big, skeleton.names[worst[0][0]]))
        if big > WARN_DISTANCE:
            far = [skeleton.names[j] for j, d in worst if d > WARN_DISTANCE]
            report.warning(format_problem(NAME, "after the fit %s is more than 1 cm from its landmark (%.1f cm at most)" % (
                list_elements("joints", far), 100 * big), "Check the landmarks of those joints, or the left and right "
                "pairs; a bad landmark moves the skeleton and the unposed result"))
    if values["refine"]:
        report.warning(format_problem(NAME, "Refine is not available yet and was not applied"))
    guessed = [name for name in LANDMARK_NAMES if name in source and source[name] == "guess"]
    if guessed:
        report.info("Guessed: %s. Look at the landmarks part (Show: Landmarks) on your mesh; set it in Rig Landmarks to correct one." % ", ".join(guessed))


BUTTONS = {}
MENUS = {}

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

The one output holds the results as packed primitives (D40; a Python SOP writes only output 0). The prim string
``a3ob_rig_part`` names each: ``fitted_skeleton``, ``fitted_body``, ``landmarks`` and ``rest_body`` (the sample body as it
came in, never drawn; Unpose needs it for the cage, task #80). The Show menu only hides parts in the
viewport. ``rig_weights.read_part`` takes a part out again.
"""

import numpy as np

from a3ob_core.rig import (BONE_OF, FOOT_LANDMARKS, foot_landmarks, lay_on_sole, LANDMARK_NAMES, body_scale, facing_sign, fit_skeleton, joint_targets, skin_points,
                           snap_to_surface, symmetrize, bend_fingers, joint_frames)

from .. import kept_result, landmark_io, rig_weights
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
             "Which results are drawn in the viewport. The node always gives all results as one geometry, packed "
             "primitives named by the prim string a3ob_rig_part (a fourth, rest_body, the sample body as it came in, "
             "is never drawn; Unpose uses it). This menu only hides the others from view; the output is the same for "
             "every choice, and changing it does not fit again (the last fit is kept). Weight Transfer takes the fitted sample body from it by itself.",
             items=(("all", "All"), ("skeleton", "Fitted skeleton"), ("body", "Fitted sample body"),
                    ("landmarks", "Landmarks")),
             item_help=(("all", "Draw the fitted skeleton, the fitted sample body and the landmarks."),
                        ("skeleton", "Draw the skeleton moved into your mesh (fitted_skeleton, a working copy, A'). Its "
                                     "joints are at your landmarks and its transform is a pure rotation "
                                     "(no scale) that aims the X axis down the bone to its child and puts the "
                                     "hinge of elbows, knees, fingers and thumbs on Z, the same on both sides "
                                     "(the same rule as the Rig Skeleton's joints)."),
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
        Parm("bendfingers", "Bend Fingers", "toggle", True,
             "Bend the finger joints of the fitted skeleton to follow the fingers of your mesh (needs Per bone length). The "
             "sample's fingers are straight and a relaxed hand is curled, so without this the wrapped sample body cannot follow "
             "the curl and Unpose keeps it. The joints after the knuckle of each of the four fingers and the thumb are placed "
             "from your finger mesh (the points of Body Group), the sample's own fingers as the model; the finger and thumb "
             "landmarks hold the end of the middle finger and the thumb. The bones keep their frames (pure rotations aimed "
             "down the bone). Off: the finger chains stay straight, only turned and sized."),
        Parm("refine", "Refine", "toggle", True,
             "Wrap the fitted sample body onto your bare body with Topo Transfer, so it takes your proportions. Each "
             "landmark is moved to the closest point of the sample body and of your body; the pairs with the same name "
             "guide the wrap. The heel and the toe tip of each foot are found on both bodies by themselves and "
             "guide it too, and the lower part of each wrapped foot is then laid on your sole, so the feet end where "
             "yours end and stand flat. The fitted sample body part becomes the wrapped body, with its bone weights. "
             "Off: the sample body is only posed and sized."),
        Parm("bodygroup", "Body Group", "group", "body",
             "The point group or primitive group of your mesh that Refine wraps onto: the bare body, without hair, "
             "helmet or other accessories. A primitive group counts as the points of its primitives. Empty means the "
             "whole mesh.", group_type="points"),
        Parm("scale", "Scale", "menu", "bone", "How the length of each bone is fitted.", items=(
            ("bone", "Per bone length"),
            ("body", "Whole body"),
        ), item_help=(
            ("bone", "Each bone gets the length that puts its end joint on the landmark."),
            ("body", "One size for the whole skeleton. The joints are turned towards the landmarks but not moved onto "
                     "them, so the quality shows how far off they are."),
        )),
        Parm("headmatch", "Head Match", "toggle", False,
             "Take the head joint from the sample instead of guessing it from the chin. You line your character up with "
             "the sample at the head (a Transform SOP that scales and moves your mesh until your head sits on the "
             "sample's head) and give that transform here: Match Scale and Match Translate. The head joint is then the "
             "sample's own head joint, mapped back from the sample's space into your mesh's through the inverse of "
             "that transform (your point = (sample point - translate) / scale). Off: the head joint is guessed from the "
             "chin landmark and this node warns that it is a guess. The transform must be a uniform scale and a "
             "translate (no rotation). The neck joints are placed as before (the neck from the torso, neck1 between "
             "the neck and the head)."),
        Parm("matchscale", "Match Scale", "float", 1.0,
             "The uniform scale of your head match transform (the Uniform Scale of the Transform SOP). Example: 1.0338 "
             "for the head height comparison.", minimum=0.01, enable_when=(("headmatch", ("1",)),)),
        Parm("matcht", "Match Translate", "float", (0.0, 0.0, 0.0),
             "The translate of your head match transform, in metres (the Translate of the Transform SOP, with its pivot "
             "at the origin). Example: 0, -1.0096, 0.", size=3, enable_when=(("headmatch", ("1",)),)),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    mesh_in = node.inputGeometry(0)
    sample_in = node.inputGeometry(2)
    if mesh_in is None or sample_in is None or node.inputGeometry(1) is None:
        raise UserError(format_problem(NAME, "an input is not connected", "Connect your mesh to input 1, the skeleton "
                                       "to input 2 and the sample body to input 3"))
    key = kept_result.make_key(node, values, display_only=("show",))
    kept = kept_result.lookup(node, key)
    if kept is not None:  # nothing the fit reads has changed (Houdini cooks again for any dirty upstream node)
        _write(node, kept[0], values["show"])
        report.items.extend(kept[1])
        return
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
        match = (values["matchscale"], values["matcht"]) if values["headmatch"] else None
        if match is not None and not match[0] > 0.0:
            raise ValueError("Match Scale must be above 0")
        targets = joint_targets(index, rest, user, truth, BONE_OF, match)
        if match is not None:
            report.info("Head Match: the head joint is the sample's own, mapped back through scale %.4f and translate "
                        "(%.4f, %.4f, %.4f)." % ((match[0],) + tuple(match[1])))
        else:
            report.warning(format_problem(NAME, "the head joint is guessed from the chin landmark (Head Match is off)",
                                          "Turn on Head Match and give the transform that lines your head up with the "
                                          "sample's (scale and translate of a Transform SOP); see the Rig Fit page of the docs"))
        fitted = fit_skeleton(skeleton.parents, rest, targets, values["scale"], body_scale(user, truth))
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem), "Check the landmarks")) from None
    bends = {}
    if values["bendfingers"] and values["scale"] == "bone":
        turns, bent, bends = bend_fingers(skeleton.parents, rest, fitted, index, positions_s, found.weights,
                                          _body_skin(mesh_in, values["bodygroup"], skin_in))
        if bent:
            fitted = fit_skeleton(skeleton.parents, rest, targets, values["scale"], body_scale(user, truth), bent, turns)
            gaps = [float(np.linalg.norm(fitted.positions[j] - targets[j])) for j in bent if j in targets]
            bends["landmark gap"] = (max(gaps) if gaps else 0.0, 0.0, 0.0)  # the end joints of the middle finger and the thumb

    body = _body_geo(sample_in, skin_points(positions_s, found.weights, rest, fitted))
    if values["refine"]:
        body = _refine(body, mesh_in, user, values["bodygroup"], facing_sign(skin_in), report)
    parts = {rig_weights.PART_SKELETON: _skeleton_geo(node.inputGeometry(1), fitted, skeleton),
             rig_weights.PART_BODY: body,
             rig_weights.PART_LANDMARKS: _landmarks_geo(user),
             rig_weights.PART_REST_BODY: _body_geo(sample_in, positions_s)}  # for Unpose; never drawn
    _write(node, parts, values["show"])
    _report(report, values, user, source, skeleton, fitted, bends)
    kept_result.store(node, key, parts, report)  # the key covers the three inputs and every parameter but Show (tasks #94, #97)


def _write(node, parts, show: str) -> None:
    """Write the parts as the output; Show hides all but the chosen one from the viewport (the data is the same)."""
    wanted = {"skeleton": rig_weights.PART_SKELETON, "body": rig_weights.PART_BODY, "landmarks": rig_weights.PART_LANDMARKS}
    hidden = [name for name in parts if name == rig_weights.PART_REST_BODY or (show != "all" and name != wanted[show])]
    rig_weights.write_parts(node.geometry(), parts, hidden)


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


def _skeleton_geo(skeleton_in, fitted, skeleton):
    """A' : the input skeleton with fitted ``P`` and ``transform`` (a pure rotation by the one orientation rule of
    ``a3ob_core.rig.orient``, task #86, as a row-vector 3x3). ``skeleton`` has the joint names and parents."""
    geo = hou.Geometry()
    geo.copy(skeleton_in)
    geo.setPointFloatAttribValues("P", [float(v) for v in fitted.positions.reshape(-1)])
    if geo.findPointAttrib("transform") is None:
        geo.addAttrib(hou.attribType.Point, "transform", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    frames = joint_frames(skeleton.names, skeleton.parents, fitted.positions)
    geo.setPointFloatAttribValues("transform", [float(v) for v in np.transpose(frames, (0, 2, 1)).reshape(-1)])
    return geo


def _body_geo(sample_in, points: np.ndarray):
    """S': the sample body, posed. Everything else, the bone selections and ``a3ob_w_*`` included, stays."""
    geo = hou.Geometry()
    geo.copy(sample_in)
    geo.setPointFloatAttribValues("P", [float(v) for v in points.reshape(-1)])
    return geo


def _body_group_only(mesh_in, group: str, report=None):
    """A copy of your mesh with only the points of ``group`` (all of it when ``group`` is empty). A point group, a
    primitive group or point numbers."""
    out = hou.Geometry()
    out.copy(mesh_in)
    if group.strip():
        keep = rig_weights.group_mask(NAME, out, group, "Body Group", report)
        if not keep.any():
            raise UserError(format_problem(NAME, "the Body Group %r has no point" % group,
                                           "Name a point or primitive group of your mesh, or leave Body Group empty for the whole mesh"))
        drop = [point for point in out.points() if not keep[point.number()]]
        if drop:
            out.deletePoints(drop)
    return out


def _body_skin(mesh_in, group: str, skin_in: np.ndarray) -> np.ndarray:
    """The skin points the fingers are fitted to: the points of ``group`` that belong to polygons (so a glove, a cuff or a
    held weapon in no group does not count), or all the skin points when ``group`` is empty or names nothing."""
    if not group.strip() or (mesh_in.findPointGroup(group.strip()) is None and mesh_in.findPrimGroup(group.strip()) is None):
        return skin_in
    positions, tris = rig_weights.read_mesh(_body_group_only(mesh_in, group))
    return positions[np.unique(tris)] if len(tris) else skin_in


def _refine(body, mesh_in, user, group: str, front: float = 1.0, report=None):
    """The fitted sample body ``body`` wrapped onto the points of ``group`` of ``mesh_in`` with the Topo Transfer verb (a
    verb, so no node is needed inside this Python SOP). The landmarks are snapped to the closest point of each surface and
    matched by their ``name``. The heel and the toe tip of each foot (``foot_landmarks``, task #82) are found on both
    surfaces and added as four more pairs, so the wrapped feet end where the user's feet end."""
    template = rig_weights.without_markers(body)
    target = _body_group_only(mesh_in, group, report)
    template_p, template_tris = rig_weights.read_mesh(template)
    target_p, target_tris = rig_weights.read_mesh(target)
    if not len(target_tris):
        raise UserError(format_problem(NAME, "the body to wrap onto has no polygons", "Check Body Group"))
    feet_template = foot_landmarks(template_p, template_tris, front)
    feet_target = foot_landmarks(target_p, target_tris, front)
    both = [name for name in FOOT_LANDMARKS if name in feet_template and name in feet_target]  # a pair needs both ends
    feet_template, feet_target = ({name: found[name] for name in both} for found in (feet_template, feet_target))
    wrapped = hou.Geometry()
    verb =hou.sopNodeTypeCategory().nodeVerb("topotransfer")
    verb.setParms({"enablegeometryconstraints": 1, "landmarkattrib": "name"})
    try:
        verb.execute(wrapped, [template, target,
                               _landmarks_geo(snap_to_surface(user, template_p, template_tris),
                                              feet_template),
                               _landmarks_geo(snap_to_surface(user, target_p, target_tris),
                                              feet_target)])
    except hou.OperationFailed as problem:
        raise UserError(format_problem(NAME, "Topo Transfer failed: %s" % problem, "Check Body Group (the body should be "
                                       "one clean surface) and the landmarks, or switch Refine off")) from None
    flat = lay_on_sole(np.array(wrapped.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3), target_p, target_tris, front)
    wrapped.setPointFloatAttribValues("P", [float(v) for v in flat.reshape(-1)])
    return wrapped


def _landmarks_geo(user, feet=None):
    """The landmarks as named points; ``feet`` (heel and toe tips, Refine only) are added after them."""
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Point, "name", "")
    names = [name for name in LANDMARK_NAMES if name in user]
    positions = [user[name] for name in names]
    for name in FOOT_LANDMARKS:
        if feet and name in feet:
            names.append(name)
            positions.append(feet[name])
    geo.createPoints([tuple(float(v) for v in position) for position in positions])
    geo.setPointStringAttribValues("name", names)
    return geo


def _report(report: Report, values, user, source, skeleton, fitted, bends=None) -> None:
    placed = sum(1 for how in source.values() if how == "set")
    report.info("%d landmarks: %d from Rig Landmarks, %d guessed. Scale: %s." % (
        len(user), placed, len(user) - placed, "per bone length" if values["scale"] == "bone" else "whole body"))
    if bends:
        gap = bends.pop("landmark gap", (0.0,))[0]
        middle = [pose[1] for name, pose in bends.items() if not name.endswith("thumb")]
        report.info("Fingers: %d fingers bent to your mesh; the bend at the middle joint is %.0f degrees on average (%.0f to %.0f); "
                    "the end of the middle finger and of the thumb are %.1f cm from their landmarks at most." % (
                        len(bends), float(np.mean(np.abs(middle))) if middle else 0.0, min(abs(v) for v in middle) if middle else 0.0,
                        max(abs(v) for v in middle) if middle else 0.0, 100 * gap))
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
        group = values["bodygroup"].strip()
        report.info("Refine: the fitted sample body is wrapped onto %s." % (
            "the point group %s of your mesh" % group if group else "your whole mesh"))
    guessed = [name for name in LANDMARK_NAMES if name in source and source[name] == "guess"]
    if guessed:
        report.info("Guessed: %s. Look at the landmarks part (Show: Landmarks) on your mesh; set it in Rig Landmarks to correct one." % ", ".join(guessed))


BUTTONS = {}
MENUS = {}

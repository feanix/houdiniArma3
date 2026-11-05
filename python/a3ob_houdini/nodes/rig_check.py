# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no rig check; the maths is ``a3ob_core.rig.check``.
"""The Rig Check node (docs/design/character-rigging.md 5.1 to 5.3 and 6.2, task #68): shows what is wrong with the weights
of a rigged mesh, and how it deforms in test poses.

Input 1 is the weighted mesh (bone selections, or KineFX capture weights), input 2 the skeleton (the Rig Skeleton output, or
the output of Rig Fit: its ``fitted_skeleton`` part is used, D40). The mesh keeps its geometry and gets the problem attributes
of 5.2. The geometry and ``Cd`` are never changed; the colours come from a node visualizer, as on Mass.

A Python SOP asset writes only output 0 (D40), so the second and third output of the design are the ``view`` menu: the checked
mesh (the default; the P3D export of it equals the export of the input), the posed mesh of the test pose, or the skeleton with
``a3ob_rig_joint_depth`` and ``Cd``.
"""

import numpy as np

from a3ob_core.rig import ORIGIN_COPIED, ORIGIN_FILLED, ORIGIN_NONE, ORIGIN_NONRIGID, ORIGIN_RIGID, check, normalised_weights

from .. import node_visualizer, rig_weights
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms
from .weight_cleanup import _proxy_points

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "BUTTONS", "loaded"]

NAME = "Rig Check"
VISUALIZER = "a3ob_rig_check_color"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"
STRADDLE_LIMIT = 0.25  # a rigid piece with this share of points on another bone is warned about
SUM_LIMIT = 0.01  # a weight sum further than this from 1 is warned about
MAX_INFLUENCES = 4  # Arma's limit
#: What the visualizer last got per node path (attribute, range, bones), so the cook writes to it only on a change.
_LAST_RAMP = {}

SPEC = NodeSpec(
    kind="rig_check",
    label="Rig Check",
    summary="Shows what is wrong with the bone weights of a rigged mesh, and how it stretches, squashes and loses volume "
            "in test poses.",
    inputs=(2, 2),
    input_labels=("Weighted mesh", "Skeleton"),
    input_help=("The mesh with its bone weights (bone selections, or capture weights).",
                "The skeleton the weights are for, for example from Rig Skeleton, or the output of Rig Fit as it is."),
    parms=(
        Parm("show", "Show", "menu", "stress", "What Visualize colours. Every problem is also written as a point attribute.",
             items=(
                 ("origin", "Origin"), ("jump", "Jump"), ("influences", "Influences"), ("sum", "Sum"),
                 ("straddle", "Straddle"), ("stress", "Stress"), ("volume", "Volume"), ("heaviest", "Heaviest bone"),
             ), item_help=(
                 ("origin", "Where each weight came from (a3ob_rig_origin, written by Weight Transfer and Accessory "
                            "Weights): black none, grey copied, orange filled in, blue rigid piece, green non-rigid piece."),
                 ("jump", "How different the weights are from the weights of the neighbour points: white none, red "
                          "completely different bones (a3ob_rig_jump)."),
                 ("influences", "How many bones hold a weight: grey to white for 1 to 4, magenta for 5, red for more "
                                "(a3ob_rig_infl). Arma keeps only 4."),
                 ("sum", "How far the weights add up from 1: white fine, yellow a little off, red a lot, black no weight "
                         "at all (a3ob_rig_sumerr)."),
                 ("straddle", "A rigid piece whose points favour more than one bone: red when half of its points are on "
                              "another bone than the rest (a3ob_rig_straddle)."),
                 ("stress", "How much the skin stretches (red) or is squashed (blue) in the chosen test pose "
                            "(a3ob_rig_stress)."),
                 ("volume", "Where the mesh loses volume (purple) or gains it (teal) in the chosen test pose, against the "
                            "rest mesh, skinned the way Arma does it (a3ob_rig_volume_change). Purple shows the joints that "
                            "need smoother weights or more edge loops."),
                 ("heaviest", "The heaviest bone of each point, in a repeating run of twelve colours, to read the regions "
                              "at a glance (a3ob_rig_dominant)."),
             ), action="show"),
        Parm("pose", "Pose", "menu", check.WORST, "The test pose that Stress and Volume measure and that the posed mesh "
             "shows. Worst of all measures every pose and keeps the largest change of each point.", items=(
                 (check.WORST, "Worst of all"),) + tuple((key, label) for key, (label, _) in check.POSES.items()),
             item_help=((check.WORST, "Poses the mesh in every test pose below and keeps, for each point, the largest "
                                      "change. The posed mesh is the pose with the most stress."),)
             + tuple((key, "%s. The bones below the bent ones follow; a bone the skeleton lacks is skipped." % label)
                     for key, (label, _) in check.POSES.items())),
        Parm("stress_range", "Stress Range", "float", 0.3,
             "The change of length (Stress) or volume (Volume) that shows as full colour: 0.3 is 30 %. Stress and volume "
             "above this are warned about.", minimum=0.01, action="range"),
        Parm("jump_limit", "Jump Limit", "float", 0.5,
             "A weight jump above this is warned about. 0 is the same weights as the neighbour, 1 is completely different "
             "bones.", minimum=0.0, maximum=1.0),
        Parm("view", "Output", "menu", "mesh", "What the node outputs. The checked mesh keeps your geometry and only gains "
             "attributes. Use the others to look; do not export them.", items=(
                 ("mesh", "Checked mesh"), ("posed", "Posed mesh"), ("skeleton", "Skeleton")),
             item_help=(("mesh", "Your mesh with the problem attributes. Its P3D export is the same as without the node."),
                        ("posed", "Your mesh bent into the chosen test pose, so you can see the deformation."),
                        ("skeleton", "The skeleton with a3ob_rig_joint_depth and Cd: green inside the mesh, yellow within "
                                     "1 cm of the surface, red outside."))),
        Parm("visualize", "Visualize", "toggle", False,
             "Colour the points in the viewport by the Show entry. This is a node visualizer, so the geometry (and any Cd "
             "it has) is not changed. The export ignores it.", action="visualize"),
    ),
    on_loaded=True,
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    if node.inputGeometry(0) is None or node.inputGeometry(1) is None:
        raise UserError(format_problem(NAME, "an input is not connected", "Connect the weighted mesh to input 1 and the "
                                       "skeleton to input 2"))
    source = rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_SKELETON, "input 2")
    skeleton = rig_weights.read_skeleton(NAME, source)
    names = skeleton.names
    if len(names) > 1 and not any(p >= 0 for p in skeleton.parents):
        raise UserError(format_problem(NAME, "the skeleton has no bone lines between its joints",
                                       "Connect a Rig Skeleton to input 2"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    count = len(geo.points())
    positions, tris = rig_weights.read_mesh(geo)
    weights = _weights(geo, names, report)
    proxy = np.zeros(count, dtype=bool)
    proxy[sorted(_proxy_points(geo))] = True
    weights[proxy] = 0.0
    rest = np.array(skeleton.positions, dtype=np.float64).reshape(-1, 3)
    edges = check.edges_of(tris)

    jump, infl, sumerr, dominant = check.weight_problems(weights, edges)
    jump[proxy], infl[proxy], sumerr[proxy], dominant[proxy] = 0.0, 0, 0.0, -1
    piece = _rigid_pieces(geo, count)
    shares = check.straddle(dominant, piece)
    depth = check.joint_depth(rest, positions, tris)

    w = normalised_weights(positions, weights)
    results = _poses(values["pose"], names, skeleton.parents, rest, positions, tris, edges, w, report)
    if results:
        stress, volume = check.combine(results, "stress"), check.combine(results, "volume")
        shown = max(results, key=lambda r: float(np.abs(r.stress).max()))
    else:
        stress, volume, shown = np.zeros(count), np.zeros(count), None
    stress[proxy], volume[proxy] = 0.0, 0.0

    for attrib, data, kind in (("a3ob_rig_jump", jump, float), ("a3ob_rig_infl", infl, int),
                               ("a3ob_rig_sumerr", sumerr, float), ("a3ob_rig_straddle", shares, float),
                               ("a3ob_rig_stress", stress, float), ("a3ob_rig_dominant", dominant, int),
                               ("a3ob_rig_volume_change", volume, float)):
        _write(geo, attrib, data, kind)

    _report_all(report, names, values, geo, count, proxy, jump, infl, sumerr, dominant, piece, shares, depth, stress, volume,
                w, shown, results)
    _write_view(node, geo, values["view"], source, depth, shown)
    _update_ramp(node, values["show"], values["stress_range"], len(names))


# ---- reading ------------------------------------------------------------------------------------------------------------

def _weights(geo, names, report: Report) -> np.ndarray:
    """Points x bones: the bone selections, or the capture weights when there are no bone selections."""
    found = rig_weights.read_selections(NAME, geo, names)
    if found.groups:
        return found.weights
    if geo.findPointAttrib(rig_weights.CAPTURE) is not None:
        return rig_weights.read_capture(NAME, geo, names)[0]
    report.warning(format_problem(NAME, "no selection of the mesh is named like a joint of the skeleton, and it has no capture "
                                  "weights", "Check that input 2 is the skeleton of this character, and weight the mesh first"))
    return found.weights


def _rigid_pieces(geo, count: int) -> np.ndarray:
    """The rigid piece number of every point (-1 for none), as Accessory Weights wrote it."""
    piece = -np.ones(count, dtype=np.int64)
    if geo.findPointAttrib(PIECE_ATTRIB) is not None and geo.findPointAttrib(ORIGIN_ATTRIB) is not None:
        numbers = np.array(geo.pointIntAttribValues(PIECE_ATTRIB), dtype=np.int64)
        origin = np.array(geo.pointIntAttribValues(ORIGIN_ATTRIB), dtype=np.int64)
        rigid = (origin == ORIGIN_RIGID) & (numbers >= 0)
        piece[rigid] = numbers[rigid]
    return piece


def _poses(chosen: str, names, parents, rest, positions, tris, edges, w, report: Report):
    results = []
    skipped = []
    for key in (list(check.POSES) if chosen == check.WORST else [chosen]):
        result = check.run_pose(key, names, parents, rest, positions, tris, edges, w)
        if result.used:
            results.append(result)
        else:
            skipped.append(check.POSES[key][0])
    if skipped:
        report.warning(format_problem(NAME, "%s %s skipped: the skeleton has none of the bones they bend" % (
            "poses" if len(skipped) > 1 else "the pose", ", ".join(skipped)), "Connect the Arma skeleton or a Rig Fit output"))
    return results


def _write(geo, name: str, data, kind) -> None:
    if geo.findPointAttrib(name) is None:
        geo.addAttrib(hou.attribType.Point, name, kind(0))
    if kind is int:
        geo.setPointIntAttribValues(name, [int(v) for v in data])
    else:
        geo.setPointFloatAttribValues(name, [float(v) for v in data])


# ---- the output ---------------------------------------------------------------------------------------------------------

def _write_view(node, geo, view: str, skeleton_geo, depth: np.ndarray, shown) -> None:
    """The posed mesh or the skeleton instead of the checked mesh, when the Output menu says so."""
    if view == "posed" and shown is not None:
        geo.setPointFloatAttribValues("P", [float(v) for v in shown.posed.reshape(-1)])
    elif view == "skeleton":
        out = hou.Geometry()
        out.copy(skeleton_geo)
        colours = [(0.2, 0.8, 0.3) if d > 0.01 else (1.0, 0.9, 0.1) if d >= 0.0 else (1.0, 0.1, 0.1) for d in depth]
        out.addAttrib(hou.attribType.Point, "a3ob_rig_joint_depth", 0.0)
        out.setPointFloatAttribValues("a3ob_rig_joint_depth", [0.0 if d != d else float(d) for d in depth])
        if out.findPointAttrib("Cd") is None:
            out.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
        out.setPointFloatAttribValues("Cd", [float(c) for colour in colours for c in colour])
        geo.clear()
        geo.merge(out)


# ---- the messages -------------------------------------------------------------------------------------------------------

def _first(numbers) -> str:
    return list_elements("points", [int(n) for n in numbers])


def _percent(value: float) -> str:
    return "%+.0f %%" % (100 * value)


def _report_all(report, names, values, geo, count, proxy, jump, infl, sumerr, dominant, piece, shares, depth, stress, volume,
                w, shown, results) -> None:
    real = ~proxy
    report.info("Checked %d points against %d bones." % (int(real.sum()), len(names)))
    _report_origin(report, geo, real)
    over = np.nonzero(real & (jump > values["jump_limit"]))[0]
    if len(over):
        report.warning(format_problem(NAME, "%d points have a weight jump above %.2f to a neighbour (%s)" % (
            len(over), values["jump_limit"], _first(over)), "Smooth the weights there, or look at Show: Jump"))
    else:
        report.info("No weight jump above %.2f." % values["jump_limit"])
    many = np.nonzero(real & (infl > MAX_INFLUENCES))[0]
    if len(many):
        report.warning(format_problem(NAME, "%d points have more than %d bones (%s)" % (len(many), MAX_INFLUENCES, _first(many)),
                                      "Run Weight Cleanup before the export"))
    bare = np.nonzero(real & (sumerr < 0))[0]
    if len(bare):
        report.warning(format_problem(NAME, "%d points have no bone weight (%s)" % (len(bare), _first(bare)),
                                      "Weight them, for example with Weight Transfer"))
    off = np.nonzero(real & (sumerr > SUM_LIMIT))[0]
    if len(off):
        report.warning(format_problem(NAME, "%d points have a weight sum that is not 1 (%s)" % (len(off), _first(off)),
                                      "Run Weight Cleanup"))
    _report_straddle(report, names, dominant, piece, shares)
    _report_joints(report, names, depth)
    if shown is None:
        return
    label = shown.label if len(results) == 1 else "Worst of all (%s has the most stress)" % shown.label
    limit = values["stress_range"]
    hard = np.nonzero(real & (np.abs(stress) > limit))[0]
    text = "Stress in %s: %s at most, %s at least" % (label, _percent(float(stress.max())), _percent(float(stress.min())))
    if len(hard):
        report.warning(format_problem(NAME, "%d points are stretched or squashed by more than %.0f %% (%s). %s" % (
            len(hard), 100 * limit, _first(hard), text), "Look at Show: Stress and smooth the weights there"))
    else:
        report.info(text + ".")
    lost = np.nonzero(real & (volume < -limit))[0]
    worst = check.worst_joints(volume, w)
    naming = ", ".join("%s %s" % (names[joint], _percent(loss)) for joint, loss in worst)
    text = "Volume in %s: %s at most lost, %s at most gained" % (label, _percent(float(volume.min())), _percent(float(volume.max())))
    if worst:
        text += ". Joints with the worst loss: %s" % naming
    if len(lost):
        report.warning(format_problem(NAME, "%d points lose more than %.0f %% of their volume (%s). %s" % (
            len(lost), 100 * limit, _first(lost), text), "Smooth the weights at those joints or add edge loops"))
    else:
        report.info(text + ".")


def _report_origin(report, geo, real) -> None:
    if geo.findPointAttrib(ORIGIN_ATTRIB) is None:
        report.info("The mesh has no a3ob_rig_origin (Weight Transfer writes it), so Show: Origin has nothing to colour.")
        return
    origin = np.array(geo.pointIntAttribValues(ORIGIN_ATTRIB), dtype=np.int64)[real]
    labels = ((ORIGIN_NONE, "no weight"), (ORIGIN_COPIED, "copied"), (ORIGIN_FILLED, "filled in"),
              (ORIGIN_RIGID, "in rigid pieces"), (ORIGIN_NONRIGID, "in non-rigid pieces"))
    report.info("Origin: %s." % ", ".join("%d %s" % (int((origin == number).sum()), text) for number, text in labels))


def _report_straddle(report, names, dominant, piece, shares) -> None:
    flagged = []
    for number in np.unique(piece[piece >= 0]):
        members = np.nonzero(piece == number)[0]
        if shares[members[0]] < STRADDLE_LIMIT:
            continue
        bones = dominant[members]
        counts = np.bincount(bones[bones >= 0], minlength=len(names))
        top = np.argsort(-counts)[:2]
        flagged.append("piece %d (bones %s)" % (number, ", ".join(
            "%s %.2f" % (names[b], counts[b] / len(members)) for b in top if counts[b])))
    if flagged:
        report.warning(format_problem(NAME, "%d rigid pieces straddle a joint: %s" % (
            len(flagged), "; ".join(flagged[:5]) + (" and %d more" % (len(flagged) - 5) if len(flagged) > 5 else "")),
            "Give the piece one bone in Accessory Weights"))


def _report_joints(report, names, depth) -> None:
    if not np.isfinite(depth).all():
        report.warning(format_problem(NAME, "the mesh has no polygons, so the joints cannot be checked against it"))
        return
    outside = [i for i, d in enumerate(depth) if d < 0]
    if outside:
        report.warning(format_problem(NAME, "%d joints are outside the mesh: %s" % (
            len(outside), "; ".join("%s by %.1f cm" % (names[i], -100 * depth[i]) for i in outside[:5])
            + (" and %d more" % (len(outside) - 5) if len(outside) > 5 else "")), "Check the landmarks of Rig Fit"))
    else:
        report.info("All %d joints are inside the mesh." % len(names))


# ---- the visualizer -----------------------------------------------------------------------------------------------------

def _bone_count(node) -> int:
    try:
        source = rig_weights.read_part(NAME, node.inputGeometry(1), rig_weights.PART_SKELETON, "input 2")
        return max(len(source.points()), 1)
    except Exception:
        return 100


def _update_ramp(node, show: str, stress_range: float, bones: int) -> None:
    """Keep the visualizer on the Show entry and the range. Writes only when something changed. Without a visualizer (hython,
    or none made yet) nothing happens."""
    viz = node_visualizer.find(node, VISUALIZER)
    key = (show, round(float(stress_range), 6), bones)
    if viz is not None and _LAST_RAMP.get(node.path()) != key:
        node_visualizer.set_attrib(viz, check.RAMPS[show][0], *check.ramp_for(show, stress_range, bones))
        _LAST_RAMP[node.path()] = key


def _show(node) -> None:
    show, stress_range = node.evalParm("show"), node.evalParm("stress_range")
    stops, low, high = check.ramp_for(show, stress_range, _bone_count(node))
    attrib, label = check.RAMPS[show]
    viz = node_visualizer.ensure_color_ramp(node, VISUALIZER, "Rig Check", attrib, stops, low, high)
    if viz is not None:
        node_visualizer.set_attrib(viz, attrib, stops, low, high)
        _LAST_RAMP[node.path()] = (show, round(float(stress_range), 6), _bone_count(node))


def visualize(kwargs) -> None:
    """The Visualize toggle was switched: add or remove the node visualizer."""
    node = kwargs["node"]
    if node.evalParm("visualize"):
        _show(node)
    else:
        node_visualizer.remove(node, VISUALIZER)
        _LAST_RAMP.pop(node.path(), None)


def show(kwargs) -> None:
    """The Show menu or the Stress Range changed: point the visualizer at the new attribute and range."""
    node = kwargs["node"]
    if node.evalParm("visualize"):
        _show(node)


def loaded(node) -> None:
    """The OnLoaded event: a scene saved with Visualize on has the toggle but may not have the visualizer."""
    if node.evalParm("visualize") and node_visualizer.available():
        _show(node)


BUTTONS = {"visualize": visualize, "show": show, "range": show}

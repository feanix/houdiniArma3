# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The clean-up steps follow A3OB ``utilities/rigging.py`` ``cleanup`` (v2.5.1, 9f2b8bc: prune, keep the heaviest bones,
# normalise); the grid snap and the report are new. A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden
# (original ArmaToolbox).
"""The Weight Cleanup node (docs/design/character-rigging.md 4.4 and 6.2, task #62): makes the bone selections follow
Arma's rules: at most 4 bones per point, weights on the grid k/254, and a sum of exactly 1.

The maths is in ``a3ob_core.rig.weights``; ``rig_weights`` reads and writes the selections. The skeleton on input 2 says
which selections are bones and whether its ``model.cfg`` class has ``isDiscrete = 1`` (the detail
``a3ob_rig_discrete`` that Rig Skeleton writes). When it has, every point keeps one bone, its heaviest.
"""

import numpy as np

from a3ob_core.rig import clean_weights

from .. import kept_result, rig_weights
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Weight Cleanup"
CHANGED_ATTRIB = "a3ob_rig_changed"

SPEC = NodeSpec(
    kind="weight_cleanup",
    label="Weight Cleanup",
    summary="Makes the bone weights follow Arma's rules: at most 4 bones per point, weights in steps of 1/254, sum 1.",
    inputs=(2, 2),
    input_labels=("Geometry", "Skeleton"),
    input_help=("The LOD geometry with bone selections (point groups with a3ob_w weights).",
                "The skeleton, for example from Rig Skeleton. Its joint names decide which selections are bones. "
                "If its model.cfg class has isDiscrete = 1, every point keeps only its heaviest bone."),
    parms=(
        Parm("maxbones", "Max Bones per Point", "int", 4,
             "The most bones one point may have. Arma allows 4. The lightest weights beyond this are dropped.",
             minimum=1, maximum=16),
        Parm("threshold", "Prune Below", "float", 0.01,
             "Weights below this are dropped. A point never loses its heaviest weight, even when it is below this.",
             minimum=0.0, maximum=1.0),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values)
    if kept_result.replay_geometry(node, report, key):  # nothing changed since the last cook: the kept result (task #97)
        return
    skeleton = rig_weights.read_skeleton(NAME, node.inputGeometry(1))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    found = rig_weights.read_selections(NAME, geo, skeleton.names)
    discrete = skeleton.discrete == 1
    # Proxy points (used only by packed prims) are left out, as the export rules do: their weights stay as they are.
    proxy = _proxy_points(geo)
    cleaned = found.weights.copy()
    change = np.zeros(len(cleaned))
    rest = np.ones(len(cleaned), dtype=bool)
    rest[sorted(proxy)] = False
    cleaned[rest], change[rest], stats = clean_weights(found.weights[rest], values["maxbones"], values["threshold"], discrete)
    rig_weights.write_selections(geo, skeleton.names, cleaned, found.groups,
                                 [bone for bone in range(len(skeleton.names)) if bone in found.groups], True)
    if geo.findPointAttrib(CHANGED_ATTRIB) is None:
        geo.addAttrib(hou.attribType.Point, CHANGED_ATTRIB, 0.0)
    geo.setPointFloatAttribValues(CHANGED_ATTRIB, [float(v) for v in change])
    _report(report, stats, found, skeleton, values["maxbones"], values["threshold"], bool(discrete))
    if not found.groups:
        report.warning(format_problem(NAME, "no selection of the geometry is named like a joint of the skeleton",
                                      "Check that input 2 is the skeleton of this character"))
    kept_result.keep_geometry(node, report, key)


def _proxy_points(geo) -> set:
    """The points only packed prims use: the same set as ``mesh.pack_points`` of the export (read_geo)."""
    from ..p3d_export.read_geo import packed_point_prims
    packed = packed_point_prims(geo)
    if not packed:
        return set()
    used = set(rig_weights.unpacked_points(geo).tolist())
    return {p for p in packed if p not in used}


def _report(report: Report, stats, found, skeleton, max_bones: int, threshold: float, discrete: bool) -> None:
    line = "%d points, %d bone selections." % (stats.points, len(found.groups))
    if discrete:
        line += " The skeleton has isDiscrete = 1, so every point keeps one bone, its heaviest, with weight 1."
    else:
        line += " %d points had more than %d bones." % (stats.over_limit, max_bones)
        line += " %d weights below %g were dropped." % (stats.dropped_small, threshold)
    report.info(line)
    if stats.sum_off:
        report.info("%d points had a weight sum other than 1 (up to %.3f away); the sum is exactly 1 now."
                    % (stats.sum_off, stats.sum_off_max))
    if stats.changed:
        report.info("%d points changed: mean change %.3f, worst %.3f (a3ob_rig_changed)."
                    % (stats.changed, stats.changed_mean, stats.changed_max))
    else:
        report.info("No point changed.")
    if found.zero_members:
        report.info("%d members with weight 0 (byte 255 in the file) count as no weight and were taken out of their "
                    "selections." % found.zero_members)
    if stats.no_weight:
        report.warning(format_problem(NAME, "%d of %d points have no bone weight" % (stats.no_weight, stats.points),
                                      "Weight them, or Arma will not move them with the skeleton"))
    if found.other:
        report.info("%s are not bones of the skeleton and were left alone." % list_elements("Selections", found.other))


BUTTONS = {}
MENUS = {}

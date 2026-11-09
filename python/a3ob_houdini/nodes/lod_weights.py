# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no LOD weights; the maths is in ``a3ob_core.rig.lod_weights``.
"""The LOD Weights node (docs/design/character-rigging.md 3.3 and 6.2, task #92): gives a reduced or hand-made LOD the
bone weights of the finished Res 1.

Input 1 is the LOD to weight (for example Res 1 after PolyReduce), input 2 the weighted source (Res 1 after Unpose and
the weight nodes), input 3 the skeleton. Each point takes the weights of the closest place on the source surface,
interpolated across the source triangle. Rigid pieces of the source (``a3ob_rig_piece`` from Accessory Weights, or an island
that follows one bone only) keep their weights exactly. The result is cleaned like Weight Cleanup does (at most 4 bones,
sum 1, steps of 1/254) and written as bone selections (point groups with ``a3ob_w_*``); the bone selections of input 1 are
replaced, every other group and attribute stays. Points get ``a3ob_rig_origin`` (1 copied, 2 filled in, 3 rigid piece).
"""

import numpy as np

from a3ob_core.rig import ORIGIN_NONE, ORIGIN_RIGID, lod_weights, source_pieces

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

NAME = "LOD Weights"
ORIGIN_ATTRIB = "a3ob_rig_origin"
PIECE_ATTRIB = "a3ob_rig_piece"

SPEC = NodeSpec(
    kind="lod_weights",
    label="LOD Weights",
    summary="Gives a reduced LOD the bone weights of the finished Res 1: each point takes the weights of the closest "
            "place on Res 1.",
    inputs=(3, 3),
    input_labels=("LOD to weight", "Weighted source (Res 1)", "Skeleton"),
    input_help=("The LOD that gets the weights, for example Res 1 after PolyReduce. Its own bone selections are replaced; "
                "its other groups and attributes stay.",
                "The source with bone weights (point groups with a3ob_w weights), usually Res 1 after Unpose and the "
                "weight nodes. Rigid pieces (a3ob_rig_piece) keep their weights.",
                "The skeleton, for example from Rig Skeleton. Its joint names decide which selections are bones and how "
                "they are spelled. If its model.cfg class has isDiscrete = 1, every point keeps only its heaviest bone."),
    parms=(
        Parm("distance", "Match Distance", "float", 0.05,
             "How far from the source a point may be and still copy its weights, as a share of the size of the LOD (the "
             "diagonal of its bounding box). Farther points are listed and filled in from their neighbours.",
             minimum=0.0),
        Parm("maxbones", "Max Bones per Point", "int", 4,
             "The most bones one point may have. Arma allows 4. The lightest weights beyond this are dropped.",
             minimum=1, maximum=16),
        Parm("threshold", "Prune Below", "float", 0.01,
             "Weights below this are dropped. A point never loses its heaviest weight.", minimum=0.0, maximum=1.0),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values)
    if kept_result.replay_geometry(node, report, key):  # nothing changed since the last cook: the kept result (task #97)
        return
    skeleton =rig_weights.read_skeleton(NAME, node.inputGeometry(2))
    source = node.inputGeometry(1)
    if source is None:
        raise UserError(format_problem(NAME, "the weighted source is not connected", "Connect Res 1 to input 2"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    bones = skeleton.names
    src_found = rig_weights.read_selections(NAME, source, bones)
    if not src_found.groups:
        raise UserError(format_problem(NAME, "no selection of the source is named like a joint of the skeleton",
                                       "Connect the weighted Res 1 to input 2 and its skeleton to input 3"))
    src_P, src_tris = rig_weights.read_mesh(source)
    dst_P, dst_tris = rig_weights.read_mesh(geo)
    if not len(src_tris):
        raise UserError(format_problem(NAME, "the source has no polygons", "Use a mesh with polygons on input 2"))
    if not len(dst_tris):
        raise UserError(format_problem(NAME, "the LOD has no polygons", "Use a mesh with polygons on input 1"))
    count = len(dst_P)
    marked = -np.ones(len(src_P), dtype=np.int64)
    if source.findPointAttrib(PIECE_ATTRIB) is not None:
        marked = np.array(source.pointIntAttribValues(PIECE_ATTRIB), dtype=np.int64)
        if source.findPointAttrib(ORIGIN_ATTRIB) is not None:  # a non-rigid piece (origin 4) follows the skin
            origin = np.array(source.pointIntAttribValues(ORIGIN_ATTRIB), dtype=np.int64)
            marked = np.where(origin == ORIGIN_RIGID, marked, -1)
    pieces = source_pieces(len(src_P), src_tris, src_found.weights, marked)

    skip = np.zeros(count, dtype=bool)  # proxy points: not skin
    skip[sorted(_proxy_points(geo))] = True
    active = ~skip
    reach = values["distance"] * float(np.linalg.norm(dst_P[active].max(0) - dst_P[active].min(0)))
    try:
        result = lod_weights(src_P, src_tris, src_found.weights, pieces, dst_P, dst_tris, active, reach,
                             values["maxbones"], values["threshold"], skeleton.discrete == 1)
    except ValueError as problem:
        raise UserError(format_problem(NAME, str(problem))) from None

    existing = rig_weights.read_selections(NAME, geo, bones)
    weights = existing.weights.copy()
    weights[active] = result.weights[active]  # bone selections of input 1 are replaced
    columns = [b for b in range(len(bones)) if b in existing.groups or (weights[:, b] > 0).any()]
    rig_weights.write_selections(geo, bones, weights, existing.groups, columns, False)
    rig_weights.write_point_int(geo, ORIGIN_ATTRIB, result.origin, 0, result.origin != ORIGIN_NONE)
    _report(report, result, reach, int(active.sum()), len(src_found.groups))
    kept_result.keep_geometry(node, report, key)


def _report(report: Report, result, reach: float, count: int, selections: int) -> None:
    info = result.info
    line = "%d points weighted from the source (%d bone selections): %d copied" % (count, selections, info["matched"])
    if info["rigid"]:
        line += ", %d from rigid pieces" % info["rigid"]
    if info["filled"]:
        line += ", %d filled in from their neighbours" % info["filled"]
    if info["fallback"]:
        line += ", %d took their closest source weights (no neighbour had a match)" % info["fallback"]
    report.info(line + ".")
    stats = result.stats
    report.info("Closest source distance: %.4g at most (Match Distance allows %.4g)." % (
        float(result.distance.max()), reach))
    if info["far"]:
        report.warning(format_problem(NAME, "%d points are farther than Match Distance from the source" % info["far"],
                                      "Check that input 1 and input 2 are the same pose, or raise Match Distance"))
    if info["unweighted"]:
        report.warning(format_problem(NAME, "%d points lie on source points with no bone weight" % info["unweighted"],
                                      "Weight the source first"))
    if stats.over_limit:
        report.info("%d points had more than the bone limit and were cut back." % stats.over_limit)


BUTTONS = {}
MENUS = {}

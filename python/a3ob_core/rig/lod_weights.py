# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no LOD weights. The closest point lookup and the harmonic fill are the ones of ``transfer`` (Ericson, Real-Time
# Collision Detection, section 5.1.5; Abdrashitov et al., SIGGRAPH Asia 2023 Technical Communications).
"""Weights for a reduced LOD from the finished Res 1 (docs/design/character-rigging.md 3.3, task #92), with no Houdini.

PolyReduce moves and merges points, so the weights it carries along are blends of blends and no longer add up. This
takes the weights again from the source surface:

1. Each target point takes the weights of the closest place on the source surface, interpolated across the source
   triangle (not the nearest point).
2. A source point that belongs to a rigid piece (``a3ob_rig_piece``, or an island that follows one bone only) keeps the
   piece rigid: a target point whose closest source point is in a piece takes that source point's weights exactly, so a
   helmet or a plate gets no blend at its edge. Skin points never blend in a piece's weights.
3. A target point farther from the source than ``reach``, or whose source is unweighted, is filled in from its
   neighbours (the harmonic fill of ``transfer.inpaint``). A point of a piece with no match anywhere takes its closest
   source weights.
4. The weights are cleaned: at most ``max_bones`` bones, normalised, on the grid k/254 (``weights.clean_weights``).
"""

import dataclasses
from typing import Dict

import numpy as np

from .cage import single_bone_pieces
from .transfer import (ORIGIN_COPIED, ORIGIN_FILLED, ORIGIN_RIGID, Laplacian, closest_points, cotan_edges, inpaint,
                       point_components)
from .weights import CleanStats, clean_weights

__all__ = ["LodWeightsResult", "source_pieces", "lod_weights"]


@dataclasses.dataclass
class LodWeightsResult:
    weights: np.ndarray  # points x bones, cleaned; rows of points that were not active are zero
    origin: np.ndarray  # int per point: ORIGIN_COPIED, ORIGIN_FILLED, ORIGIN_RIGID, ORIGIN_NONE when not active
    distance: np.ndarray  # distance of each active point to the source surface (0 for the others)
    stats: CleanStats
    info: Dict[str, int]


def source_pieces(count: int, tris: np.ndarray, weights: np.ndarray, marked: np.ndarray) -> np.ndarray:
    """The rigid piece number of each source point (-1 for none). ``marked`` is ``a3ob_rig_piece`` (-1 outside pieces).
    Islands of the source mesh whose points all follow the same one bone are pieces too (as Unpose does)."""
    marked = np.asarray(marked, dtype=np.int64)
    edges = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]) if len(tris) else np.zeros((0, 2), np.int64)
    first = int(marked.max()) + 1 if marked.size and marked.max() >= 0 else 0
    found = single_bone_pieces(edges, weights, marked >= 0, first)
    return np.where(marked >= 0, marked, found)


def _rows(W: np.ndarray) -> np.ndarray:
    total = W.sum(axis=1, keepdims=True)
    return np.divide(W, total, out=np.zeros_like(W), where=total > 1e-9)


def lod_weights(src_P: np.ndarray, src_tris: np.ndarray, src_W: np.ndarray, src_piece: np.ndarray, dst_P: np.ndarray,
                dst_tris: np.ndarray, active: np.ndarray, reach: float, max_bones: int = 4, threshold: float = 0.01,
                discrete: bool = False) -> LodWeightsResult:
    """The weights of the ``active`` target points from the source surface ``src_tris`` of ``src_P`` with weights
    ``src_W`` (points x bones) and rigid piece numbers ``src_piece``. ``dst_tris`` are the triangles between the target
    points; ``reach`` is the largest distance of a match in the units of the points. Raises ``ValueError`` when there
    is nothing to work on."""
    src_tris = np.asarray(src_tris, dtype=np.int64).reshape(-1, 3)
    dst_tris = np.asarray(dst_tris, dtype=np.int64).reshape(-1, 3)
    if not len(src_tris):
        raise ValueError("the source has no polygons")
    count, bones = len(dst_P), src_W.shape[1]
    index = np.nonzero(active)[0]
    if not len(index):
        raise ValueError("no target point to weight")
    src_W = _rows(np.maximum(src_W, 0.0))
    info = {"matched": 0, "rigid": 0, "filled": 0, "fallback": 0, "far": 0, "unweighted": 0}

    tri, bary, dist = closest_points(dst_P[index], src_P, src_tris)
    corner = src_tris[tri]
    in_piece = src_piece[corner] >= 0  # (k, 3)
    # Skin corners only: a piece never blends into skin. A triangle with no skin corner has none to blend.
    skin_bary = np.where(in_piece, 0.0, bary)
    skin_total = skin_bary.sum(axis=1, keepdims=True)
    skin_bary = np.divide(skin_bary, skin_total, out=np.zeros_like(skin_bary), where=skin_total > 1e-12)
    at = (skin_bary[:, :, None] * src_W[corner]).sum(1)
    # Piece: the nearest corner decides, when it belongs to a piece.
    nearest = corner[np.arange(len(index)), bary.argmax(1)]
    rigid = src_piece[nearest] >= 0
    at[rigid] = src_W[nearest[rigid]]

    ok_dist = dist <= reach
    ok_weight = at.sum(1) >= 0.5
    matched = ok_dist & ok_weight
    info["far"] = int((~ok_dist).sum())
    info["unweighted"] = int((ok_dist & ~ok_weight).sum())

    known = np.ones(count, dtype=bool)
    fixed = np.zeros((count, bones))
    known[index] = matched
    fixed[index] = np.where(matched[:, None], at, 0.0)
    labels = point_components(count, dst_tris) if len(dst_tris) else np.arange(count)
    has_match = np.zeros(int(labels.max()) + 1, dtype=bool)
    has_match[labels[index[matched]]] = True
    stuck = np.zeros(count, dtype=bool)
    stuck[index] = ~matched & ~has_match[labels[index]]
    known |= stuck
    fixed[index] = np.where(stuck[index][:, None], at, fixed[index])
    info["matched"] = int((matched & ~rigid).sum())
    info["rigid"] = int((matched & rigid).sum())
    info["fallback"] = int(stuck.sum())
    info["filled"] = int(len(index) - matched.sum() - stuck.sum())

    if len(dst_tris) and info["filled"]:
        lap = Laplacian(count, *cotan_edges(dst_P, dst_tris))
        filled, _ = inpaint(lap, known, fixed)
    else:
        filled = fixed
    filled[~active] = 0.0
    raw = np.maximum(filled, 0.0)
    cleaned = np.zeros_like(raw)
    cleaned[index], _change, stats = clean_weights(raw[index], max_bones, threshold, discrete)
    origin = np.zeros(count, dtype=np.int64)
    origin[index] = ORIGIN_FILLED
    origin[index[matched]] = ORIGIN_COPIED
    origin[index[matched & rigid]] = ORIGIN_RIGID
    distance = np.zeros(count)
    distance[index] = dist
    return LodWeightsResult(cleaned, origin, distance, stats, info)

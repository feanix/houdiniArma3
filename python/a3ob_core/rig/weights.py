# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The clean-up order (prune, keep the heaviest, normalise) follows A3OB ``utilities/rigging.py`` (``cleanup``, v2.5.1,
# 9f2b8bc); the code is our own, and the grid snap is new (A3OB does not snap).
"""Bone weights as numpy matrices, with no Houdini (docs/design/character-rigging.md sections 3.2, 4.2 and 4.4).

A weight set is a dense float64 matrix ``W`` with one row per point and one column per bone. A zero is "no weight".
The helpers here turn it into the per-point lists of the KineFX capture format and back, build the capture's
``pCaptData`` numbers, and run the export clean-up: at most N bones per point, weights on the grid k/254 with a sum of
exactly 1.

Only numpy features that exist in numpy 1.26 and 2.x are used.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

__all__ = ["GRID", "CAPT_FLOATS", "match_bone_names", "capture_lists", "matrix_from_capture", "inverse_rest_floats",
           "CleanStats", "clean_weights", "grid_units"]

#: The weights of a P3D file are k/254 (data model 7.1, ``decode_weight``): not 1/255.
GRID = 254
#: ``boneCapture_pCaptData`` holds this many floats per region (the inverse rest matrix and four neutral values).
CAPT_FLOATS = 20
#: A sum counts as 1 when it is within this of 1 (float32 weights add up to 1 within a few 1e-7).
SUM_TOLERANCE = 1e-5
#: Float32 weights are a few 1e-5 units off the grid; this much counts as on it when flooring.
_EPS = 1e-4
#: A change below this is float32 noise, not a change.
CHANGE_TOLERANCE = 1e-6


def match_bone_names(bones: Sequence[str], names: Sequence[str]) -> Tuple[Dict[int, str], List[Tuple[str, str]]]:
    """Match selection ``names`` to ``bones`` ignoring case.

    Returns ``({bone index: selection name}, clashes)``. A clash is a pair of selection names that differ only in case
    and both match one bone (the caller reports it; the bone is left out of the first result). Names that match no bone
    are not bone selections and do not appear."""
    found: Dict[int, List[str]] = {}
    index = {}
    for number, bone in enumerate(bones):
        index.setdefault(bone.lower(), number)
    for name in names:
        number = index.get(name.lower())
        if number is not None:
            found.setdefault(number, []).append(name)
    matches: Dict[int, str] = {}
    clashes: List[Tuple[str, str]] = []
    for number, listed in found.items():
        if len(listed) > 1:
            clashes.append((listed[0], listed[1]))
        else:
            matches[number] = listed[0]
    return matches, clashes


def capture_lists(weights: np.ndarray) -> Tuple[List[List[int]], List[List[float]]]:
    """The per-point ``boneCapture_index`` and ``boneCapture_data`` lists of a weight matrix: the bones with a weight above
    zero, heaviest first (the order of equal weights is the bone order). A point without a weight gets empty lists."""
    indices: List[List[int]] = []
    data: List[List[float]] = []
    for row in np.asarray(weights, dtype=np.float64):
        used = np.nonzero(row > 0)[0]
        used = used[np.argsort(-row[used], kind="stable")]
        indices.append([int(i) for i in used])
        data.append([float(row[i]) for i in used])
    return indices, data


def matrix_from_capture(indices: Sequence[Sequence[int]], data: Sequence[Sequence[float]], region_count: int) -> np.ndarray:
    """The weight matrix (points x regions) of per-point capture lists. A region used twice by one point adds up. An index
    outside ``0 .. region_count - 1`` (the ``-1`` padding of the packed form, for example) is skipped."""
    matrix = np.zeros((len(indices), region_count), dtype=np.float64)
    for point, (listed, weights) in enumerate(zip(indices, data)):
        for region, weight in zip(listed, weights):
            if 0 <= region < region_count:
                matrix[point, region] += weight
    return matrix


def inverse_rest_floats(position: Sequence[float], rotation: Optional[Sequence[float]] = None) -> List[float]:
    """The 20 floats of one region of ``boneCapture_pCaptData`` for a joint at ``position`` with the 9-float point
    attribute ``transform`` (``rotation``, row-major, identity when None).

    Layout (settled by cooking Joint Capture Proximity and Capture Attribute Unpack, and checked with Joint Deform):
    16 floats are the inverse of the joint's rest matrix as a 4x4 in row-vector form (``x' = x * M``; the rotation rows
    first, the translation in the last row), then four neutral floats (taper and range), all 1.0. With an identity
    rotation the last row is ``-position`` and the rest is the identity."""
    rest = np.eye(4)
    if rotation is not None:
        rest[:3, :3] = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    rest[3, :3] = position
    inverse = np.linalg.inv(rest)
    return [float(v) for v in inverse.reshape(16)] + [1.0, 1.0, 1.0, 1.0]


def grid_units(weights: np.ndarray) -> np.ndarray:
    """Weights that sit on the grid, as integers ``k`` (rounded). Meant for tests and messages."""
    return np.rint(np.asarray(weights, dtype=np.float64) * GRID).astype(np.int64)


@dataclasses.dataclass
class CleanStats:
    """What ``clean_weights`` found and changed (counts are points unless the name says weights)."""
    points: int = 0
    #: Points with no weight at all before the clean-up (their row stays empty).
    no_weight: int = 0
    #: Points with more bones than the limit before the clean-up.
    over_limit: int = 0
    #: Weights below the threshold that were dropped.
    dropped_small: int = 0
    #: Points whose sum was not 1 before (not counting the empty ones), and the largest distance from 1.
    sum_off: int = 0
    sum_off_max: float = 0.0
    #: Points that changed at all, the mean L1 change over them, the worst one.
    changed: int = 0
    changed_mean: float = 0.0
    changed_max: float = 0.0
    #: Points that kept only their heaviest bone (the discrete case).
    discrete: bool = False


def clean_weights(weights: np.ndarray, max_bones: int = 4, threshold: float = 0.01, discrete: bool = False
                  ) -> Tuple[np.ndarray, np.ndarray, CleanStats]:
    """The export clean-up (design 4.4) of a weight matrix. Returns ``(new matrix, change per point, stats)``.

    In this order: drop weights below ``threshold`` (a point whose weights are all below keeps its heaviest, so a point
    with a weight never ends up with none); keep the ``max_bones`` heaviest (equal weights: the lower bone number
    wins); with ``discrete`` keep only the heaviest; scale to a sum of 1; snap to the grid k/254 so that the sum is
    exactly 254 units (floor each, then give the missing units to the largest remainders). A point whose sum is 0 stays
    empty. The change per point is the sum of ``|new - old|`` over the bones."""
    old = np.maximum(np.asarray(weights, dtype=np.float64), 0.0)  # a negative weight is no weight
    count = old.shape[0]
    stats = CleanStats(points=count, discrete=discrete)
    if old.size == 0:
        return old.copy(), np.zeros(count), stats
    width = old.shape[1]
    member = old > 0
    infl_before = member.sum(axis=1)
    sums = old.sum(axis=1)
    stats.no_weight = int(np.count_nonzero(infl_before == 0))
    stats.over_limit = int(np.count_nonzero(infl_before > max_bones))
    weighted = infl_before > 0
    off = weighted & (np.abs(sums - 1.0) > SUM_TOLERANCE)
    stats.sum_off = int(np.count_nonzero(off))
    stats.sum_off_max = float(np.abs(sums - 1.0)[off].max()) if off.any() else 0.0

    # 1. prune. The heaviest weight of each point is never pruned.
    top = old.max(axis=1, keepdims=True)
    small = member & (old < threshold) & (old < top)
    work = np.where(small, 0.0, old)
    stats.dropped_small = int(np.count_nonzero(small))

    # 2. keep the heaviest ``max_bones`` (discrete: one). The stable sort makes equal weights keep the lower bone.
    keep = 1 if discrete else max(1, int(max_bones))
    if width > keep:
        order = np.argsort(-work, axis=1, kind="stable")
        rank = np.empty_like(order)
        rows = np.arange(count)[:, None]
        rank[rows, order] = np.arange(width)[None, :]
        work = np.where(rank < keep, work, 0.0)

    # 3. scale to a sum of 1 (a point with no weight stays empty)
    total = work.sum(axis=1, keepdims=True)
    work = np.divide(work, total, out=np.zeros_like(work), where=total > 0)

    # 4. snap to k/254 with an exact sum
    units = work * GRID
    floors = np.floor(units + _EPS)
    remainder = np.where(work > 0, units - floors, -1.0)
    deficit = np.where(total[:, 0] > 0, GRID - floors.sum(axis=1), 0).astype(np.int64)
    order = np.argsort(-remainder, axis=1, kind="stable")
    rank = np.empty_like(order)
    rows = np.arange(count)[:, None]
    rank[rows, order] = np.arange(width)[None, :]
    floors = floors + (rank < deficit[:, None])
    new = floors / GRID

    change = np.abs(new - old).sum(axis=1)
    moved = change > CHANGE_TOLERANCE
    stats.changed = int(np.count_nonzero(moved))
    if stats.changed:
        stats.changed_mean = float(change[moved].mean())
        stats.changed_max = float(change.max())
    return new, change, stats

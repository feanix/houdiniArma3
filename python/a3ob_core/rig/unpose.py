# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no unpose. The method is the one measured in docs/design/character-rigging.md 2.6 (scripts/spikes/rigging/
# unpose_distortion.py): the inverse of each point's blended bone matrix, one matrix per rigid piece.
"""Move a posed mesh back onto the Arma rest pose, with no Houdini (task #67).

Rig Fit makes the fitted skeleton ``A'`` (joints in the user's mesh); Weight Transfer and Weight Cleanup weight the mesh in
that pose (D37 Q13). Unpose moves each point by the inverse of its blended bone matrix, from ``A'`` back to the Arma
skeleton ``A``. Linear blend skinning is assumed, as in the spike.

- **The move of one bone** is built from the pose difference only (task #76 made the fitted skeleton's ``transform`` a pure
  rotation aimed down the bone, while the Arma skeleton keeps its own frames, so the stored frames are not compared).
  Both skeletons get the same frame rule (``fit.bone_frames``) on their joint positions. The rotation is ``R = F' F^T``.
  The size has two parts. **Along the bone** it is the length ratio of the bone: the distance to the joint it is aimed at,
  in ``A'`` over ``A``. **Across the bone** it is the size of the body ``g``: the median length ratio of the bones of the
  trunk (from the root down the main chain to the head). A longer arm is not a thicker arm, and a short bone (the neck)
  does not shrink the head. A joint with nothing to aim at
  (a chain end) has size ``g`` both ways. A perfect fit (``A' = A``) gives ``R = I`` and size 1, so nothing moves.
- **Matrix of a bone:** ``x' = q + R S (x - p)`` with ``p`` the joint in ``A``, ``q`` the joint in ``A'`` and
  ``S = g I + (r - g) a a^T`` (``a`` the direction of the bone in ``A``, ``r`` its length ratio).
- **Rigid pieces** (``a3ob_rig_piece`` of Accessory Weights in Rigid mode, D41) are moved by one matrix per piece: the
  blend of the average weights of its points. A blend of bones is not a rotation, so it is replaced by the closest
  rotation (polar decomposition) times the body size ``g``, turning about the centre of the piece. All edges of a piece
  then change by the same factor, which is 1 when the piece has the size ``g`` posed against its rest.

Rotations are 3x3 column-vector matrices, as in ``fit``.
"""

import dataclasses
from typing import List, Optional, Sequence

import numpy as np

from .fit import aim_targets, bone_frames, normalised_weights

__all__ = ["BoneMoves", "bone_moves", "unpose_points", "bone_displacements"]


@dataclasses.dataclass
class BoneMoves:
    """The move of every bone from ``A`` to ``A'``: ``x' = q + L (x - p)`` with ``L = R (g I + (r - g) a a^T)``."""
    rotation: np.ndarray  # R (n, 3, 3)
    along: np.ndarray  # r, the length ratio of each bone (n,)
    axis: np.ndarray  # a, the direction of each bone in A (n, 3)
    size: float  # g, the size of the body
    rest: np.ndarray  # p, joints of A (n, 3)
    fitted: np.ndarray  # q, joints of A' (n, 3)

    @property
    def linear(self) -> np.ndarray:
        stretch = self.size * np.eye(3) + (self.along - self.size)[:, None, None] * np.einsum("ja,jb->jab", self.axis, self.axis)
        return np.einsum("jab,jbc->jac", self.rotation, stretch)

    @property
    def shift(self) -> np.ndarray:
        """The translation ``q - L p`` of ``x' = L x + shift``."""
        return self.fitted - np.einsum("jab,jb->ja", self.linear, self.rest)


def bone_moves(parents: Sequence[int], rest: np.ndarray, fitted: np.ndarray) -> BoneMoves:
    """The move of every bone from the Arma skeleton (joint positions ``rest``) to the fitted one (``fitted``), from the
    joint positions only: the same frame rule on both, the bone length ratio along the bone, the body size across it."""
    rest, fitted = np.asarray(rest, dtype=np.float64), np.asarray(fitted, dtype=np.float64)
    count = len(parents)
    frames_a, frames_b = bone_frames(parents, rest), bone_frames(parents, fitted)
    rotation = np.einsum("jab,jcb->jac", frames_b, frames_a)  # F' F^T
    ends = aim_targets(parents, rest)
    ratio = np.full(count, np.nan)
    for joint, end in enumerate(ends):
        if end >= 0:
            ratio[joint] = float(np.linalg.norm(fitted[end] - fitted[joint]) / np.linalg.norm(rest[end] - rest[joint]))
    trunk, joint = [], next((j for j, parent in enumerate(parents) if parent < 0), -1)
    while joint >= 0 and ends[joint] >= 0:  # the main chain from the root: pelvis, spine, neck, head
        trunk.append(ratio[joint])
        joint = ends[joint]
    size = float(np.median(trunk)) if trunk else 1.0
    ratio[np.isnan(ratio)] = size
    return BoneMoves(rotation, ratio, frames_a[:, :, 0].copy(), size, rest, fitted)


def _blend_inverse(weights: np.ndarray, moves: BoneMoves):
    """``(L, t)`` per row of ``weights``: the blended linear part and shift of the matrices, ``x' = L x + t``."""
    return np.einsum("nj,jab->nab", weights, moves.linear), weights @ moves.shift


def unpose_points(points: np.ndarray, weights: np.ndarray, moves: BoneMoves, piece: Optional[np.ndarray] = None
                  ) -> np.ndarray:
    """Move ``points`` from the fitted pose back to the Arma rest. ``weights`` is points x bones (scaled to a sum of 1
    here; a point with none takes the weights of the closest weighted point). Each point is moved by the inverse of its
    blended matrix. ``piece`` (an int per point, -1 for none) names the points that are moved by one matrix per piece:
    the blend of the average weights of the piece."""
    points = np.asarray(points, dtype=np.float64)
    w = normalised_weights(points, np.asarray(weights, dtype=np.float64))
    linear, shift = _blend_inverse(w, moves)
    out = np.linalg.solve(linear, (points - shift)[:, :, None])[:, :, 0]
    if piece is not None:
        for number in np.unique(piece[piece >= 0]):
            members = piece == number
            out[members] = _rigid_piece(points[members], w[members].mean(axis=0), moves)
    return out


def _rigid_piece(points: np.ndarray, weights: np.ndarray, moves: BoneMoves) -> np.ndarray:
    """The points of one piece moved back by one matrix: the blend of ``weights``, made a rotation and a size (turning about
    the centre of the piece, which goes where the blend takes it)."""
    linear = (weights @ moves.linear.reshape(len(weights), 9)).reshape(3, 3)
    shift = weights @ moves.shift
    u, _, vt = np.linalg.svd(linear)
    sign = np.sign(np.linalg.det(u @ vt)) or 1.0
    rotation = u @ np.diag([1.0, 1.0, sign]) @ vt  # the closest rotation to the blend
    size = moves.size  # the blend of rotations is shorter than its parts: the size is set on its own
    centre = points.mean(axis=0)
    home = np.linalg.solve(linear, centre - shift)
    return home + (points - centre) @ rotation / max(size, 1e-12)  # row vectors: R^T (x - c) / size


def bone_displacements(points: np.ndarray, moved: np.ndarray, weights: np.ndarray, count: int, top: int = 3
                       ) -> List[tuple]:
    """The bones whose points moved most: ``(bone, mean displacement, point count)`` for the ``top`` bones with the
    largest mean, each point counting for its heaviest bone (task #67 report)."""
    distance = np.linalg.norm(moved - points, axis=1)
    heaviest = weights.argmax(axis=1)
    found = []
    for bone in range(count):
        members = (heaviest == bone) & (weights.sum(axis=1) > 0)
        if members.any():
            found.append((bone, float(distance[members].mean()), int(members.sum())))
    return sorted(found, key=lambda item: -item[1])[:top]

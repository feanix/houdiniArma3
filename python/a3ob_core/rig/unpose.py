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
  The size is **along the bone** only: the length ratio of the bone, the distance to the joint it is aimed at, in ``A'``
  over ``A``. Across the bone there is no size change (task #80: the old trunk size factor made the mesh 26 % too big,
  because a median of bone lengths is not the size of the body). A joint with nothing to aim at (a chain end) has no
  size. A perfect fit (``A' = A``) gives ``R = I`` and size 1, so nothing moves.
- **Matrix of a bone:** ``x' = q + R S (x - p)`` with ``p`` the joint in ``A``, ``q`` the joint in ``A'`` and
  ``S = I + (r - 1) a a^T`` (``a`` the direction of the bone in ``A``, ``r`` its length ratio).
- **Rigid pieces** (``a3ob_rig_piece`` of Accessory Weights in Rigid mode, D41) are moved by one matrix per piece: the
  blend of the average weights of its points. A blend of bones is not a rotation, so it is replaced by the closest
  rotation (polar decomposition), turning about the centre of the piece. No edge of a piece changes.
- The cage method (task #80, D44) is in ``cage.py``.

Rotations are 3x3 column-vector matrices, as in ``fit``.
"""

import dataclasses
from typing import List, Optional, Sequence

import numpy as np

from .fit import aim_targets, bone_frames, normalised_weights

__all__ = ["BoneMoves", "bone_moves", "unpose_points", "bone_displacements"]


@dataclasses.dataclass
class BoneMoves:
    """The move of every bone from ``A`` to ``A'``: ``x' = q + L (x - p)`` with ``L = R (I + (r - 1) a a^T)``."""
    rotation: np.ndarray  # R (n, 3, 3)
    along: np.ndarray  # r, the length ratio of each bone (n,)
    axis: np.ndarray  # a, the direction of each bone in A (n, 3)
    rest: np.ndarray  # p, joints of A (n, 3)
    fitted: np.ndarray  # q, joints of A' (n, 3)

    @property
    def linear(self) -> np.ndarray:
        stretch = np.eye(3) + (self.along - 1.0)[:, None, None] * np.einsum("ja,jb->jab", self.axis, self.axis)
        return np.einsum("jab,jbc->jac", self.rotation, stretch)

    @property
    def shift(self) -> np.ndarray:
        """The translation ``q - L p`` of ``x' = L x + shift``."""
        return self.fitted - np.einsum("jab,jb->ja", self.linear, self.rest)


def bone_moves(parents: Sequence[int], rest: np.ndarray, fitted: np.ndarray) -> BoneMoves:
    """The move of every bone from the Arma skeleton (joint positions ``rest``) to the fitted one (``fitted``), from the
    joint positions only: the same frame rule on both, and the bone length ratio along the bone (no size across it)."""
    rest, fitted = np.asarray(rest, dtype=np.float64), np.asarray(fitted, dtype=np.float64)
    count = len(parents)
    frames_a, frames_b = bone_frames(parents, rest), bone_frames(parents, fitted)
    rotation = np.einsum("jab,jcb->jac", frames_b, frames_a)  # F' F^T
    ends = aim_targets(parents, rest)
    ratio = np.full(count, np.nan)
    for joint, end in enumerate(ends):
        if end >= 0:
            ratio[joint] = float(np.linalg.norm(fitted[end] - fitted[joint]) / np.linalg.norm(rest[end] - rest[joint]))
    ratio[np.isnan(ratio)] = 1.0
    return BoneMoves(rotation, ratio, frames_a[:, :, 0].copy(), rest, fitted)


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
    """The points of one piece moved back by one matrix: the blend of ``weights``, made a rotation (turning about
    the centre of the piece, which goes where the blend takes it). No size change."""
    linear = (weights @ moves.linear.reshape(len(weights), 9)).reshape(3, 3)
    shift = weights @ moves.shift
    u, _, vt = np.linalg.svd(linear)
    sign = np.sign(np.linalg.det(u @ vt)) or 1.0
    rotation = u @ np.diag([1.0, 1.0, sign]) @ vt  # the closest rotation to the blend
    centre = points.mean(axis=0)
    home = np.linalg.solve(linear, centre - shift)
    return home + (points - centre) @ rotation  # row vectors: R^T (x - c)


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

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no unpose. The method is the advisor's prototype of 2026-11-06 (D44, task #80): a cage from the wrapped body
# to the Arma sample body, and one rigid transform for each rigid piece.
"""The pieces of the cage Unpose that need no Houdini (task #80, D44).

The cage itself (Point Deform: rest lattice = the sample body wrapped onto the user's body, deformed lattice = the sample
body in the Arma rest) is the Houdini verb; it runs in ``a3ob_houdini.nodes.unpose``. Here are the two parts around it:

- **The cage.** ``point_deform`` is the maths of the Point Deform SOP (default parameters), in numpy. Houdini has no verb
  for that SOP (``nodeVerb("pointdeform")`` is None; it is a subnet of wrangles), and a Python SOP cannot make nodes while it
  cooks, so the wrangles' maths is ported and checked against the real node (mean 1 mm apart on the owner scene).
- **Which points are a rigid piece.** A piece is the ``a3ob_rig_piece`` of Accessory Weights in Rigid mode, or a connected
  island of points outside the body that follow one bone only (the helmet).
- **Where a piece goes.** The cage moves the points of the piece to new places. The piece is then put at the rigid
  transform (rotation and shift, no size) that fits those places best (Kabsch). So the piece stays on the body where the
  cage puts it, and none of its edges changes.
"""

from typing import Optional

import numpy as np

__all__ = ["point_deform", "lattice_transforms", "single_bone_pieces", "rigid_fit", "place_pieces"]

#: A weight below this is no weight when a point is checked for following one bone only.
WEIGHT_EPS = 1e-4


def _rotation_of(matrices: np.ndarray) -> np.ndarray:
    """The closest rotation of each 3x3 matrix (polar decomposition, as VEX ``polardecomp``)."""
    u, _, vt = np.linalg.svd(matrices)
    rot = u @ vt
    flip = np.linalg.det(rot) < 0
    u[flip, :, 2] *= -1.0
    return u @ vt


def _neighbour_rings(count: int, polys) -> np.ndarray:
    """``(n, K)`` neighbour point numbers of every point (padded with -1), in the order Houdini's ``neighbours()`` mostly
    gives: for each polygon corner at the point (in polygon order), the previous point and then the next one, without
    repeats. Successive neighbours are what the Point Deform frames are built from."""
    lists = [[] for _ in range(count)]
    for ring in polys:
        ring = list(ring)
        for k, a in enumerate(ring):
            for q in (ring[k - 1], ring[(k + 1) % len(ring)]):
                if q != a and q not in lists[a]:
                    lists[a].append(q)
    table = -np.ones((count, max(max((len(x) for x in lists), default=0), 1)), dtype=np.int64)
    for i, x in enumerate(lists):
        table[i, :len(x)] = x
    return table


def _frames(vectors: np.ndarray, ups: np.ndarray) -> np.ndarray:
    """VEX ``maketransform(normalize(z), normalize(y))`` per row: a matrix whose rows are the x, y and z axes."""
    z = vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-300)
    y = ups / np.maximum(np.linalg.norm(ups, axis=-1, keepdims=True), 1e-300)
    return np.stack([np.cross(y, z), y, z], axis=-2)


def lattice_transforms(rest: np.ndarray, deformed: np.ndarray, polys) -> np.ndarray:
    """The 3x3 matrix of every lattice point that takes a row vector of the rest lattice to the deformed one: the average
    of the frames built from successive pairs of neighbours, made a rotation (the ``compute_xforms_sequential`` wrangle of
    the Point Deform SOP, Rigid Projection on)."""
    table = _neighbour_rings(len(rest), polys)
    count, width = table.shape
    degree = (table >= 0).sum(axis=1)
    rows = np.arange(count)
    total = np.zeros((count, 3, 3))
    weight = np.zeros(count)
    for k in range(width):
        use = (degree > k) & (degree >= 2)
        if not use.any():
            continue
        now = table[:, k].clip(0)
        last = table[rows, (k - 1) % np.maximum(degree, 1)].clip(0)
        dp, ldp = deformed[now] - deformed, deformed[last] - deformed
        rdp, lrdp = rest[now] - rest, rest[last] - rest
        up, rup = np.cross(dp, ldp), np.cross(rdp, lrdp)
        w = np.sqrt(np.linalg.norm(up, axis=1) * np.linalg.norm(rup, axis=1))
        good = use & (w > 1e-24)
        if not good.any():
            continue
        fd, fr = _frames(dp[good], up[good]), _frames(rdp[good], rup[good])
        total[good] += w[good, None, None] * (fr.transpose(0, 2, 1) @ fd)
        weight[good] += w[good]
    out = np.tile(np.eye(3), (count, 1, 1))
    some = weight > 0
    out[some] = _rotation_of(total[some] / weight[some, None, None])
    return out


def point_deform(points: np.ndarray, rest: np.ndarray, deformed: np.ndarray, polys, radius: float = 0.1,
                 minpt: int = 1, maxpt: int = 100, chunk: int = 512) -> np.ndarray:
    """``points`` moved by a cage: the same maths as the Point Deform SOP with its default parameters. Each point finds the
    lattice points of ``rest`` within ``radius`` (at most ``maxpt``, at least ``minpt`` whatever the distance), with the
    weight ``(1 - (d / radius)^2)^2``. Each lattice point moves it as its own rigid transform and shift would, and the
    results are averaged by weight. ``polys`` are the polygons of the lattice (lists of point numbers); ``rest`` and
    ``deformed`` have the same point order."""
    points, rest, deformed = (np.asarray(a, dtype=np.float64) for a in (points, rest, deformed))
    xform = lattice_transforms(rest, deformed, polys)
    shift = deformed - rest
    out = points.copy()
    keep = min(maxpt, len(rest))
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk]
        d2 = ((p[:, None, :] - rest[None, :, :]) ** 2).sum(axis=2)
        near = np.argpartition(d2, keep - 1, axis=1)[:, :keep] if keep < len(rest) else np.tile(np.arange(len(rest)), (len(p), 1))
        nd2 = np.take_along_axis(d2, near, axis=1)
        scale = np.full(len(p), radius * radius)
        inside = nd2 <= radius * radius
        few = inside.sum(axis=1) < minpt  # too few in the radius: take the minpt closest, radius a bit more than the farthest
        if few.any():
            order = np.argsort(nd2[few], axis=1)[:, :minpt]
            far = np.take_along_axis(nd2[few], order, axis=1)[:, -1]
            scale[few] = 1.21 * far
            inside[few] = False
            rows = np.nonzero(few)[0]
            for r, o in zip(rows, order):
                inside[r, o] = True
        r2 = nd2 / scale[:, None]
        w = np.where(inside, (1.0 - r2) ** 2, 0.0)
        local = p[:, None, :] - rest[near]  # (m, k, 3)
        moved = np.einsum("mka,mkab->mkb", local, xform[near]) + rest[near] + shift[near]
        total = w.sum(axis=1)
        delta = np.einsum("mk,mka->ma", w, moved - p[:, None, :])
        ok = total > 0
        out[start:start + chunk][ok] = p[ok] + delta[ok] / total[ok, None]
    return out


def single_bone_pieces(edges: np.ndarray, weights: np.ndarray, skip: np.ndarray, first: int) -> np.ndarray:
    """The piece number of every point (-1 for none) of the islands of points that follow one bone only.

    ``edges`` is (m, 2) point pairs of the polygons, ``weights`` points x bones, ``skip`` a bool per point for the points
    that are never a piece (the body, the points already in a piece, points not unposed). An island is the points joined by
    edges that are not skipped; it is a piece when every point of it has weight on one bone and the same bone. The numbers
    start at ``first``."""
    count = len(weights)
    present = weights > WEIGHT_EPS
    one = (present.sum(axis=1) == 1) & ~skip
    bone = np.where(one, present.argmax(axis=1), -1)
    parent = np.arange(count)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    for a, b in edges[~(skip[edges[:, 0]] | skip[edges[:, 1]])]:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    root = np.array([find(i) for i in range(count)], dtype=np.int64)
    piece = -np.ones(count, dtype=np.int64)
    number = first
    for r in np.unique(root[~skip]):
        members = (root == r) & ~skip
        if members.sum() < 2:
            continue
        if one[members].all() and len(np.unique(bone[members])) == 1:
            piece[members] = number
            number += 1
    return piece


def rigid_fit(points: np.ndarray, target: np.ndarray):
    """``(R, c, t)``: the rotation ``R`` and centres that take ``points`` closest to ``target`` with no size change:
    ``x' = t + R (x - c)``. Rows are points; the result of the fit is a rotation (never a mirror)."""
    c, t = points.mean(axis=0), target.mean(axis=0)
    u, _, vt = np.linalg.svd((target - t).T @ (points - c))
    sign = np.sign(np.linalg.det(u @ vt)) or 1.0
    return u @ np.diag([1.0, 1.0, sign]) @ vt, c, t


def place_pieces(points: np.ndarray, caged: np.ndarray, piece: np.ndarray, out: Optional[np.ndarray] = None) -> np.ndarray:
    """``caged`` (the points moved by the cage) with every piece replaced by its points moved by one rigid transform that
    fits the cage best. ``piece`` is an int per point, -1 for none."""
    out = caged.copy() if out is None else out
    for number in np.unique(piece[piece >= 0]):
        members = piece == number
        rotation, c, t = rigid_fit(points[members], caged[members])
        out[members] = t + (points[members] - c) @ rotation.T
    return out

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no weight transfer. The method is Abdrashitov, Raichstat, Monsen, Hill, "Robust Skin Weights Transfer via
# Weight Inpainting", SIGGRAPH Asia 2023 Technical Communications (DOI 10.1145/3610543.3626180), re-implemented from the
# paper's description (no code of the authors is used). The numpy code follows the #60 spike
# ``scripts/spikes/rigging/weight_transfer_numpy.py``; its closest point step is Ericson, Real-Time Collision Detection,
# section 5.1.5.
"""Skin weight transfer between two meshes, with no Houdini (docs/design/character-rigging.md sections 2.1 to 2.5).

All of it is numpy (scipy is not in hython). The steps:

1. **First pass.** For each target point, the closest point on the source surface. It is a match when it is within
   ``distance`` (a share of the target's bounding-box diagonal) and the normals differ by at most ``angle``. A match gets
   the source weights (barycentric). The other points get a harmonic fill: weights that minimise a cotangent Laplacian
   energy with the matched rows fixed (conjugate gradients, one column per bone, all at once).
2. **Rigid pieces** (Accessory Weights, D41). Each connected piece of the target group takes the area-weighted average of the first-pass weights it lies on, then
   either one dominant bone (weight 1) or that average as one fixed blend for every point. A ``...Roll`` (twist)
   weight counts for the next bone down the chain that is not a twist (D37 Q12). A chosen bone wins.
3. **Non-rigid pieces** (Accessory Weights). Each point takes the source weights of the surface under it (the first pass
   machinery, with the source group as the surface); the weights are then smoothed along the piece's own edges.

A weight set is a dense matrix (points x bones), as in ``weights``. Only numpy features of numpy 1.26 and 2.x are used.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

__all__ = ["ORIGIN_NONE", "ORIGIN_COPIED", "ORIGIN_FILLED", "ORIGIN_RIGID", "ORIGIN_NONRIGID", "Settings", "Piece",
           "TransferResult", "point_components", "drop_small_pieces", "vertex_normals", "closest_points",
           "surface_weights", "cotan_edges", "Laplacian", "inpaint", "first_pass", "transfer_body", "accessory_layer",
           "twist_targets", "is_twist"]

#: ``a3ob_rig_origin`` values: where the weights of a point came from.
ORIGIN_NONE = 0
ORIGIN_COPIED = 1
ORIGIN_FILLED = 2
ORIGIN_RIGID = 3
ORIGIN_NONRIGID = 4


@dataclasses.dataclass
class Settings:
    #: ``D``: the largest distance of a match, as a share of the bounding-box diagonal of the target (paper: 0.05).
    distance: float = 0.05
    #: ``Theta``: the largest angle between the normals of a match, in degrees (paper: 35).
    angle: float = 35.0
    #: "dominant" (one bone, weight 1), "blend" (one fixed row) or "bone" (a chosen bone) for each rigid piece.
    rigid_mode: str = "dominant"
    merge_twist: bool = True
    #: Smoothing passes along a non-rigid piece.
    smooth: int = 8
    #: Pieces with fewer points than this are ignored (the sample's proxy triangles are 3 points).
    minpiece: int = 10
    #: Conjugate gradient stop: the residual relative to the right-hand side, and the most iterations.
    tolerance: float = 1e-5
    max_iterations: int = 500


@dataclasses.dataclass
class Piece:
    """One connected piece of the rigid or non-rigid layer."""
    number: int
    layer: str  # "rigid" or "nonrigid"
    points: int
    #: The bone of a rigid piece in dominant mode (or its override), else -1.
    bone: int = -1
    #: Why this piece got its bone: "dominant", "override", "blend", "smooth", or "none" when it found no weight.
    how: str = ""


@dataclasses.dataclass
class TransferResult:
    weights: np.ndarray  # points x bones; rows of points no layer touched are zero
    origin: np.ndarray  # int per point, the ORIGIN_* values
    piece: np.ndarray  # int per point: the piece number, -1 outside the pieces
    pieces: List[Piece]
    info: Dict[str, int]


# ---------------------------------------------------------------------------------------------------------------------
# Connected pieces
# ---------------------------------------------------------------------------------------------------------------------

def point_components(count: int, tris: np.ndarray) -> np.ndarray:
    """A component number for each of ``count`` points; points are joined by the triangles. A point in no triangle is a
    component of its own. The numbers run 0.. in order of the lowest point of each component."""
    parent = list(range(count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, c in np.asarray(tris, dtype=np.int64).reshape(-1, 3).tolist():
        ra, rb, rc = find(a), find(b), find(c)
        if ra != rb:
            parent[ra] = rb
        rc = find(c)
        rb = find(b)
        if rc != rb:
            parent[rc] = rb
    roots = np.array([find(i) for i in range(count)], dtype=np.int64)
    _, first, inverse = np.unique(roots, return_index=True, return_inverse=True)
    order = np.argsort(np.argsort(first))
    return order[inverse.reshape(-1)]


def drop_small_pieces(count: int, tris: np.ndarray, min_points: int) -> np.ndarray:
    """The triangles that are in a connected piece of at least ``min_points`` points. The sample body has loose
    3-point triangles that only carry proxy positions; they are not skin."""
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    if not len(tris) or min_points <= 1:
        return tris
    labels = point_components(count, tris)
    sizes = np.bincount(labels, minlength=int(labels.max()) + 1)
    return tris[sizes[labels[tris[:, 0]]] >= min_points]


# ---------------------------------------------------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------------------------------------------------

def vertex_normals(P: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals; a point in no triangle gets (0, 0, 0)."""
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    face = np.cross(b - a, c - a)
    n = np.zeros_like(P)
    for k in range(3):
        np.add.at(n, tris[:, k], face)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.maximum(length, 1e-20)


def _closest_bary(p, a, b, c) -> np.ndarray:
    """Barycentric coordinates of the closest point on triangles ``abc`` to ``p`` (Ericson 5.1.5), vectorised."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = p - b
    d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = p - c
    d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    k = len(p)
    out = np.zeros((k, 3))
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = 1.0 / (va + vb + vc)
        v = vb * denom
        w = vc * denom
        out[:] = np.stack([1 - v - w, v, w], axis=1)
        m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        out[m] = np.stack([np.zeros(k), 1 - t, t], axis=1)[m]
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        t = d2 / (d2 - d6)
        out[m] = np.stack([1 - t, np.zeros(k), t], axis=1)[m]
        m = (d6 >= 0) & (d5 <= d6)
        out[m] = np.array([0.0, 0.0, 1.0])
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        t = d1 / (d1 - d3)
        out[m] = np.stack([1 - t, t, np.zeros(k)], axis=1)[m]
        m = (d3 >= 0) & (d4 <= d3)
        out[m] = np.array([0.0, 1.0, 0.0])
        m = (d1 <= 0) & (d2 <= 0)
        out[m] = np.array([1.0, 0.0, 0.0])
    # A degenerate triangle can leave nan: fall back to its first corner.
    bad = ~np.isfinite(out).all(axis=1)
    out[bad] = np.array([1.0, 0.0, 0.0])
    return out


def closest_points(Q: np.ndarray, P: np.ndarray, tris: np.ndarray, candidates: int = 24
                   ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each query point the closest point on the triangle mesh: ``(triangle, barycentric (k, 3), distance)``.

    The triangles with the smallest (centroid distance - triangle radius), a lower bound of the real distance, are the
    candidates; the exact closest point is taken among them. This stands in for a bounding-volume tree (no scipy
    KD-tree in hython). The #60 spike checked it against brute force: none of 200 sampled queries missed."""
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    centroid = (a + b + c) / 3.0
    radius = np.maximum.reduce([np.linalg.norm(x - centroid, axis=1) for x in (a, b, c)])
    count = len(tris)
    candidates = min(candidates, count)
    centroid_sq = (centroid * centroid).sum(1)
    tri_out = np.zeros(len(Q), dtype=np.int64)
    bary_out = np.zeros((len(Q), 3))
    dist_out = np.zeros(len(Q))
    chunk = max(16, int(3e6 // max(count, 1)))
    for start in range(0, len(Q), chunk):
        q = Q[start:start + chunk]
        k = len(q)
        squared = (q * q).sum(1)[:, None] + centroid_sq[None, :] - 2.0 * (q @ centroid.T)
        bound = np.sqrt(np.maximum(squared, 0.0)) - radius[None, :]
        if candidates < count:
            cand = np.argpartition(bound, candidates - 1, axis=1)[:, :candidates]
        else:
            cand = np.tile(np.arange(count), (k, 1))
        flat_q = np.repeat(q, candidates, axis=0)
        flat_t = cand.reshape(-1)
        bary = _closest_bary(flat_q, a[flat_t], b[flat_t], c[flat_t])
        pt = bary[:, :1] * a[flat_t] + bary[:, 1:2] * b[flat_t] + bary[:, 2:3] * c[flat_t]
        dist = np.linalg.norm(flat_q - pt, axis=1).reshape(k, candidates)
        best = dist.argmin(1)
        rows = np.arange(k)
        tri_out[start:start + k] = cand[rows, best]
        bary_out[start:start + k] = bary.reshape(k, candidates, 3)[rows, best]
        dist_out[start:start + k] = dist[rows, best]
    return tri_out, bary_out, dist_out


def surface_weights(Q: np.ndarray, P: np.ndarray, tris: np.ndarray, W: np.ndarray
                    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The weights ``W`` interpolated at the closest point of each query on the surface ``tris`` of ``P``:
    ``(weights, distance, triangle, barycentric)``."""
    tri, bary, dist = closest_points(Q, P, tris)
    corner = tris[tri]
    at = (bary[:, :, None] * W[corner]).sum(1)
    return at, dist, tri, bary


def cotan_edges(P: np.ndarray, tris: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Edges ``(i, j, w)`` with cotangent weights (each edge once). The weights are kept between a small positive floor
    and 100, so a flat or needle triangle cannot make the solve singular."""
    out_i, out_j, out_w = [], [], []
    for k in range(3):
        i, j, l = tris[:, k], tris[:, (k + 1) % 3], tris[:, (k + 2) % 3]
        e1, e2 = P[j] - P[i], P[l] - P[i]
        cross = np.linalg.norm(np.cross(e1, e2), axis=1)
        cot = np.minimum((e1 * e2).sum(1) / np.maximum(cross, 1e-20), 100.0)
        out_i.append(j)
        out_j.append(l)
        out_w.append(0.5 * cot)
    i, j, w = np.concatenate(out_i), np.concatenate(out_j), np.concatenate(out_w)
    lo, hi = np.minimum(i, j), np.maximum(i, j)
    size = int(max(lo.max(), hi.max())) + 1
    key = lo.astype(np.int64) * size + hi
    order = np.argsort(key)
    key, w = key[order], w[order]
    uniq, start = np.unique(key, return_index=True)
    wsum = np.add.reduceat(w, start)
    floor = 1e-3 * np.abs(wsum).mean()
    return uniq // size, uniq % size, np.maximum(wsum, floor)


def uniform_edges(tris: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The edges of the triangles, each once, all with weight 1."""
    i = np.concatenate([tris[:, 0], tris[:, 1], tris[:, 2]])
    j = np.concatenate([tris[:, 1], tris[:, 2], tris[:, 0]])
    lo, hi = np.minimum(i, j), np.maximum(i, j)
    size = int(max(lo.max(), hi.max())) + 1
    uniq = np.unique(lo.astype(np.int64) * size + hi)
    return uniq // size, uniq % size, np.ones(len(uniq))


class Laplacian:
    """A symmetric graph Laplacian from an edge list, with a matrix product on (n, B) arrays (numpy only)."""

    def __init__(self, n: int, ei: np.ndarray, ej: np.ndarray, w: np.ndarray):
        self.n = n
        rows = np.concatenate([ei, ej])
        cols = np.concatenate([ej, ei])
        data = np.concatenate([w, w])
        order = np.argsort(rows, kind="stable")
        self.rows, self.cols, self.data = rows[order], cols[order], data[order]
        counts = np.bincount(self.rows, minlength=n)
        self.has_row = counts > 0
        self.starts = np.concatenate([[0], np.cumsum(counts)[:-1]])[self.has_row]
        self.degree = np.bincount(self.rows, weights=self.data, minlength=n)

    def adjacency_times(self, X: np.ndarray) -> np.ndarray:
        out = np.zeros_like(X)
        if len(self.data):
            out[self.has_row] = np.add.reduceat(self.data[:, None] * X[self.cols], self.starts, axis=0)
        return out

    def times(self, X: np.ndarray) -> np.ndarray:
        return self.degree[:, None] * X - self.adjacency_times(X)


def inpaint(lap: Laplacian, known: np.ndarray, X_known: np.ndarray, tolerance: float = 1e-5, max_iter: int = 500
            ) -> Tuple[np.ndarray, int]:
    """Solve ``L_uu X_u = -L_um X_m`` for the unknown rows (the harmonic fill). Returns ``(X, iterations)``.

    ``known`` is a boolean per row; ``X_known`` holds the fixed rows (the others are ignored). Jacobi-preconditioned
    conjugate gradients. Every unknown row must have an edge and be connected to a known row: the caller takes the
    others out of the system.

    Speed (task #72): the system is cut down to the unknown rows only, and the matrix product is one gather per
    neighbour slot (rows are sorted by how many unknown neighbours they have, so a slot touches only the rows that have
    it). A bone column with nothing to spread is not solved, and a column drops out as soon as it has converged. The
    columns do not talk to each other, so each one still takes the steps it would have taken."""
    n, B = X_known.shape
    unknown = ~known
    X = np.where(known[:, None], X_known, 0.0)
    if not unknown.any():
        return X, 0
    rows_u = np.nonzero(unknown)[0]
    nu = len(rows_u)
    # The right-hand side: the pull of the known neighbours (X is zero on the unknown rows here).
    rhs = lap.adjacency_times(X)[rows_u]
    # The unknown-unknown part of the matrix, as slots.
    position = np.full(n, -1, dtype=np.int64)
    position[rows_u] = np.arange(nu)
    r_row, r_col = position[lap.rows], position[lap.cols]
    inner = (r_row >= 0) & (r_col >= 0)
    r_row, r_col, r_val = r_row[inner], r_col[inner], lap.data[inner]
    degree_inner = np.bincount(r_row, minlength=nu)
    order = np.argsort(-degree_inner, kind="stable")  # new place -> unknown row
    rank = np.empty(nu, dtype=np.int64)
    rank[order] = np.arange(nu)
    new_row, new_col = rank[r_row], rank[r_col]
    by_row = np.argsort(new_row, kind="stable")
    new_row, new_col, r_val = new_row[by_row], new_col[by_row], r_val[by_row]
    first = np.concatenate([[0], np.cumsum(degree_inner[order])[:-1]])
    slot = np.arange(len(new_row)) - first[new_row]
    slots = []  # (neighbour, weight) per slot; slot k covers the first rows that have more than k neighbours
    for k in range(int(degree_inner.max()) if nu else 0):
        take = slot == k
        slots.append((new_col[take], r_val[take][:, None]))
    diag = lap.degree[rows_u][order][:, None]
    rhs = rhs[order]

    def apply(P):
        out = diag * P
        for neighbour, weight in slots:
            out[:len(neighbour)] -= weight * P[neighbour]
        return out

    norm0 = np.sqrt((rhs * rhs).sum(0))
    live = np.nonzero(norm0 > 1e-30)[0]  # the columns that have anything to spread
    norm0 = norm0[live]
    result = np.zeros((nu, B))
    it = 0
    if len(live):
        rhs = np.ascontiguousarray(rhs[:, live])
        x = np.zeros_like(rhs)
        r = rhs.copy()
        z = r / diag
        p = z.copy()
        rz = (r * z).sum(0)
        for it in range(1, max_iter + 1):
            Ap = apply(p)
            alpha = rz / np.maximum((p * Ap).sum(0), 1e-30)
            x += alpha * p
            r -= alpha * Ap
            done = np.sqrt((r * r).sum(0)) / norm0 < tolerance
            if done.any():
                result[:, live[done]] = x[:, done]
                keep = ~done
                if not keep.any():
                    live = live[:0]
                    break
                live, norm0, x, r, p, rz = live[keep], norm0[keep], x[:, keep], r[:, keep], p[:, keep], rz[keep]
            z = r / diag
            rz_new = (r * z).sum(0)
            p = z + (rz_new / np.maximum(rz, 1e-30)) * p
            rz = rz_new
        if len(live):  # out of iterations
            result[:, live] = x
    X[rows_u[order]] = result
    return X, it


# ---------------------------------------------------------------------------------------------------------------------
# Layer 1: the first pass
# ---------------------------------------------------------------------------------------------------------------------

def _normalise_rows(W: np.ndarray) -> np.ndarray:
    total = W.sum(axis=1, keepdims=True)
    return np.divide(W, total, out=np.zeros_like(W), where=total > 1e-9)


def first_pass(src_P: np.ndarray, src_tris: np.ndarray, src_W: np.ndarray, dst_P: np.ndarray, dst_tris: np.ndarray,
               active: np.ndarray, reach: float, settings: Settings, far_nearest: bool = False
               ) -> Tuple[np.ndarray, np.ndarray, Dict[str, int]]:
    """Closest point match and inpainting for the ``active`` points of the target (``dst_tris`` are the triangles
    between them). ``reach`` is the largest distance of a match in the units of the points. Returns
    ``(weights, origin, info)``; inactive points get zeros and ORIGIN_NONE.

    A point whose component has no match at all cannot be filled from its neighbours; it takes the weights of its
    closest source point without the thresholds (``info["fallback"]``). With ``far_nearest`` a point farther than
    ``reach`` does the same, whatever its neighbours do (the accessory non-rigid mode, D41)."""
    count, bones = len(dst_P), src_W.shape[1]
    info = {"matched": 0, "filled": 0, "fallback": 0, "rejected_distance": 0, "rejected_normal": 0,
            "rejected_unweighted": 0}
    weights = np.zeros((count, bones))
    origin = np.zeros(count, dtype=np.int64)
    index = np.nonzero(active)[0]
    if not len(index):
        return weights, origin, info
    tri, bary, dist = closest_points(dst_P[index], src_P, src_tris)
    src_n = vertex_normals(src_P, src_tris)
    dst_n = vertex_normals(dst_P, dst_tris) if len(dst_tris) else np.zeros_like(dst_P)
    corner = src_tris[tri]
    n_at = (bary[:, :, None] * src_n[corner]).sum(1)
    n_at /= np.maximum(np.linalg.norm(n_at, axis=1, keepdims=True), 1e-20)
    cos = (n_at * dst_n[index]).sum(1)
    at = (bary[:, :, None] * src_W[corner]).sum(1)
    ok_dist = dist <= reach
    ok_normal = cos >= np.cos(np.radians(settings.angle))
    ok_weight = at.sum(1) >= 0.5  # not one of the few unweighted source points
    matched = ok_dist & ok_normal & ok_weight
    info["rejected_distance"] = int((~ok_dist).sum())
    info["rejected_normal"] = int((ok_dist & ~ok_normal).sum())
    info["rejected_unweighted"] = int((ok_dist & ok_normal & ~ok_weight).sum())

    known = np.ones(count, dtype=bool)  # points that are not active are fixed at zero and have no edge
    fixed = np.zeros((count, bones))
    known[index] = matched
    fixed[index] = np.where(matched[:, None], at, 0.0)
    labels = point_components(count, dst_tris) if len(dst_tris) else np.arange(count)
    has_match = np.zeros(int(labels.max()) + 1, dtype=bool)
    has_match[labels[index[matched]]] = True
    stuck = np.zeros(count, dtype=bool)
    stuck[index] = ~matched & (~has_match[labels[index]] | (far_nearest & ~ok_dist))
    known |= stuck
    fixed[index] = np.where(stuck[index][:, None], at, fixed[index])
    info["matched"] = int(matched.sum())
    info["fallback"] = int(stuck.sum())
    info["filled"] = int(len(index) - matched.sum() - stuck.sum())

    if len(dst_tris):
        ei, ej, w = cotan_edges(dst_P, dst_tris)
        lap = Laplacian(count, ei, ej, w)
        filled, info["iterations"] = inpaint(lap, known, fixed, settings.tolerance, settings.max_iterations)
    else:
        filled, info["iterations"] = fixed, 0
    filled[~active] = 0.0
    weights = _normalise_rows(np.maximum(filled, 0.0))
    origin[index] = ORIGIN_FILLED
    origin[index[matched]] = ORIGIN_COPIED
    return weights, origin, info


# ---------------------------------------------------------------------------------------------------------------------
# Layers 2 and 3: pieces
# ---------------------------------------------------------------------------------------------------------------------

def is_twist(name: str) -> bool:
    """A twist bone is named ``...Roll`` (``LeftArmRoll``), ignoring case."""
    return name.lower().endswith("roll")


def twist_targets(bone_names: Sequence[str], parents: Sequence[int]) -> List[int]:
    """For each bone, the bone its weight goes to when twist weights are merged (D37 Q12): a bone that is not a twist
    stays itself; a twist goes to the first child that is not a twist, going on down through twist children. A twist
    with no such child stays itself. ``parents[i]`` is the parent bone number or -1."""
    children: Dict[int, List[int]] = {}
    for bone, parent in enumerate(parents):
        if parent >= 0:
            children.setdefault(int(parent), []).append(bone)
    out = list(range(len(bone_names)))
    for bone, name in enumerate(bone_names):
        current, seen = bone, {bone}
        while is_twist(bone_names[current]):
            below = [c for c in children.get(current, []) if c not in seen]
            if not below:
                current = bone
                break
            nxt = next((c for c in below if not is_twist(bone_names[c])), below[0])
            seen.add(nxt)
            current = nxt
        out[bone] = current
    return out


def _piece_points(count: int, tris: np.ndarray, mask: np.ndarray, minpiece: int
                  ) -> Tuple[List[Tuple[np.ndarray, np.ndarray]], int]:
    """The connected pieces of the triangles whose three points are in ``mask``: a list of ``(points, triangles)`` in the
    order of their lowest point, and the number of pieces that were ignored for having fewer than ``minpiece`` points.
    Points of the mask that are in no triangle are single-point pieces."""
    own = tris[mask[tris].all(axis=1)] if len(tris) else tris.reshape(0, 3)
    labels = point_components(count, own) if len(own) else np.arange(count)
    in_mask = np.nonzero(mask)[0]
    pieces, ignored = [], 0
    if not len(in_mask):
        return pieces, ignored
    order = np.argsort(labels[in_mask], kind="stable")
    sorted_points = in_mask[order]
    cut = np.nonzero(np.diff(labels[sorted_points]))[0] + 1
    groups = np.split(sorted_points, cut)
    groups.sort(key=lambda g: int(g.min()))
    tri_label = labels[own[:, 0]] if len(own) else np.zeros(0, dtype=np.int64)
    for points in groups:
        if len(points) < max(minpiece, 1):
            ignored += 1
            continue
        pieces.append((points, own[tri_label == labels[points[0]]]))
    return pieces, ignored


def _point_areas(P: np.ndarray, tris: np.ndarray, points: np.ndarray) -> np.ndarray:
    """A third of the area of the triangles around each of ``points`` (never exactly zero, so an average always works)."""
    if not len(tris):
        return np.ones(len(points))
    area = 0.5 * np.linalg.norm(np.cross(P[tris[:, 1]] - P[tris[:, 0]], P[tris[:, 2]] - P[tris[:, 0]]), axis=1)
    share = np.zeros(len(P))
    for k in range(3):
        np.add.at(share, tris[:, k], area / 3.0)
    out = share[points]
    return np.maximum(out, 1e-12 * max(float(out.max()), 1e-12))


def _smooth(weights: np.ndarray, tris: np.ndarray, points: np.ndarray, passes: int) -> np.ndarray:
    """``passes`` rounds of averaging each point with its neighbours along the piece's own edges (half way each time)."""
    if passes <= 0 or not len(tris):
        return weights
    out = weights.copy()
    ei, ej, w = uniform_edges(tris)
    lap = Laplacian(len(weights), ei, ej, w)
    inside = np.zeros(len(weights), dtype=bool)
    inside[points] = True
    use = inside & (lap.degree > 0)
    for _ in range(passes):
        mean = lap.adjacency_times(out)[use] / lap.degree[use][:, None]
        out[use] = 0.5 * out[use] + 0.5 * mean
    return _normalise_rows(out)


def transfer_body(src_P: np.ndarray, src_tris: np.ndarray, src_W: np.ndarray, dst_P: np.ndarray, dst_tris: np.ndarray,
                  body: np.ndarray, settings: Settings) -> TransferResult:
    """The Weight Transfer node (D41): the weights of the sample body onto the ``body`` points of the target (a boolean
    mask), by closest point and inpainting. Points outside the mask get zeros and ORIGIN_NONE; the caller leaves them as
    they are. Raises ``ValueError`` with a plain message when there is nothing to work on."""
    count = len(dst_P)
    src_tris = drop_small_pieces(len(src_P), src_tris, settings.minpiece)
    if not len(src_tris):
        raise ValueError("the source body has no polygons")
    dst_tris = np.asarray(dst_tris, dtype=np.int64).reshape(-1, 3)
    if not body.any():
        raise ValueError("no target point is in the body group")
    body_tris = dst_tris[body[dst_tris].all(axis=1)] if len(dst_tris) else dst_tris
    reach = settings.distance * float(np.linalg.norm(dst_P[body].max(0) - dst_P[body].min(0)))
    weights, origin, info = first_pass(src_P, src_tris, src_W, dst_P, body_tris, body, reach, settings)
    return TransferResult(weights, origin, -np.ones(count, dtype=np.int64), [], info)


def accessory_layer(P: np.ndarray, tris: np.ndarray, W: np.ndarray, source: np.ndarray, target: np.ndarray, mode: str,
                    twist: Optional[Sequence[int]], bone: int, settings: Settings) -> TransferResult:
    """The Accessory Weights node (D41). One mesh (``P``, ``tris``) holds both groups: ``source`` (points that have the
    weights ``W``, points x bones) and ``target`` (the points to weight), boolean masks that must not overlap. ``mode`` is
    "rigid" (``settings.rigid_mode`` "dominant", "blend" or "bone"; for "bone" the bone number is ``bone`` and the source
    is not used) or "nonrigid". ``twist`` is ``twist_targets`` (None: no merge).

    Rigid: each connected piece of the target takes the area-weighted average of the source weights near it, then one
    bone or one fixed blend. Non-rigid: each point takes the source weights under it (distance and angle limits as the
    body pass; a point with no source in reach takes the nearest source point), smoothed along the piece.
    ``info["fallback"]`` counts the target points that took the nearest source point because none was in reach.
    Raises ``ValueError`` with a plain message when there is nothing to work on."""
    count, bones = len(P), W.shape[1]
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    info = {"matched": 0, "filled": 0, "fallback": 0, "rejected_distance": 0, "rejected_normal": 0,
            "rejected_unweighted": 0, "rigid_pieces": 0, "nonrigid_pieces": 0, "ignored_pieces": 0,
            "unweighted_pieces": 0, "far_points": 0}
    weights = np.zeros((count, bones))
    origin = np.zeros(count, dtype=np.int64)
    piece_of = -np.ones(count, dtype=np.int64)
    pieces: List[Piece] = []
    if not target.any():
        raise ValueError("no point is in the target group")
    use_source = not (mode == "rigid" and settings.rigid_mode == "bone")
    src_tris = tris[source[tris].all(axis=1)] if len(tris) else tris
    if use_source and not len(src_tris):
        raise ValueError("the source group has no polygons")
    used = target | source if use_source else target  # the size that the distance is a share of
    reach = settings.distance * float(np.linalg.norm(P[used].max(0) - P[used].min(0)))
    found, info["ignored_pieces"] = _piece_points(count, tris, target, settings.minpiece)
    if mode == "rigid":
        merge = np.zeros((bones, bones))
        merge[np.arange(bones), np.arange(bones) if twist is None or not settings.merge_twist else list(twist)] = 1.0
        for points, piece_tris in found:
            piece = Piece(len(pieces), "rigid", len(points))
            if use_source:
                at, dist, _tri, _bary = surface_weights(P[points], P, src_tris, W)
            else:
                at, dist = np.zeros((len(points), bones)), np.zeros(len(points))
            weights[points] = _rigid_piece(piece, points, piece_tris, at, dist, reach, P, bone, merge, settings, bones,
                                           info)
            origin[points] = ORIGIN_RIGID
            piece_of[points] = piece.number
            info["rigid_pieces"] += 1
            info["unweighted_pieces"] += int(piece.how == "none")
            pieces.append(piece)
    else:
        active = np.zeros(count, dtype=bool)
        for points, _piece_tris in found:
            active[points] = True
        if active.any():
            active_tris = tris[active[tris].all(axis=1)] if len(tris) else tris
            norm = _normalise_rows(W)
            weights, _origin, got = first_pass(P, src_tris, norm, P, active_tris, active, reach, settings, True)
            for key in ("matched", "filled", "fallback", "rejected_distance", "rejected_normal", "rejected_unweighted"):
                info[key] = got[key]
        for points, piece_tris in found:
            piece = Piece(len(pieces), "nonrigid", len(points), how="smooth")
            weights[points] = _smooth(weights, piece_tris, points, settings.smooth)[points]
            if not (weights[points].sum(axis=1) > 0).any():
                piece.how = "none"
            origin[points] = ORIGIN_NONRIGID
            piece_of[points] = piece.number
            info["nonrigid_pieces"] += 1
            info["unweighted_pieces"] += int(piece.how == "none")
            pieces.append(piece)
    return TransferResult(weights, origin, piece_of, pieces, info)


def _rigid_piece(piece: Piece, points, tris, at, dist, reach, dst_P, bone, merge, settings, bones, info) -> np.ndarray:
    """The weight rows of every point of a rigid piece."""
    if settings.rigid_mode == "bone":
        piece.bone, piece.how = bone, "chosen"
        row = np.zeros(bones)
        row[bone] = 1.0
        return np.tile(row, (len(points), 1))
    near = dist <= reach
    info["far_points"] += int((~near).sum())
    if not near.any():  # nothing in reach: the nearest source weights count
        near = np.ones(len(points), dtype=bool)
        info["fallback"] += len(points)
    area = _point_areas(dst_P, tris, points)
    average = (area[near, None] * at[near]).sum(0)
    if average.sum() <= 1e-9:
        piece.how = "none"
        return np.zeros((len(points), bones))
    average /= average.sum()
    if settings.rigid_mode == "blend":
        piece.how = "blend"
        return np.tile(average, (len(points), 1))
    merged = average @ merge
    piece.bone, piece.how = int(merged.argmax()), "dominant"
    row = np.zeros(bones)
    row[piece.bone] = 1.0
    return np.tile(row, (len(points), 1))

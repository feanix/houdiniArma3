# Spike for task #60 (character rigging design): skin weight transfer by closest point plus Laplacian inpainting, after
# Abdrashitov et al., "Robust Skin Weights Transfer via Weight Inpainting", SIGGRAPH Asia 2023 Technical Communications
# (DOI not checked here). Only numpy is required (scipy is used only if it imports and --scipy is
# given, to compare speed). This is a measuring spike, not product code. The paper's method is re-implemented here
# from its description; no code of the authors is used.
#
# Run (hython, numpy 2.3.2):
#   "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/spikes/rigging/weight_transfer_numpy.py
# or with a normal Python that has numpy (add --scipy to time scipy.sparse.linalg.cg as well):
#   python scripts/spikes/rigging/weight_transfer_numpy.py --scipy
# Environment: A3OB_SAMPLES = Samples root (the Samples are read at run time, nothing is stored).
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, HERE)

import sample_facts  # noqa: E402
from a3ob_core.p3d.model import decode_weight  # noqa: E402
from a3ob_core.p3d.reader import read_p3d_file  # noqa: E402


# ---------------------------------------------------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------------------------------------------------

def load_sample_lod(index):
    """(P (n,3), tris (m,3), W (n,B), bone_names) of one LOD of the sample body. Quads become two triangles."""
    addons = sample_facts.ADDONS
    cfg = os.path.join(addons, "Test_Character_01", "model.cfg")
    bones = sample_facts.parse_skeleton(cfg, "OFP2_ManSkeleton")
    model = read_p3d_file(os.path.join(addons, "Test_Character_01", "A3_character_example.p3d"), strict=False)
    lod = model.lods[index]
    P = np.array(lod.points, dtype=np.float64).reshape(-1, 3)
    tris = []
    for face in lod.faces():
        v = face.vertices
        for k in range(1, len(v) - 1):
            tris.append((v[0], v[k], v[k + 1]))
    tris = np.array(tris, dtype=np.int64)
    by_lower = {name.lower(): name for name in bones}
    names = []
    columns = []
    table = np.array([decode_weight(b) for b in range(256)])
    for sel_name in lod.selection_names():
        if sel_name.lower() in by_lower:
            names.append(by_lower[sel_name.lower()])
            columns.append(table[np.frombuffer(bytes(lod.selection(sel_name).vertex_weights), dtype=np.uint8)])
    W = np.stack(columns, axis=1)
    return drop_small_pieces(P, tris, W, min_points=10) + (names,)


def drop_small_pieces(P, tris, W, min_points):
    """Remove connected pieces with fewer than ``min_points`` points. The sample has 3-point triangles that carry the
    proxy positions (weapon, launcher, hands, head ...); they are not skin. Returns compacted (P, tris, W)."""
    parent = list(range(len(P)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for t in tris:
        for k in (1, 2):
            a, b = find(int(t[0])), find(int(t[k]))
            if a != b:
                parent[a] = b
    roots = np.array([find(i) for i in range(len(P))])
    _, inverse, counts = np.unique(roots, return_inverse=True, return_counts=True)
    keep = counts[inverse] >= min_points
    remap = -np.ones(len(P), dtype=np.int64)
    remap[keep] = np.arange(int(keep.sum()))
    tris = remap[tris]
    tris = tris[(tris >= 0).all(1)]
    return P[keep], tris, W[keep]


# ---------------------------------------------------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------------------------------------------------

def vertex_normals(P, tris):
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    fn = np.cross(b - a, c - a)  # area weighted
    n = np.zeros_like(P)
    for k in range(3):
        np.add.at(n, tris[:, k], fn)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.maximum(length, 1e-20)


def closest_bary(p, a, b, c):
    """Closest point on triangles (Ericson, Real-Time Collision Detection 5.1.5), vectorised. Returns bary (k,3)."""
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
    return out


def closest_points(Q, P, tris, candidates=24, chunk=256):
    """For each query point the closest point on the triangle mesh: (tri index, bary, distance).

    Candidate triangles are the ``candidates`` with the smallest (centroid distance - triangle radius), then the exact closest point is taken among them.
    This is a stand-in for a BVH (no scipy KD-tree in hython); ``verify_closest`` measures how often it misses.
    """
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    centroid = (a + b + c) / 3.0
    radius = np.maximum.reduce([np.linalg.norm(x - centroid, axis=1) for x in (a, b, c)])
    tri_out = np.zeros(len(Q), dtype=np.int64)
    bary_out = np.zeros((len(Q), 3))
    dist_out = np.zeros(len(Q))
    for start in range(0, len(Q), chunk):
        q = Q[start:start + chunk]
        d2 = np.sqrt(((q[:, None, :] - centroid[None, :, :]) ** 2).sum(2)) - radius[None, :]  # lower bound on distance
        cand = np.argpartition(d2, candidates, axis=1)[:, :candidates]  # (k, candidates)
        k = len(q)
        flat_q = np.repeat(q, candidates, axis=0)
        flat_t = cand.reshape(-1)
        bary = closest_bary(flat_q, a[flat_t], b[flat_t], c[flat_t])
        pt = bary[:, :1] * a[flat_t] + bary[:, 1:2] * b[flat_t] + bary[:, 2:3] * c[flat_t]
        dist = np.linalg.norm(flat_q - pt, axis=1).reshape(k, candidates)
        best = dist.argmin(1)
        rows = np.arange(k)
        tri_out[start:start + k] = cand[rows, best]
        bary_out[start:start + k] = bary.reshape(k, candidates, 3)[rows, best]
        dist_out[start:start + k] = dist[rows, best]
    return tri_out, bary_out, dist_out


def verify_closest(Q, P, tris, found_dist, sample=200, seed=1):
    """Fraction of a random sample of queries where the full brute-force search finds a closer triangle."""
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(Q), size=min(sample, len(Q)), replace=False)
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    misses = 0
    for i in idx:
        q = np.repeat(Q[i][None, :], len(tris), axis=0)
        bary = closest_bary(q, a, b, c)
        pt = bary[:, :1] * a + bary[:, 1:2] * b + bary[:, 2:3] * c
        best = np.linalg.norm(q - pt, axis=1).min()
        if best < found_dist[i] - 1e-9:
            misses += 1
    return misses / len(idx)


def cotan_edges(P, tris):
    """Edge list (i, j, w) with cotangent weights (both directions are NOT duplicated), clamped to a small positive."""
    out_i, out_j, out_w = [], [], []
    for k in range(3):
        i, j, l = tris[:, k], tris[:, (k + 1) % 3], tris[:, (k + 2) % 3]
        e1, e2 = P[j] - P[i], P[l] - P[i]
        cross = np.linalg.norm(np.cross(e1, e2), axis=1)
        cot = (e1 * e2).sum(1) / np.maximum(cross, 1e-20)
        out_i.append(j)
        out_j.append(l)
        out_w.append(0.5 * cot)
    i, j, w = np.concatenate(out_i), np.concatenate(out_j), np.concatenate(out_w)
    lo, hi = np.minimum(i, j), np.maximum(i, j)
    key = lo.astype(np.int64) * (int(max(lo.max(), hi.max())) + 1) + hi
    order = np.argsort(key)
    key, w = key[order], w[order]
    uniq, start = np.unique(key, return_index=True)
    wsum = np.add.reduceat(w, start)
    size = int(max(lo.max(), hi.max())) + 1
    ei, ej = uniq // size, uniq % size
    floor = 1e-3 * np.abs(wsum).mean()
    return ei, ej, np.maximum(wsum, floor)


class Laplacian:
    """Symmetric graph Laplacian from an edge list, with a matrix-vector product on (n, B) arrays (numpy only)."""

    def __init__(self, n, ei, ej, w):
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

    def adjacency_times(self, X):
        contrib = self.data[:, None] * X[self.cols]
        out = np.zeros_like(X)
        out[self.has_row] = np.add.reduceat(contrib, self.starts, axis=0)
        return out

    def times(self, X):
        return self.degree[:, None] * X - self.adjacency_times(X)


def inpaint(lap, known_mask, X_known, tol=1e-6, max_iter=500, use_scipy=False):
    """Solve L_uu X_u = -L_um X_m for the unknown vertices (harmonic fill). Returns (X, iterations, residual).

    Conjugate gradients with a Jacobi preconditioner, all columns at once, written with numpy only.
    X_known is (n, B) with the matched rows filled; the other rows are ignored.
    """
    n, B = X_known.shape
    U = ~known_mask
    X = np.where(known_mask[:, None], X_known, 0.0)
    if not U.any():
        return X, 0, 0.0
    rhs = -(lap.times(X))[U]  # L_um X_m with X_u = 0
    diag = lap.degree[U][:, None]

    def apply(Xu):
        full = np.zeros((n, B))
        full[U] = Xu
        return lap.times(full)[U]

    if use_scipy:
        import scipy.sparse as sp
        import scipy.sparse.linalg as spl
        mat = sp.csr_matrix((lap.data, (lap.rows, lap.cols)), shape=(n, n))
        luu = (sp.diags(lap.degree) - mat).tocsr()[U][:, U]
        sol = np.stack([spl.cg(luu, rhs[:, j], rtol=tol, maxiter=max_iter)[0] for j in range(B)], axis=1)
        X[U] = sol
        return X, -1, float(np.abs(luu @ sol - rhs).max())

    x = np.zeros_like(rhs)
    r = rhs - apply(x)
    z = r / diag
    p = z.copy()
    rz = (r * z).sum(0)
    norm0 = np.maximum(np.sqrt((rhs * rhs).sum(0)), 1e-30)
    it = 0
    for it in range(1, max_iter + 1):
        Ap = apply(p)
        alpha = rz / np.maximum((p * Ap).sum(0), 1e-30)
        x += alpha * p
        r -= alpha * Ap
        if (np.sqrt((r * r).sum(0)) / norm0).max() < tol:
            break
        z = r / diag
        rz_new = (r * z).sum(0)
        p = z + (rz_new / np.maximum(rz, 1e-30)) * p
        rz = rz_new
    X[U] = x
    return X, it, float(np.abs(r).max())


# ---------------------------------------------------------------------------------------------------------------------
# The transfer
# ---------------------------------------------------------------------------------------------------------------------

def transfer(src_P, src_tris, src_W, dst_P, dst_tris, max_distance, max_angle_deg=30.0, use_scipy=False):
    """Closest point transfer with distance and normal thresholds, then harmonic inpainting. Returns (W, info)."""
    info = {}
    t0 = time.perf_counter()
    tri, bary, dist = closest_points(dst_P, src_P, src_tris)
    info["t_closest"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    src_n = vertex_normals(src_P, src_tris)
    dst_n = vertex_normals(dst_P, dst_tris)
    corner = src_tris[tri]  # (k,3)
    n_at = (bary[:, :, None] * src_n[corner]).sum(1)
    n_at /= np.maximum(np.linalg.norm(n_at, axis=1, keepdims=True), 1e-20)
    cos = (n_at * dst_n).sum(1)
    W_at = (bary[:, :, None] * src_W[corner]).sum(1)
    ok_dist = dist <= max_distance
    ok_normal = cos >= np.cos(np.radians(max_angle_deg))
    ok_weight = W_at.sum(1) >= 0.5  # the source point is not one of the few unweighted points
    known = ok_dist & ok_normal & ok_weight
    info["t_threshold"] = time.perf_counter() - t0
    info["n"] = len(dst_P)
    info["matched"] = int(known.sum())
    info["rejected_distance"] = int((~ok_dist).sum())
    info["rejected_normal"] = int((ok_dist & ~ok_normal).sum())
    info["rejected_unweighted_source"] = int((ok_dist & ok_normal & ~ok_weight).sum())

    t0 = time.perf_counter()
    ei, ej, w = cotan_edges(dst_P, dst_tris)
    lap = Laplacian(len(dst_P), ei, ej, w)
    info["t_laplacian"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    W, iters, resid = inpaint(lap, known, W_at, use_scipy=use_scipy)
    info["t_inpaint"] = time.perf_counter() - t0
    info["cg_iterations"] = iters
    info["cg_residual"] = resid
    W = np.maximum(W, 0.0)
    total = W.sum(1, keepdims=True)
    W = np.where(total > 1e-9, W / np.maximum(total, 1e-9), W)
    info["known_mask"] = known
    info["t_total"] = info["t_closest"] + info["t_threshold"] + info["t_laplacian"] + info["t_inpaint"]
    return W, info


def subdivide_midpoint(P, tris, W):
    """One level of midpoint subdivision (every triangle into four). Weights of new points are the edge averages."""
    edge_id = {}
    newP, newW = [P], [W]
    count = len(P)
    out = []
    for t in tris:
        mids = []
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            key = (a, b) if a < b else (b, a)
            if key not in edge_id:
                edge_id[key] = count
                count += 1
                newP.append(((P[a] + P[b]) * 0.5)[None, :])
                newW.append(((W[a] + W[b]) * 0.5)[None, :])
            mids.append(edge_id[key])
        a, b, c = (int(x) for x in t)
        m_ab, m_bc, m_ca = mids
        out += [(a, m_ab, m_ca), (m_ab, b, m_bc), (m_ca, m_bc, c), (m_ab, m_bc, m_ca)]
    return np.concatenate(newP), np.array(out, dtype=np.int64), np.concatenate(newW)


def error_report(W, truth, label):
    """Compare weights with the truth on points that have truth weight; print and return a dict."""
    ok = truth.sum(1) > 0.5
    l1 = np.abs(W - truth).sum(1)[ok]
    dom_same = (W.argmax(1) == truth.argmax(1))[ok]
    infl = (W > 1.0 / 255).sum(1)
    res = {
        "l1_mean": float(l1.mean()), "l1_median": float(np.median(l1)), "l1_p95": float(np.percentile(l1, 95)),
        "l1_max": float(l1.max()), "dominant_changed": float(1.0 - dom_same.mean()),
        "over4": int((infl > 4).sum()), "zero_sum": int((W.sum(1) < 0.5).sum()),
    }
    print("   %-34s L1 mean %.4f median %.4f p95 %.4f max %.3f | dominant bone changed %.2f%% | >4 influences %d | "
          "unweighted %d" % (label, res["l1_mean"], res["l1_median"], res["l1_p95"], res["l1_max"],
                             100 * res["dominant_changed"], res["over4"], res["zero_sum"]))
    return res


def export_cleanup(W, max_influences=4):
    """What the Arma export clean-up does to dense weights: keep the strongest N per point, normalise, then snap to the
    1/254 grid of the P3D selection byte (see a3ob_core.p3d.model.encode_weight). Returns (cleaned W, stats)."""
    order = np.argsort(-W, axis=1)
    keep = np.zeros_like(W, dtype=bool)
    rows = np.arange(len(W))[:, None]
    keep[rows, order[:, :max_influences]] = True
    pruned = np.where(keep, W, 0.0)
    total = pruned.sum(1, keepdims=True)
    pruned = np.where(total > 1e-9, pruned / np.maximum(total, 1e-9), pruned)
    byte = np.clip(np.rint(255 - 254 * pruned), 1, 255)
    snapped = np.where(pruned > 0, (255 - byte) / 254.0, 0.0)
    snapped = np.where(pruned >= 1.0, 1.0, snapped)
    sums = snapped.sum(1)
    had = (W > 1e-6).sum(1)
    stats = {
        "points_over_limit": int((had > max_influences).sum()),
        "mean_abs_change": float(np.abs(pruned - W).sum(1).mean()),
        "max_abs_change": float(np.abs(pruned - W).sum(1).max()),
        "sum_deviation_max": float(np.abs(sums[sums > 0] - 1).max()),
        "sum_deviation_mean": float(np.abs(sums[sums > 0] - 1).mean()),
    }
    return snapped, stats


def make_target(P, scale, jitter, seed, centre):
    rng = np.random.default_rng(seed)
    return (P - centre) * np.array(scale) + centre + rng.normal(0.0, jitter, P.shape)


def main():
    use_scipy = "--scipy" in sys.argv
    if use_scipy:
        import scipy
        print("scipy %s will be used for the inpainting solve" % scipy.__version__)
    print("numpy %s, python %s" % (np.__version__, sys.version.split()[0]))
    src_P, src_tris, src_W, names = load_sample_lod(0)
    lod1_P, lod1_tris, lod1_W, _ = load_sample_lod(1)
    diag = float(np.linalg.norm(src_P.max(0) - src_P.min(0)))
    print("source: LOD Resolution 1, %d points, %d triangles, %d bones, bbox diagonal %.3f m" % (
        len(src_P), len(src_tris), len(names), diag))

    jitter = 0.002
    centre = (src_P.max(0) + src_P.min(0)) / 2.0
    cases = []
    # The mismatch sweep: scale (1+m/2, 1+m, 1+1.5m) about the bbox centre. m=0.10 is the (1.05, 1.10, 1.15) stress case.
    for m in (0.0, 0.02, 0.05, 0.10):
        scale = (1 + 0.5 * m, 1 + m, 1 + 1.5 * m)
        cases.append(("A m=%.2f same topology, scale %s about the bbox centre + %.0f mm jitter" % (m, scale, jitter * 1000),
                      make_target(src_P, scale, jitter, 1, centre), src_tris, src_W))
    scale = (1.05, 1.10, 1.15)
    sub_P, sub_tris, sub_W = subdivide_midpoint(lod1_P, lod1_tris, lod1_W)
    cases.append(("B LOD2 (Resolution 2) midpoint-subdivided",
                  make_target(sub_P, scale, jitter, 2, centre), sub_tris, sub_W))
    big_P, big_tris, big_W = subdivide_midpoint(src_P, src_tris, src_W)
    cases.append(("C LOD1 (Resolution 1) midpoint-subdivided (timing)", make_target(big_P, scale, jitter, 3, centre), big_tris, big_W))

    for name, P, tris, truth in cases:
        print("\n== target %s: %d points, %d triangles ==" % (name, len(P), len(tris)))
        for frac in (0.05,) if name.startswith("A") else (0.02, 0.05):
            W, info = transfer(src_P, src_tris, src_W, P, tris, max_distance=frac * diag, use_scipy=use_scipy)
            print("   thresholds: distance %.0f%% of diagonal (%.3f m), normal 30 deg" % (100 * frac, frac * diag))
            print("   matched %d, inpainted %d (rejected: distance %d, normal %d, unweighted source %d)" % (
                info["matched"], info["n"] - info["matched"], info["rejected_distance"], info["rejected_normal"],
                info["rejected_unweighted_source"]))
            print("   time: closest %.2fs, thresholds %.2fs, laplacian %.2fs, inpaint %.2fs (CG %d it, residual %.1e)"
                  " total %.2fs" % (info["t_closest"], info["t_threshold"], info["t_laplacian"], info["t_inpaint"],
                                    info["cg_iterations"], info["cg_residual"], info["t_total"]))
            error_report(W, truth, "inpainted + normalised")
            cleaned, stats = export_cleanup(W)
            print("   export clean-up (max 4, normalise, 1/254 grid): %(points_over_limit)d points had more than 4; "
                  "L1 change per point mean %(mean_abs_change).4f max %(max_abs_change).3f; sum deviation from 1 after "
                  "snapping: mean %(sum_deviation_mean).4f max %(sum_deviation_max).4f" % stats)
            # What the copied points alone look like, and what a plain nearest-vertex copy gives (no thresholds):
            known = info["known_mask"]
            error_report(np.where(known[:, None], W, 0.0), np.where(known[:, None], truth, 0.0), "matched points only")
        if len(P) < 12000:
            t0 = time.perf_counter()
            tri, bary, dist = closest_points(P[:600], src_P, src_tris)
            miss = verify_closest(P[:600], src_P, src_tris, dist)
            print("   candidate search check: %.1f%% of 200 sampled queries missed the true closest triangle" % (100 * miss))


if __name__ == "__main__":
    main()

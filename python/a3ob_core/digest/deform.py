# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the edge ratio is the edge part of ``a3ob_core.rig.check.edge_stress`` (docs/design/digests.md).
"""The deform-compare digest, with no Houdini (task #81): how the edges of mesh B are longer or shorter than the same
edges of mesh A (the two meshes have the same topology, for example a posed mesh and its unposed copy).

The ratio of an edge is its length in B over its length in A (1 is unchanged). Edges are counted by the region of their two
points: the region of a point is that of its dominant bone in A (``check.weight_problems``), so a mesh with no weights has no
regions, only the groups. An edge whose two points are in different regions is under ``between regions``.
"""

from typing import List, Optional, Sequence

import numpy as np

from a3ob_core.rig import check

from . import regions as region_rules
from .page import NR, Page, WrongQuestion, cm, count, num, percentiles, row
from .skeleton import Source

__all__ = ["edge_ratios", "ratio_stats", "dominant_labels", "deform_page", "bounds_size"]


def edge_ratios(a: np.ndarray, b: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """The length of each edge in ``b`` over its length in ``a``; NaN where the edge has no length in ``a``."""
    len_a = np.linalg.norm(a[edges[:, 0]] - a[edges[:, 1]], axis=1)
    len_b = np.linalg.norm(b[edges[:, 0]] - b[edges[:, 1]], axis=1)
    out = np.full(len(edges), np.nan)
    good = len_a > 1e-9
    out[good] = len_b[good] / len_a[good]
    return out


def ratio_stats(ratios: np.ndarray):
    """``(edges counted, median, p1, p99)`` of the finite ratios (the last three None when there are none)."""
    finite = ratios[np.isfinite(ratios)]
    p1, med, p99 = percentiles(finite, (1, 50, 99))
    return len(finite), med, p1, p99


def bounds_size(points: np.ndarray, mask: np.ndarray) -> Optional[np.ndarray]:
    """The size (x, y, z) in metres of the box around the masked points, None for no point."""
    chosen = points[mask]
    return None if not len(chosen) else chosen.max(axis=0) - chosen.min(axis=0)


def dominant_labels(weights: Optional[np.ndarray], bones: Sequence[str], by: str) -> np.ndarray:
    """A label per point: its dominant bone's region (``by == "region"``) or the bone's name (``"bone"``); ``""`` for a
    point with no weight (and for a bone that is in no region)."""
    if weights is None:
        return np.array([], dtype=object)
    _, _, _, dominant = check.weight_problems(weights, np.zeros((0, 2), dtype=np.int64))
    names = [region_rules.region_of(b) if by == "region" else b for b in bones]
    return np.array([names[d] if d >= 0 else "" for d in dominant], dtype=object)


def _size_text(size: Optional[np.ndarray]) -> str:
    return NR if size is None else " x ".join(cm(v) for v in size)


def deform_page(a: np.ndarray, b: np.ndarray, tris: np.ndarray, weights: Optional[np.ndarray], bones: Sequence[str],
                source_a: Source, source_b: Source, by: str = "region", body: Optional[np.ndarray] = None,
                body_name: str = "body", group: Optional[np.ndarray] = None, group_name: str = "") -> Page:
    """The page. ``a`` and ``b`` are (n, 3) points in metres, ``tris`` the shared triangles, ``weights`` the bone weights of
    A (points x bones) or None, ``body`` and ``group`` boolean masks over the points (None: not on the mesh)."""
    if by not in ("region", "bone"):
        raise WrongQuestion("--by is region or bone, not %r" % by)
    edges = check.edges_of(tris)
    ratios = edge_ratios(a, b, edges)
    total = ratio_stats(ratios)
    missing: List[str] = []
    labels = dominant_labels(weights, bones, by)
    summary = "deform-compare: B/A edge length median %s, p1 %s, p99 %s, %s edges" % (
        num(total[1], 3), num(total[2], 3), num(total[3], 3), count(total[0]))
    context = ["scene: %s; ratio = edge length in B / in A" % source_a.scene_name(), source_a.line("A: "), source_b.line("B: ")]
    widths = (17, 7, 7, 7, 7, 7)
    body_lines: List[str] = [row(("%s" % by, "points", "edges", "median", "p1", "p99"), widths, 1)]

    def add(label: str, points: int, mask: np.ndarray) -> None:
        n, med, p1, p99 = ratio_stats(ratios[mask])
        body_lines.append(row((label, count(points), count(n), num(med, 3), num(p1, 3), num(p99, 3)), widths, 1))

    if weights is None:
        missing.append("%s rows (mesh A has no bone weights)" % by)
    else:
        ends = labels[edges]  # (e, 2) labels of the two points of each edge
        same = ends[:, 0] == ends[:, 1]
        names = list(region_rules.REGIONS) if by == "region" else sorted({x for x in labels if x})
        for name in names:
            members = labels == name
            if by == "region" or members.any():
                add(name, int(members.sum()), same & (ends[:, 0] == name))
        weighted = (ends[:, 0] != "") & (ends[:, 1] != "")
        add(region_rules.BETWEEN, int((labels != "").sum()), weighted & ~same)
        unweighted = labels == ""
        if unweighted.any():
            add(region_rules.NO_WEIGHT, int(unweighted.sum()), ~weighted)
    if body is None:
        missing.append("body rows (no group %s on the mesh)" % body_name)
    else:
        inside = body[edges[:, 0]] & body[edges[:, 1]]
        outside = ~body[edges[:, 0]] & ~body[edges[:, 1]]
        add("group " + body_name, int(body.sum()), inside)
        add("rest (not " + body_name + ")", int((~body).sum()), outside)
    if group_name:
        if group is None or not group.any():
            missing.append("group %s (no such group, or no point in it)" % group_name)
        else:
            inside = group[edges[:, 0]] & group[edges[:, 1]]
            add("group " + group_name, int(group.sum()), inside)
            before, after = bounds_size(a, group), bounds_size(b, group)
            body_lines.append("%s size cm: A %s; B %s" % (group_name, _size_text(before), _size_text(after)))
    return Page(summary, context, body_lines, missing, "weights; skeleton-compare A B; deform-compare A B --by bone")

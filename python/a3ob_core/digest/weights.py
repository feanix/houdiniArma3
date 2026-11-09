# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the weight rules are those of P3D Export (docs/design/digests.md).
"""The weights digest, with no Houdini (task #81): the bone weights of a mesh, counted the way P3D Export counts them.

The weights are a matrix (points x bones) that the Houdini side reads with ``rig_weights.read_selections`` (or
``read_capture``): a weight counts only where the point is a member of the bone's selection, and a member with weight 0 or
less is no weight (``type_rules`` ``bone_weights``). An influence is a weight above 0, the rule the export's own tier-2 check
(T2-WGT4) uses. The per-point sum and the heaviest bone come from ``check.weight_problems``.
"""

from typing import List, Optional, Sequence

import numpy as np

from a3ob_core.rig import check

from .page import NR, Page, count, num, pairs, percentiles
from .skeleton import Source

__all__ = ["weight_figures", "weights_page", "TOP_BONES"]

#: Bones listed by name on a default page (the others are summed); ``show_all`` lists every bone.
TOP_BONES = 18


def weight_figures(weights: np.ndarray, ignore: Optional[np.ndarray] = None) -> dict:
    """The counts of the page as a dict, so a test can compare them with the export's own: ``points``, ``unweighted``,
    ``influences`` (per point), ``sums`` (per point), ``per_bone`` (points with a weight, per bone)."""
    keep = np.ones(len(weights), dtype=bool) if ignore is None else ~np.asarray(ignore, dtype=bool)
    w = weights[keep]
    influences = (w > 0).sum(axis=1).astype(np.int64)
    sums = w.sum(axis=1)
    return {"points": int(keep.sum()), "unweighted": int((sums <= 0).sum()), "influences": influences, "sums": sums,
            "per_bone": (w > 0).sum(axis=0).astype(np.int64), "ignored": int((~keep).sum()), "kept": w}


def weights_page(weights: np.ndarray, bones: Sequence[str], source: Source, how: str, zero_members: int = 0,
                 ignore: Optional[np.ndarray] = None, max_influences: int = 4, sum_limit: float = 0.01,
                 show_all: bool = False) -> Page:
    """The page for ``weights`` (points x bones) of ``bones``. ``how`` says what was read ("bone selections", "capture
    weights"). ``ignore`` marks points that are not counted (proxy points)."""
    f = weight_figures(weights, ignore)
    w = f["kept"]
    infl, sums = f["influences"], f["sums"]
    over = int((infl > max_influences).sum())
    most = int(infl.max()) if len(infl) else None
    weighted = sums > 0
    _, _, sumerr, _ = check.weight_problems(w, np.zeros((0, 2), dtype=np.int64))
    off = sumerr[weighted]
    summary = "weights: %d points; %d unweighted; %d over %d bones; most influences %s" % (
        f["points"], f["unweighted"], over, max_influences, count(most))
    context = ["scene: " + source.scene_name(), source.line("node: "),
               "read: %s, as P3D Export reads them (weight only in-selection)" % how]
    body: List[str] = []
    steps = ["%d:%d" % (n, int((infl == n).sum())) for n in range(max_influences + 1)]
    steps.append("%d+:%d" % (max_influences + 1, int((infl > max_influences).sum())))
    body.append("points by influences (Arma keeps %d): %s" % (max_influences, " ".join(steps)))
    if len(off):
        p50, p99 = percentiles(off, (50, 99))
        worst = int(np.nonzero(weighted)[0][int(np.argmax(off))])
        body.append("sum off 1: median %s, p99 %s, max %s (point %d); over %s: %d" % (
            num(p50, 4), num(p99, 4), num(float(off.max()), 4), worst, num(sum_limit, 2), int((off > sum_limit).sum())))
    else:
        body.append("weight sum off 1: %s (no point has a weight)" % NR)
    body.append("members with weight 0 (written as no weight): %d" % zero_members)
    per_bone = f["per_bone"]
    order = [k for k in np.argsort(-per_bone, kind="stable") if per_bone[k] > 0]
    shown = order if show_all else order[:TOP_BONES]
    body.append("points per bone (%d of %d bones have points):" % (len(order), len(bones)))
    entries = ["%s %d" % (bones[k], per_bone[k]) for k in shown]
    body.extend(pairs(entries, 1 if show_all else 3))
    if len(order) > len(shown):
        rest = order[len(shown):]
        body.append("and %d more bones: %d points in all" % (len(rest), int(per_bone[rest].sum())))
    empty = [bones[k] for k in range(len(bones)) if per_bone[k] == 0]
    body.append("bones with no points: %d" % len(empty) + ("; " + ", ".join(empty) if show_all and empty else ""))
    missing: List[str] = []
    if f["ignored"]:
        body.append("not counted: %d proxy points" % f["ignored"])
    return Page(summary, context, body, missing, "deform-compare A B; skeleton; landmarks")

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the feet are measured by ``a3ob_core.rig.foot`` (docs/design/digests.md, task #82).
"""The feet and wrap-distance digests, with no Houdini (task #82).

``feet``: the sole pitch and the heel and toe-tip of each foot of one or more bodies, one row per body and foot. The reference
is a flat sole (0 degrees) and the Arma sample's own rows: a character that is unposed onto the sample should end up with
soles as flat as the sample's, its heel and toe where the sample's are.

``wrap-distance``: how far the wrapped sample body (Rig Fit's ``fitted_body``) lies from the user's body, per body region,
and how far its heel and toe tips are. The reference is zero: a wrap that fits exactly. Distances are from each wrapped point
to the closest point of the user's surface.

Both use the rule of ``rig.foot`` (the same heel, toe and sole line that Refine's landmarks and sole step use), so the page
measures with the very rule that the fix uses.
"""

from typing import List, Optional, Sequence, Tuple

import numpy as np

from a3ob_core.rig.foot import find_feet
from a3ob_core.rig.transfer import closest_points

from . import regions as region_rules
from .page import NR, Page, cm, clip, count, deg, num, percentiles, row
from .skeleton import Source

__all__ = ["feet_page", "wrap_distance_page", "foot_rows", "region_distances"]


def foot_rows(label: str, points: Optional[np.ndarray], front: float = 1.0) -> List[Tuple[str, ...]]:
    """The rows ``(label, foot, pitch, heel z, toe z, length, sole y)`` of the feet of ``points`` (cm and degrees as text).
    Heel z and toe z are the back and the front end of the foot (its farthest points). A body whose feet are not found
    gives one row of ``n/r``."""
    feet = find_feet(points, front) if points is not None else {}
    if not feet:
        return [(label, "-", NR, NR, NR, NR, NR)]
    out = []
    for side, tag in ((1, "L"), (-1, "R")):
        if side in feet:
            f = feet[side]
            out.append((label, tag, deg(f.pitch), cm(f.back_end[2]), cm(f.front_end[2]),
                        cm(float(np.linalg.norm((f.front_end - f.back_end)[[0, 2]]))), cm(f.low)))
    return out


def feet_page(entries: Sequence[Tuple[str, Optional[np.ndarray]]], sources: Sequence[Source], front: float = 1.0) -> Page:
    """The page. ``entries`` are ``(label, points)`` (points None: not recorded), ``sources`` what each was read from."""
    widths = (21, 2, 6, 7, 7, 7, 7)
    body = [row(("body", "ft", "pitch", "heel z", "toe z", "length", "sole y"), widths, 1)]
    missing: List[str] = []
    pitches = []
    for label, points in entries:
        rows = foot_rows(clip(label, widths[0]), points, front)
        body.extend(row(r, widths, 1) for r in rows)
        if rows[0][1] == "-":
            missing.append("feet of %s (no body points, or no foot found)" % label)
        pitches += [float(r[2]) for r in rows if r[2] != NR]
    body.append("pitch in degrees, toes up positive; cm: heel z and toe z are the ends of the foot, sole y its lowest point")
    body.append("reference: a flat sole is 0.0; the Arma sample's rows are what an unposed character is measured against")
    spread = "%s to %s deg" % (num(min(pitches)), num(max(pitches))) if pitches else NR
    summary = "feet: %d bod%s, sole pitch %s" % (len(entries), "y" if len(entries) == 1 else "ies", spread)
    context = ["scene: " + sources[0].scene_name()] if sources else []
    return Page(summary, context + [s.line("") for s in sources], body, missing, "wrap-distance; deform-compare A B --by bone; landmarks")


def region_distances(wrapped: np.ndarray, labels: np.ndarray, owner: np.ndarray, owner_tris: np.ndarray):
    """``(distances, {region: distances})``: the distance of every wrapped point to the closest point of the owner's surface,
    and the distances grouped by ``labels`` (a region name per wrapped point, ``""`` for none)."""
    _, _, dist = closest_points(wrapped, owner, owner_tris)
    return dist, {name: dist[labels == name] for name in region_rules.REGIONS if (labels == name).any()}


def wrap_distance_page(wrapped: np.ndarray, wrapped_tris: np.ndarray, labels: Optional[np.ndarray], owner: np.ndarray,
                       owner_tris: np.ndarray, source_wrapped: Source, source_owner: Source, front: float = 1.0) -> Page:
    """The page. ``wrapped``/``wrapped_tris`` is the wrapped body, ``labels`` its region per point (None: no weights),
    ``owner``/``owner_tris`` the user's body group."""
    dist, by_region = region_distances(wrapped, np.asarray(labels if labels is not None else [""] * len(wrapped), dtype=object),
                                       owner, owner_tris)
    p50, p95 = percentiles(dist, (50, 95))
    summary = "wrap-distance: wrapped body to your body, median %s cm, p95 %s cm, max %s cm, %s points" % (
        cm(p50), cm(p95), cm(float(dist.max()) if len(dist) else None), count(len(dist)))
    context = ["scene: " + source_wrapped.scene_name(), source_wrapped.line("wrapped: "), source_owner.line("your body: ")]
    widths = (17, 7, 7, 7, 7)
    body = [row(("region", "points", "median", "p95", "max"), widths, 1)]
    missing: List[str] = []
    if labels is None:
        missing.append("region rows (the wrapped body has no bone weights)")
    for name, values in by_region.items():
        a, b = percentiles(values, (50, 95))
        body.append(row((name, count(len(values)), cm(a), cm(b), cm(float(values.max()))), widths, 1))
    # the ends of the feet, by the rule of rig.foot: how far the wrapped heel and toe end lie from yours along the foot
    mine = find_feet(wrapped[np.unique(wrapped_tris)], front)
    theirs = find_feet(owner[np.unique(owner_tris)], front)
    ends = []
    for side, tag in ((1, "L"), (-1, "R")):
        for what, pick in (("heel", "back_end"), ("toe", "front_end")):
            gap = None
            if side in mine and side in theirs:
                a, b = getattr(mine[side], pick), getattr(theirs[side], pick)
                gap = float((a - b) @ theirs[side].axis)  # along the foot, plus is farther forward than yours
            ends.append("%s %s %s" % (what, tag, num(None if gap is None else 100.0 * gap)))
    body.append("foot ends, wrapped minus yours along the foot, cm: " + "; ".join(ends))
    body.append("reference: 0.0 is a wrap that fits your body exactly")
    if not mine or not theirs:
        missing.append("foot ends (no foot found on the wrapped body or on your body)")
    return Page(summary, context, body, missing, "feet; landmarks; deform-compare A B")

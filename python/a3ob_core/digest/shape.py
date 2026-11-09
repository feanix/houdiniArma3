# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the rules are new (docs/design/digests.md, task #84). The first version was the advisor's one-off
# measurement script of 2026-11-07 (shape83.py).
"""The finger, hand and foot shape digests, with no Houdini (task #84).

The #82 and #83 digests could not see a finger that was squeezed flat or a foot that was rolled onto its edge: the finger bend
comes from three centroids, the sole pitch is front to back only, and the foot's forward axis is taken from the foot itself,
so a turn or a sideways tilt cancels out. These pages measure the shape itself.

``finger-shape``: the cross-section of every finger segment, from the points whose heaviest bone it is. The segment's axis is the
direction between the centroids of its neighbours (segment 1: to segment 2; segment 2: segment 1 to segment 3; segment 3:
segment 2 to segment 3). Across that axis the points are cut along their two principal directions; the width is the larger one
and the thickness the smaller, each the 5th to 95th percentile extent. The figure to watch is thickness / width.

``hand-shape``: the extents of the points whose heaviest bone is the hand along their three principal axes, and the knuckle
arch (how far the middle two finger bases lie off the line from the first to the last).

``foot-shape``: the sole roll (sideways tilt of the sole, outer edge lower is negative), toe-out (how far the foot's axis is
turned outward from straight ahead), and the length, width and height of the foot bones' points.

``shape-compare``: the edge-length ratio of mesh B against mesh A for the finger edges (along the finger, across its width,
across its thickness) and the foot edges (along, across, up), each measured in A's frame, and what is left of the foot points
after the best rigid fit.

The angle between the shin and the foot (the "ankle" column of the one-off script) is not here: it came from a principal axis of
the Leg points below 35 cm, which turns over with the sign of the axis, and gave 161 degrees for one foot and 24 for the other
on the same pose. It measured the method, not the foot.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from a3ob_core.rig.cage import sole_roll
from a3ob_core.rig.foot import find_feet

from .page import MAX_COLUMNS, NR, Page, cm, clip, count, deg, num, row, short_label
from .pose import FINGER_NAMES, MIN_POINTS, bone_turn
from .skeleton import Source

__all__ = ["Section", "segment_section", "section_table", "hand_extents", "knuckle_arch", "FootShape", "foot_shape",
           "edge_ratio_split", "finger_page", "hand_page", "foot_page", "compare_page"]

#: The four fingers whose segments are measured (the thumb has two segments and no neighbours to frame them).
FINGERS = FINGER_NAMES[:4]


def _extent(values: np.ndarray) -> float:
    return float(np.percentile(values, 95) - np.percentile(values, 5))


@dataclasses.dataclass
class Section:
    """One finger segment: its ``axis``, the ``width`` and ``thickness`` directions across it (unit vectors), the width and
    thickness (metres: 5th to 95th percentile extents) and the number of points."""
    axis: np.ndarray
    across: np.ndarray
    thin: np.ndarray
    width: float
    thickness: float
    points: int

    @property
    def ratio(self) -> float:
        return self.thickness / self.width if self.width > 0 else float("nan")


def segment_section(points: np.ndarray, dominant: np.ndarray, side: str, finger: str, k: int) -> Optional[Section]:
    """The cross-section of segment ``k`` (1 to 3) of one finger, or None where a segment of the finger has fewer than
    ``MIN_POINTS`` points."""
    names = ["%shand%s%d" % (side, finger, j) for j in (1, 2, 3)]
    if any((dominant == n).sum() < MIN_POINTS for n in names):
        return None
    c = [points[dominant == n].mean(axis=0) for n in names]
    axis = {1: c[1] - c[0], 2: c[2] - c[0], 3: c[2] - c[1]}[k]
    axis = axis / max(np.linalg.norm(axis), 1e-12)
    q = points[dominant == names[k - 1]]
    q = q - q.mean(axis=0)
    flat = q - np.outer(q @ axis, axis)
    _, vectors = np.linalg.eigh(flat.T @ flat)
    across, thin = vectors[:, 2], vectors[:, 1]
    return Section(axis, across, thin, _extent(q @ across), _extent(q @ thin), len(q))


def section_table(points: np.ndarray, dominant: np.ndarray, side: str) -> Dict[Tuple[str, int], Optional[Section]]:
    return {(f, k): segment_section(points, dominant, side, f, k) for f in FINGERS for k in (1, 2, 3)}


def hand_extents(points: np.ndarray, dominant: np.ndarray, side: str) -> Optional[Tuple[Tuple[float, float, float], int]]:
    """``((long, middle, short) extent in metres, point count)`` of the points whose heaviest bone is the hand, along their
    three principal axes, or None."""
    q = points[dominant == side + "hand"]
    if len(q) < MIN_POINTS:
        return None
    q = q - q.mean(axis=0)
    _, v = np.linalg.eigh(q.T @ q)
    return (_extent(q @ v[:, 2]), _extent(q @ v[:, 1]), _extent(q @ v[:, 0])), len(q)


def knuckle_arch(points: np.ndarray, dominant: np.ndarray, side: str) -> Optional[float]:
    """How far (metres) the middle two finger bases lie off the straight line from the first finger base to the last: the
    centres of the points of the four first segments."""
    c = []
    for f in FINGERS:
        mine = points[dominant == "%shand%s1" % (side, f)]
        if len(mine) < MIN_POINTS:
            return None
        c.append(mine.mean(axis=0))
    line = c[3] - c[0]
    line = line / max(np.linalg.norm(line), 1e-12)
    mid = 0.5 * (c[1] + c[2]) - c[0]
    return float(np.linalg.norm(mid - (mid @ line) * line))


# ---- feet ---------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class FootShape:
    roll: float  # degrees, outer edge higher positive
    toe_out: float  # degrees, toes turned outward positive
    length: float  # metres
    width: float
    height: float
    points: int


def _foot_bones(side: str) -> List[str]:
    return [side + "foot", side + "toebase"]


def foot_shape(points: np.ndarray, dominant: np.ndarray, front: float = 1.0) -> Dict[int, FootShape]:
    """``{+1: left, -1: right}`` shapes of the feet of a standing body (``points`` are its body points, ``dominant`` the name
    of the heaviest bone of each). The foot's forward axis and floor are those of ``rig.foot.find_feet``; the roll is the slope
    of the lowest point against the sideways direction (``rig.cage.sole_roll``, the rule the Unpose fix uses); the toe-out is
    the angle of the axis from straight ahead; the size is of the points of the foot and toe-base bones."""
    feet = find_feet(points, front)
    out: Dict[int, FootShape] = {}
    for side, name in ((1, "left"), (-1, "right")):
        if side not in feet:
            continue
        foot = feet[side]
        axis = foot.axis / np.linalg.norm(foot.axis)
        across = np.cross([0.0, 1.0, 0.0], axis)
        across /= np.linalg.norm(across)
        if across[0] * side < 0:
            across = -across
        q = points[np.isin(dominant, _foot_bones(name))]
        if len(q) < 12:
            continue
        roll = sole_roll(q, axis, side)
        toe_out = float(np.degrees(np.arctan2(axis[0] * side, axis[2])))
        centred = q - q.mean(axis=0)
        out[side] = FootShape(roll, toe_out, _extent(centred @ axis), _extent(centred @ across), float(q[:, 1].max() - foot.low),
                              len(q))
    return out


# ---- edges ----------------------------------------------------------------------------------------------------------------

def _unit_edges(a: np.ndarray, b: np.ndarray, edges: np.ndarray):
    """``(edges kept, ratio of the lengths in B over A, unit vectors in A)`` for the edges longer than 1e-6 in A."""
    va = a[edges[:, 1]] - a[edges[:, 0]]
    la = np.linalg.norm(va, axis=1)
    lb = np.linalg.norm(b[edges[:, 1]] - b[edges[:, 0]], axis=1)
    keep = la > 1e-6
    return edges[keep], lb[keep] / la[keep], va[keep] / la[keep, None]


def edge_ratio_split(a: np.ndarray, b: np.ndarray, edges: np.ndarray, dominant: np.ndarray, side: str,
                     body: Optional[np.ndarray] = None) -> Dict[str, Dict[str, np.ndarray]]:
    """The length ratios (B over A) of the edges inside one bone, split by the direction of the edge in A:
    ``{"finger": {"along", "width", "thickness"}, "foot": {"along", "across", "up"}}`` (arrays; empty where there is none).
    ``a``/``b`` are the points of the two meshes (same point order), ``edges`` an (m, 2) array of point pairs, ``dominant``
    the bone name of each point in A. A finger edge is split in the frame of its segment (``segment_section``), a foot
    edge in the frame of the foot (forward axis, sideways, up). An edge is put under the direction it is most parallel to."""
    kept, ratio, unit = _unit_edges(a, b, edges)
    out = {"finger": {"along": [], "width": [], "thickness": []}, "foot": {"along": [], "across": [], "up": []}}
    for f in FINGERS:
        for k in (1, 2, 3):
            sec = segment_section(a, dominant, side, f, k)
            if sec is None:
                continue
            bone = "%shand%s%d" % (side, f, k)
            m = (dominant[kept[:, 0]] == bone) & (dominant[kept[:, 1]] == bone)
            c = np.abs(np.stack([unit[m] @ sec.axis, unit[m] @ sec.across, unit[m] @ sec.thin], axis=1))
            label = c.argmax(axis=1)
            for i, key in enumerate(("along", "width", "thickness")):
                out["finger"][key] += list(ratio[m][label == i])
    feet = find_feet(a if body is None else a[body])
    sign = 1 if side == "left" else -1
    if sign in feet:
        axis = feet[sign].axis / np.linalg.norm(feet[sign].axis)
        across = np.cross([0.0, 1.0, 0.0], axis)
        across /= np.linalg.norm(across)
        bones = _foot_bones(side)
        m = np.isin(dominant[kept[:, 0]], bones) & np.isin(dominant[kept[:, 1]], bones)
        c = np.abs(np.stack([unit[m] @ axis, unit[m] @ across, unit[m][:, 1]], axis=1))
        label = c.argmax(axis=1)
        for i, key in enumerate(("along", "across", "up")):
            out["foot"][key] += list(ratio[m][label == i])
    return {g: {k: np.asarray(v, dtype=np.float64) for k, v in d.items()} for g, d in out.items()}


def foot_leftover(a: np.ndarray, b: np.ndarray, dominant: np.ndarray, side: str) -> Optional[float]:
    """The rms distance (metres) left between the foot points of A and B after the best rigid fit, or None."""
    mine = np.isin(dominant, _foot_bones(side))
    if mine.sum() < MIN_POINTS:
        return None
    return bone_turn(a[mine], b[mine])[1]


# ---- the pages ------------------------------------------------------------------------------------------------------------

Entry = Tuple[str, Optional[np.ndarray], Optional[np.ndarray]]


def _usable(entries: Sequence[Entry], missing: List[str], what: str) -> List[Entry]:
    good = []
    for label, points, dominant in entries:
        if points is None or dominant is None or not len(points):
            missing.append("%s of %s (no points, or no bone weights)" % (what, label))
        else:
            good.append((label, points, dominant))
    return good


def finger_page(entries: Sequence[Entry], sources: Sequence[Source], show_all: bool = False) -> Page:
    """The finger-shape page. ``entries`` are ``(label, points, dominant bone names)``."""
    missing: List[str] = []
    good = _usable(entries, missing, "finger shape")
    widths = (17, 2, 8, 8, 8, 8)
    body = [row(("t/w x100, segs 1 2 3", "hd", "index", "middle", "ring", "pinky"), widths, 1)]
    cms = [row(("seg 2, cm wide x thick", "hd", "index", "middle", "ring", "pinky"), widths, 1)]
    middle: Dict[str, List[float]] = {}
    for label, points, dominant in good:
        for side, tag in (("left", "L"), ("right", "R")):
            table = section_table(points, dominant, side)
            cells, wide = [], []
            for f in FINGERS:
                segs = [table[(f, k)] for k in (1, 2, 3)]
                cells.append(" ".join("--" if s is None else "%2d" % round(100 * s.ratio) for s in segs))
                wide.append("-" if segs[1] is None else "%.1fx%.1f" % (100 * segs[1].width, 100 * segs[1].thickness))
                if f == "middle" and segs[1] is not None:
                    middle.setdefault(label, []).append(segs[1].ratio)
            body.append(row([clip(label, 17), tag] + cells, widths, 1))
            cms.append(row([clip(label, 17), tag] + wide, widths, 1))
            if show_all:
                for (f, k), s in table.items():
                    if s is not None:
                        body.append("    %s %s%d: %s x %s cm, n%d" % (tag, f, k, cm(s.width), cm(s.thickness), s.points))
    body.extend(cms)
    body.append("t/w = thickness / width (100 = round); the Arma sample rows are the reference")
    spread = ", ".join("%s %s" % (short_label(k), num(float(np.mean(v)), 2)) for k, v in middle.items())
    summary = "finger-shape: middle seg 2 t/w: %s" % (spread or NR)
    if len(summary) > MAX_COLUMNS:
        summary = summary[:MAX_COLUMNS - 3].rsplit(",", 1)[0] + ", ..."
    context = ["scene: " + sources[0].scene_name()] if sources else []
    return Page(summary, context + [s.line("") for s in sources], body, missing, "shape-compare A B; fingers; hand-shape; feet")


def hand_page(entries: Sequence[Entry], sources: Sequence[Source]) -> Page:
    """The hand-shape page: extents along the principal axes, and the knuckle arch."""
    missing: List[str] = []
    good = _usable(entries, missing, "hand shape")
    widths = (21, 2, 5, 17, 6)
    body = [row(("body", "hd", "pts", "extents cm (3 axes)", "arch"), widths, 1)]
    first = None
    for label, points, dominant in good:
        for side, tag in (("left", "L"), ("right", "R")):
            found = hand_extents(points, dominant, side)
            arch = knuckle_arch(points, dominant, side)
            if found is None:
                body.append(row((clip(label, 21), tag, NR, NR, NR), widths, 1))
                missing.append("hand of %s %s (fewer than %d points)" % (label, tag, MIN_POINTS))
                continue
            (a, b, c), n = found
            first = first or (label, tag, a)
            body.append(row((clip(label, 21), tag, count(n), "%s x %s x %s" % (cm(a), cm(b), cm(c)), cm(arch)), widths, 1))
    body.append("extents: 5th to 95th percentile along the hand points' 3 principal axes")
    body.append("arch = how far the middle finger bases lie off the first-to-last line")
    body.append("reference: the Arma sample rows (the size change to them is by design)")
    summary = "hand-shape: %s" % ("%s %s long %s cm" % (first[0], first[1], cm(first[2])) if first else NR)
    context = ["scene: " + sources[0].scene_name()] if sources else []
    return Page(summary, context + [s.line("") for s in sources], body, missing, "finger-shape; fingers; shape-compare A B")


def foot_page(entries: Sequence[Entry], sources: Sequence[Source], front: float = 1.0) -> Page:
    """The foot-shape page: roll, toe-out and size of the feet. ``entries`` points are the body points only."""
    missing: List[str] = []
    widths = (21, 2, 6, 7, 6, 6, 6)
    body = [row(("body", "ft", "roll", "toe-out", "length", "width", "height"), widths, 1)]
    rolls = []
    for label, points, dominant in entries:
        found = foot_shape(points, dominant, front) if points is not None and dominant is not None and len(points) else {}
        if not found:
            body.append(row((clip(label, 21), "-") + (NR,) * 5, widths, 1))
            missing.append("feet of %s (no points, no bone weights, or no foot found)" % label)
            continue
        for side, tag in ((1, "L"), (-1, "R")):
            if side in found:
                f = found[side]
                rolls.append(f.roll)
                body.append(row((clip(label, 21), tag, deg(f.roll), deg(f.toe_out), cm(f.length), cm(f.width), cm(f.height)),
                                widths, 1))
    body.append("degrees: roll = sideways tilt of the sole, outer edge lower negative;")
    body.append("toe-out + = toes turned outward. cm: size of the foot and toe-base points")
    body.append("reference: the Arma sample rows; a flat sole rolls 0")
    finite = [r for r in rolls if np.isfinite(r)]
    summary = "foot-shape: %d bod%s, sole roll %s" % (len(entries), "y" if len(entries) == 1 else "ies",
                                                      "%s to %s deg" % (num(min(finite)), num(max(finite))) if finite else NR)
    context = ["scene: " + sources[0].scene_name()] if sources else []
    return Page(summary, context + [s.line("") for s in sources], body, missing, "feet; shape-compare A B; deform-compare A B")


def compare_page(a: np.ndarray, b: np.ndarray, edges: np.ndarray, dominant: np.ndarray, body_mask: Optional[np.ndarray],
                 source_a: Source, source_b: Source) -> Page:
    """The shape-compare page. ``dominant`` is the bone name per point in A, ``body_mask`` the body points (for the feet)."""
    widths = (6, 2, 17, 17, 17)
    lines = [row(("", "hd", "along", "across width", "across thickness"), widths, 1)]
    missing: List[str] = []
    cell = lambda v: "%.2f (%.2f) n%d" % (np.median(v), np.percentile(v, 5), len(v)) if len(v) else NR
    foot_lines = [row(("", "ft", "along", "across", "up"), widths, 1)]
    leftover = []
    along_medians = []
    for side, tag in (("left", "L"), ("right", "R")):
        split = edge_ratio_split(a, b, edges, dominant, side, body_mask)
        f, ft = split["finger"], split["foot"]
        if not len(f["along"]):
            missing.append("finger edges of the %s hand (fewer than %d points on a segment)" % (side, MIN_POINTS))
        lines.append(row(("finger", tag, cell(f["along"]), cell(f["width"]), cell(f["thickness"])), widths, 1))
        if not len(ft["along"]):
            missing.append("foot edges of the %s foot (no foot found)" % side)
        foot_lines.append(row(("foot", tag, cell(ft["along"]), cell(ft["across"]), cell(ft["up"])), widths, 1))
        rms = foot_leftover(a, b, dominant, side)
        leftover.append("%s %s" % (tag, cm(rms, 2)))
        if len(f["along"]):
            along_medians.append(float(np.median(f["along"])))
    body = lines + foot_lines
    body.append("median edge length ratio B over A (5th percentile, edge count); 1.00 = same")
    body.append("foot points left after the best rigid fit, rms cm: " + ", ".join(leftover))
    body.append("reference: 1.00; a move to the sample's size is one ratio in all directions,")
    body.append("a squeeze shows as one direction away from the others")
    summary = "shape-compare: finger edges along %s" % (num(float(np.mean(along_medians)), 2) if along_medians else NR)
    context = ["scene: " + source_a.scene_name(), source_a.line("A: "), source_b.line("B: ")]
    return Page(summary, context, body, missing, "finger-shape; foot-shape; deform-compare A B --by region")

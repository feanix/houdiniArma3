# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the rule is new (docs/design/digests.md, task #83).
"""The fingers and head-turn digests, with no Houdini (task #83).

``fingers``: how much each finger is bent, for one or more bodies. The bend at a knuckle is the angle between the two
directions that meet there; the directions run between the centroids of the points that belong to each finger segment (the
points whose heaviest bone is that segment's bone). The middle knuckle is the angle between (segment 1 to segment 2) and
(segment 2 to segment 3); the base knuckle is the angle between (palm to segment 1) and (segment 1 to segment 2). Zero is a
straight finger. The reference is the Arma sample's own row: an unposed character should end up with fingers as straight as the
sample's.

``head-turn``: how much a bone's points turned between a mesh and its moved copy (for example the posed mesh and the Unpose
result): the best rigid rotation (Kabsch) from the points of A to the points of B, as pitch (nose down positive), yaw (turn
to the left, +X, positive) and roll (left ear up positive). The reference is zero: the bone kept its pose. The lean of the
neck and head segments from vertical of a fitted skeleton and of the Arma rest is added when both are given.
"""

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .page import NR, Page, WrongQuestion, cm, count, deg, num, row
from .skeleton import Source

__all__ = ["FINGER_NAMES", "angle_between", "finger_bends", "fingers_page", "bone_turn", "turn_angles", "lean", "head_page",
           "dominant_bone_names", "MIN_POINTS"]

FINGER_NAMES = ("index", "middle", "ring", "pinky", "thumb")
#: A finger segment with fewer points than this has no centroid worth measuring.
MIN_POINTS = 3


def angle_between(a: np.ndarray, b: np.ndarray) -> float:
    """The angle in degrees between two vectors."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return float("nan")
    return float(np.degrees(np.arccos(np.clip(float(a @ b) / (na * nb), -1.0, 1.0))))


def dominant_bone_names(weights: np.ndarray, bones: Sequence[str]) -> np.ndarray:
    """The lower-case name of the heaviest bone of every point, ``""`` for a point with no weight."""
    names = np.array([b.lower() for b in bones] + [""], dtype=object)
    total = weights.sum(axis=1)
    return names[np.where(total > 0, weights.argmax(axis=1), len(bones))]


def finger_bends(points: np.ndarray, dominant: np.ndarray, side: str, finger: str) -> Tuple[Optional[float], Optional[float]]:
    """``(base bend, middle bend)`` in degrees of one finger (``side`` ``"left"``/``"right"``, ``finger`` one of
    ``FINGER_NAMES``), from the points and the lower-case dominant bone name of each. ``None`` where a segment has fewer than
    ``MIN_POINTS`` points."""
    def centre(bone: str) -> Optional[np.ndarray]:
        mine = points[dominant == bone]
        return mine.mean(axis=0) if len(mine) >= MIN_POINTS else None

    palm = centre("%shand" % side)
    seg = [centre("%shand%s%d" % (side, finger, k)) for k in (1, 2, 3)]
    base = angle_between(seg[0] - palm, seg[1] - seg[0]) if palm is not None and seg[0] is not None and seg[1] is not None else None
    mid = angle_between(seg[1] - seg[0], seg[2] - seg[1]) if all(s is not None for s in seg) else None
    return base, mid


def _cell(bends: Tuple[Optional[float], Optional[float]]) -> str:
    base, mid = bends
    one = lambda v: "-" if v is None or not np.isfinite(v) else "%d" % round(v)
    return "%s/%s" % (one(base), one(mid))


def fingers_page(entries: Sequence[Tuple[str, Optional[np.ndarray], Optional[np.ndarray]]], sources: Sequence[Source]) -> Page:
    """The page. ``entries`` are ``(label, points, dominant bone names)``; points or names None: not recorded."""
    widths = (21, 2, 6, 6, 6, 6, 6)
    body = [row(("body", "hd", "index", "middle", "ring", "pinky", "thumb"), widths, 1)]
    missing: List[str] = []
    middles: Dict[str, List[float]] = {}
    for label, points, dominant in entries:
        if points is None or dominant is None or not len(points):
            missing.append("fingers of %s (no points, or no bone weights)" % label)
            body.append(row((label, "-") + (NR,) * 5, widths, 1))
            continue
        for side, tag in (("left", "L"), ("right", "R")):
            found = [finger_bends(points, dominant, side, f) for f in FINGER_NAMES]
            body.append(row([label[:widths[0]], tag] + [_cell(b) for b in found], widths, 1))
            middles.setdefault(label, []).extend(b[1] for b in found[:4] if b[1] is not None and np.isfinite(b[1]))
    body.append("bend in degrees, base/middle knuckle; 0 = straight (thumb: its first two)")
    body.append("reference: the Arma sample rows; a straight finger = 0")
    spread = ", ".join("%s %s" % (k[:9], num(float(np.mean(v)), 0)) for k, v in middles.items() if v)
    summary = "fingers: mean middle bend of 4, deg: %s" % (spread or NR)
    context = ["scene: " + sources[0].scene_name()] if sources else []
    return Page(summary, context + [s.line("") for s in sources], body, missing, "deform-compare A B --by region; feet; head-turn")


# ---- the turn of a bone ---------------------------------------------------------------------------------------------------

def turn_angles(rotation: np.ndarray) -> Tuple[float, float, float]:
    """``(pitch, yaw, roll)`` in degrees of a rotation (column vectors) written ``Ry(yaw) Rx(pitch) Rz(roll)`` with Y up, +Z
    forward and +X left: pitch positive tilts the nose down, yaw positive turns the nose to the left, roll positive lifts the
    left ear."""
    pitch = np.arcsin(np.clip(-rotation[1, 2], -1.0, 1.0))
    yaw = np.arctan2(rotation[0, 2], rotation[2, 2])
    roll = np.arctan2(rotation[1, 0], rotation[1, 1])
    return float(np.degrees(pitch)), float(np.degrees(yaw)), float(np.degrees(roll))


def bone_turn(a: np.ndarray, b: np.ndarray):
    """``(rotation, rms)``: the rigid rotation (Kabsch, no size change) that takes the points ``a`` closest to ``b`` about their
    centres, and the rms distance (metres) left after it. Same point order."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    u, _, vt = np.linalg.svd((b - cb).T @ (a - ca))
    sign = np.sign(np.linalg.det(u @ vt)) or 1.0
    rotation = u @ np.diag([1.0, 1.0, sign]) @ vt
    left = (b - cb) - (a - ca) @ rotation.T
    return rotation, float(np.sqrt((left ** 2).sum(axis=1).mean()))


def lean(top: np.ndarray, bottom: np.ndarray) -> float:
    """The lean of the segment ``bottom`` to ``top`` from vertical (Y), degrees; forward (+Z) positive."""
    d = np.asarray(top, dtype=np.float64) - np.asarray(bottom, dtype=np.float64)
    return float(np.degrees(np.arctan2(d[2], d[1])))


def head_page(rows: Sequence[Tuple[str, np.ndarray, np.ndarray]], names: Sequence[str], source_a: Source, source_b: Source,
              fitted: Optional[Dict[str, np.ndarray]] = None, arma: Optional[Dict[str, np.ndarray]] = None) -> Page:
    """The page. ``rows`` are ``(bone, points of A, points of B)`` (same order); ``fitted`` and ``arma`` map a lower-case joint
    name to its position (the fitted skeleton and the Arma rest) for the lean lines, or None."""
    widths = (14, 7, 7, 7, 7, 7)
    body = [row(("bone", "points", "pitch", "yaw", "roll", "rms cm"), widths, 1)]
    missing: List[str] = []
    first = None
    for bone, a, b in rows:
        if len(a) < MIN_POINTS:
            body.append(row((bone, count(len(a)), NR, NR, NR, NR), widths, 1))
            missing.append("turn of %s (fewer than %d points)" % (bone, MIN_POINTS))
            continue
        rotation, rms = bone_turn(a, b)
        angles = turn_angles(rotation)
        first = first or angles
        body.append(row((bone, count(len(a)), deg(angles[0]), deg(angles[1]), deg(angles[2]), cm(rms)), widths, 1))
    body.append("degrees: pitch nose down +, yaw to the left (+X) +, roll left ear up +")
    body.append("rms = what is left after the rigid fit. reference: 0 = the bone kept its pose")
    if fitted and arma:
        body.append(row(("lean from vertical, deg", "fitted", "Arma"), (24, 8, 8), 1) + "  (forward +)")
        for low, high in (("neck", "neck1"), ("neck1", "head")):
            if all(n in d for d in (fitted, arma) for n in (low, high)):
                body.append(row(("%s -> %s" % (low, high), num(lean(fitted[high], fitted[low])), num(lean(arma[high], arma[low]))),
                                (24, 8, 8), 1))
    elif fitted is not None or arma is not None:
        missing.append("lean lines (need both a fitted skeleton and the Arma skeleton)")
    summary = "head-turn: " + ("%s pitch %s, yaw %s, roll %s deg" % (rows[0][0], deg(first[0]), deg(first[1]), deg(first[2]))
                               if first else NR)
    context = ["scene: %s; turn of the points from A to B" % source_a.scene_name(), source_a.line("A: "), source_b.line("B: ")]
    return Page(summary, context, body, missing, "deform-compare A B --by bone; skeleton; fingers")

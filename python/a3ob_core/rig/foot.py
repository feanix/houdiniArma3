# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no feet analysis; the rule is new (task #82, docs/design/character-rigging.md 2.6, "Feet").
"""Heel, toe tip and sole of the two feet of a standing body, with no Houdini (task #82).

Rig Fit's Refine wraps the sample body onto the user's body (Topo Transfer). The landmarks of Rig Fit stop at the ankle, so
nothing holds the foot ends and the wrapped feet slide: the toes came out 1.6 to 1.8 cm above the owner's sole and the heel
1.6 cm short (task #82). Heel and toe-tip points of both feet fix that. They are found on the mesh itself, the same way on
the user's body and on the sample body, so they are a pair by construction and the user sets nothing.

The rule (``find_feet``), for a body that stands on Y and faces +Z:

1. A foot is the points of one side of the middle line (``x`` against the middle of the lowest points of both feet).
2. Its sole band is the points less than ``SOLE_BAND`` of the body height above its lowest point. The band holds the heel,
   the ball and the toes, and not the shin or the ankle (they are higher than the band at the heel's back end).
3. The forward axis of the foot is the long axis (principal axis) of the band seen from above, turned to face +Z. So a
   foot that is turned out a little is measured along itself.
4. The heel is the back end of the band along that axis and the toe tip is the front end. Each is the mean of the band
   points in the last ``CAP`` of the body height at that end, not one extreme point, so a stray vertex does not move it.
5. The sole pitch is the angle of the edge at the middle of the lower convex hull of the foot seen from the side: the line the
   foot rests on. Toes up is positive.

Why it is robust: the band is relative to the foot's own lowest point and to the body height, so a shoe, a bare foot, a
bigger or smaller body and a body that is not exactly at y = 0 all give the same rule. Heel and toe are ends of the same
band on both bodies, so the pair means the same thing on both. Nothing depends on a vertex number.
"""

import dataclasses
from typing import Dict, Optional

import numpy as np

from .transfer import closest_points

__all__ = ["Foot", "find_feet", "foot_axis", "SOLE_BAND", "CAP", "FOOT_LANDMARKS", "foot_landmarks", "lay_on_sole", "SNAP_BAND"]

#: The sole band: points this share of the body height above the lowest point of the foot (about 6 cm of 1.8 m).
SOLE_BAND = 0.035
#: The end cap of the band, as a share of the body height (about 1.8 cm of 1.8 m).
CAP = 0.01
#: The sole step reaches this share of the body height above the lowest point of a wrapped foot (about 5 cm of 1.8 m).
SNAP_BAND = 0.03
#: A point touches the sole line when it is closer than this share of the body height (about 5 mm of 1.8 m).
TOUCH = 0.003


@dataclasses.dataclass
class Foot:
    """One foot: ``side`` +1 (left, +X) or -1; ``heel`` and ``toe`` (3,) the end points of the sole band; ``axis`` the
    unit forward axis (horizontal); ``pitch`` the sole pitch in degrees (toes up positive, None when the foot has too few
    points); ``low`` the lowest height of the foot."""
    side: int
    heel: np.ndarray
    toe: np.ndarray
    axis: np.ndarray
    pitch: Optional[float]
    low: float
    #: The back and the front end of the flat part of the sole (None when the foot has no sole line).
    sole_heel: Optional[np.ndarray] = None
    sole_toe: Optional[np.ndarray] = None
    #: The middle line (x) between the feet that this foot was cut from, and the body height.
    centre: float = 0.0
    height: float = 0.0
    #: The farthest points of the sole band along ``axis``, back and front: the foot's real ends (``heel`` and ``toe`` are the
    #: means of the end caps, steadier to wrap with).
    back_end: Optional[np.ndarray] = None
    front_end: Optional[np.ndarray] = None


def foot_axis(points: np.ndarray, front: float = 1.0) -> np.ndarray:
    """The unit long axis of ``points`` seen from above (Y up), pointing towards ``front`` * Z."""
    flat = points[:, [0, 2]] - points[:, [0, 2]].mean(axis=0)
    values, vectors = np.linalg.eigh(np.cov(flat.T))
    way = vectors[:, int(np.argmax(values))]
    way = way if way[1] * front >= 0 else -way
    return np.array([way[0], 0.0, way[1]])


def _middle(points: np.ndarray, height: float, low: float) -> float:
    """The middle line (x) between the feet: the middle of the points within a hand's breadth of the sole."""
    feet = points[points[:, 1] < low + 0.04 * height]
    return float((feet[:, 0].min() + feet[:, 0].max()) / 2.0)


def find_feet(points: np.ndarray, front: float = 1.0) -> Dict[int, Foot]:
    """The feet of a standing body ``points`` (n, 3; Y up; the toes towards ``front`` * Z). Returns ``{+1: left,
    -1: right}``; a side with fewer than 12 points in its band is missing."""
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 50:
        return {}
    low = float(points[:, 1].min())
    height = float(points[:, 1].max()) - low
    centre = _middle(points, height, low)
    found: Dict[int, Foot] = {}
    for side in (1, -1):
        mine = points[side * (points[:, 0] - centre) > 0.0]
        mine = mine[mine[:, 1] < low + 0.25 * height]  # the leg below the knee is enough; the rest is not the foot
        if not len(mine):
            continue
        floor = float(mine[:, 1].min())
        band = mine[mine[:, 1] < floor + SOLE_BAND * height]
        if len(band) < 12:
            continue
        axis = foot_axis(band, front)
        along = (band - band.mean(axis=0)) @ axis
        back, forward = float(along.min()), float(along.max())
        cap = CAP * height
        heel = band[along < back + cap].mean(axis=0)
        toe = band[along > forward - cap].mean(axis=0)
        pitch, sole_heel, sole_toe = _sole(mine, band, height, floor, axis, cap)
        found[side] = Foot(side, heel, toe, axis, pitch, floor, sole_heel, sole_toe, centre, height,
                           band[int(np.argmin(along))], band[int(np.argmax(along))])
    return found


def _lower_hull(u, y):
    """The lower convex hull of the 2D points ``(u, y)`` as a list of ``(u, y)``, left to right (monotone chain)."""
    order = np.lexsort((y, u))
    hull = []
    for k in order:
        point = (float(u[k]), float(y[k]))
        while len(hull) >= 2 and ((hull[-1][0] - hull[-2][0]) * (point[1] - hull[-2][1])
                                  - (hull[-1][1] - hull[-2][1]) * (point[0] - hull[-2][0])) <= 0.0:
            hull.pop()
        hull.append(point)
    return hull


def _sole(foot, band, height, floor, axis, cap):
    """``(pitch, sole_heel, sole_toe)`` of a foot. The line the foot rests on is the edge, at the middle of the foot, of the
    lower convex hull of the foot seen from the side (height against distance along ``axis``): where the sole is flat it is
    the whole sole, where the toes are lifted it is the line from the heel to the ball. A sparse mesh (the sample's sole has a
    few points) gives the same line as a dense one, which a fit to the lowest point of each slice would not. The pitch is the
    angle of that line in degrees (toes up positive). ``sole_heel`` and ``sole_toe`` are the back and the front end of the
    points of the sole band that touch the line (within ``TOUCH`` of the height): where the flat sole ends at the heel and at
    the toes. All three are None when the foot has too few points."""
    low_part = foot[foot[:, 1] < floor + 2 * SOLE_BAND * height]
    if len(low_part) < 12:
        return None, None, None
    hull = _lower_hull(low_part @ axis, low_part[:, 1])
    if len(hull) < 2:
        return None, None, None
    middle = 0.5 * (hull[0][0] + hull[-1][0])
    (u0, y0), (u1, y1) = next(edge for edge in zip(hull, hull[1:]) if edge[1][0] >= middle)
    if u1 <= u0:
        return None, None, None
    slope = (y1 - y0) / (u1 - u0)
    along = band @ axis
    touching = np.abs(band[:, 1] - (y0 + slope * (along - u0))) < TOUCH * height
    ends = along[touching]
    sole_heel = band[touching & (along < ends.min() + cap)].mean(axis=0)
    sole_toe = band[touching & (along > ends.max() - cap)].mean(axis=0)
    return float(np.degrees(np.arctan(slope))), sole_heel, sole_toe


#: The names of the foot pairs Refine adds to the landmarks (they are not among the 18 landmarks of Rig Landmarks: they are
#: found on the two meshes, never placed by the user).
FOOT_LANDMARKS = tuple("lm_%s_%s" % (what, tag) for what in ("heel", "toe", "sole_heel", "sole_toe") for tag in "lr")


def foot_landmarks(P, tris, front=1.0):
    """``{name: position}`` of ``FOOT_LANDMARKS`` for the triangle mesh ``(P, tris)`` of a standing body: the heel and the toe
    tip of each foot (``find_feet``), each moved to the closest point of the surface so that it lies on the skin. The same
    call on the user's body and on the sample body gives the pairs that Topo Transfer matches by name."""
    points = P[np.unique(tris)] if len(tris) else P
    feet = find_feet(points, front)
    names, query = [], []
    for side, tag in ((1, "l"), (-1, "r")):
        if side in feet:
            foot = feet[side]
            if foot.sole_heel is None:
                continue
            names += ["lm_%s_%s" % (what, tag) for what in ("heel", "toe", "sole_heel", "sole_toe")]
            query += [foot.heel, foot.toe, foot.sole_heel, foot.sole_toe]
    if not names:
        return {}
    tri, bary, _ = closest_points(np.array(query), P, tris)
    at = (bary[:, :, None] * P[tris[tri]]).sum(1)
    return {name: value for name, value in zip(names, at)}


def lay_on_sole(wrapped, P, tris, front=1.0):
    """The sole step (task #82). ``wrapped`` (n, 3) is the sample body after the wrap; ``(P, tris)`` is the user's body. The
    wrap leaves the lower part of each wrapped foot a little above or away from the user's foot (the landmarks fix the
    ends; between them the sole floats). Each point of the foot within ``SNAP_BAND`` of the body height above the wrapped
    foot's lowest point moves to the closest point of the user's body, fully in the lower half of the band and less and
    less above it, so the sole lies on the user's sole and the rest of the foot does not move. Returns the new points."""
    wrapped = np.asarray(wrapped, dtype=np.float64)
    out = wrapped.copy()
    for foot in find_feet(wrapped, front).values():
        reach = SNAP_BAND * foot.height
        above = wrapped[:, 1] - foot.low
        mine = (foot.side * (wrapped[:, 0] - foot.centre) > 0.0) & (above < reach) & (above > -reach)
        if not mine.any():
            continue
        tri, bary, _ = closest_points(wrapped[mine], P, tris)
        goal = (bary[:, :, None] * P[tris[tri]]).sum(1)
        weight = np.clip((1.0 - above[mine] / reach) / 0.4, 0.0, 1.0)
        out[mine] += weight[:, None] * (goal - wrapped[mine])
    return out

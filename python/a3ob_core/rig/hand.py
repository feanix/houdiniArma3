# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the finger bend is new (task #83, docs/design/character-rigging.md, "Hands").
"""Bend the finger joints of the fitted skeleton to the user's finger mesh, with no Houdini (task #83).

Rig Fit's first pass (``fit.fit_skeleton``) turns and scales every finger chain but keeps it straight, because the only
finger landmarks are the middle fingertip and the thumb tip. A relaxed hand is curled; the straight fitted fingers cannot
follow it, the wrap cannot either, and the cage then keeps the curl. This module finds the joints of every finger chain on the
mesh itself. The result is a position for the two joints after the knuckle of each of the four fingers and the thumb, which
``fit_skeleton`` takes as ``bent`` (so the frames stay pure rotations aimed down the bones, task #76), and a turn and a size
for the hand (``hands``), because a curled middle fingertip is short and points off the hand, so the first pass sizes and
turns the hand a little wrong.

**The centre line of a finger** is not measured on the mesh points directly (the mesh has no finger parts, fingers touch each
other and a finger has only a few dozen points). The sample's own finger is the model: the sample body's points that follow
the bones of that finger, moved rigidly with their bones. A few numbers per finger (the bend at the knuckle, the bend at the
middle joint, a swing to the side at the knuckle, a length scale) pose the model, and the pose is the one that puts the model
closest to the finger's points of the user's mesh (a two-way closest-point distance, truncated at ``REACH``, so that neither a
thick finger, nor a few points, nor one stray point decides it). The centre line is then the model's bones, which sit in the
middle of the model's points by construction, whatever the thickness of the user's finger.

**Which points are a finger's.** A finger curls in the plane of its own line and the palm normal, so a curled finger keeps its
place across the hand. The first round measures the across axis on the points themselves (the long axis of the points just
beyond the knuckles, where the four fingers lie side by side) and gives each point to the finger whose knuckle is nearest across
the hand (two fingers that touch are cut half way between them); the thumb gets the points along the line from its base to its
tip landmark. Later rounds give each point to the closest finger of the pose just found, in 3D, and the palm is a competitor too.

**The hand's frame** (``_fit_turn``, ``_refine_frame``). The turn and size come first from the sample's palm points against the
points of the mesh, and are then made to agree with the fingers found in them (the whole fitted hand, both ways, against all the
points), and the fingers are fitted again, twice. When one hand explains its points clearly worse than the other, it is fitted
once more with the other hand's frame mirrored, and the better of the two is kept.

**Why it holds with few points.** A finger with fewer than ``MIN_POINTS`` points is left straight. The cost is a mean, so a
sparse finger is judged as fairly as a dense one; a weak pull towards the first pass's length scale and the landmarks of the
middle finger and the thumb (their end joints) hold a finger that has little to go on.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .fit import FitResult, swing

__all__ = ["FINGERS", "Chain", "hand_chains", "bend_fingers", "REACH", "MIN_POINTS"]

FINGERS = ("index", "middle", "ring", "pinky", "thumb")
#: A point farther than this (metres) from a finger's model is not that finger's; the cost is cut at it.
REACH = 0.03
#: A finger with fewer points than this stays straight.
MIN_POINTS = 8
#: The turn of the finger bones is searched in this range (degrees): bending towards the palm up to ``FLEX_MAX``, back to
#: ``FLEX_MIN``. The swing to the side is at most ``SWING_MAX`` (fingers) at the knuckle.
FLEX_MIN, FLEX_MAX, SWING_MAX = -35.0, 100.0, 20.0
#: The length scale of a finger is searched within these factors of the scale the first pass gave the hand.
SCALE_LOW, SCALE_HIGH = 0.85, 1.4


@dataclasses.dataclass
class Chain:
    """One finger: ``joints`` the three joint numbers (knuckle, middle, end), ``thumb`` True for the thumb."""
    name: str
    joints: Tuple[int, int, int]
    thumb: bool = False


def hand_chains(bone_index: Dict[str, int], side: str) -> Tuple[Optional[int], List[Chain]]:
    """``(hand joint, chains)`` of ``side`` (``"left"`` or ``"right"``): the chains the skeleton has all three joints of."""
    hand = bone_index.get(side + "hand")
    chains = []
    for name in FINGERS:
        joints = tuple(bone_index.get("%shand%s%d" % (side, name, k)) for k in (1, 2, 3))
        if hand is not None and None not in joints:
            chains.append(Chain(name, joints, name == "thumb"))
    return hand, chains


def _rotation(axis: np.ndarray, degrees: float) -> np.ndarray:
    a = np.radians(degrees)
    k = np.asarray(axis, dtype=np.float64)
    cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(a) * cross + (1.0 - np.cos(a)) * (cross @ cross)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-20)


class _Model:
    """The sample's finger as a rigid model: the points of each of its three bones, moved with the bones."""

    def __init__(self, rest: np.ndarray, chain: Chain, segments: List[np.ndarray], knuckle: np.ndarray, hand_rot: np.ndarray,
                 palm: np.ndarray, anchor: Optional[np.ndarray] = None):
        self.anchor = anchor  # where a landmark says the end joint is (middle finger, thumb), or None
        self.rest = rest[list(chain.joints)]
        self.segments = segments
        self.knuckle = knuckle  # J1 in the fitted pose (the first pass keeps it fixed on the hand)
        self.hand_rot = hand_rot
        self.thumb = chain.thumb
        self.name = chain.name
        self.a1 = rest[chain.joints[1]] - rest[chain.joints[0]]
        self.a2 = rest[chain.joints[2]] - rest[chain.joints[1]]
        self.d = _unit(hand_rot @ self.a1)  # the straight finger's direction
        self.palm = palm
        self.flex = _unit(np.cross(self.d, palm))  # turning about this tilts the finger towards +palm
        self.points_rest = np.concatenate(segments) if segments else np.zeros((0, 3))

    def directions(self, p: Sequence[float]):
        """``(u1, u2)`` the unit directions of bone 1 and bone 2 for ``p = (flex 1, swing 1, flex 2, swing 2)`` in degrees."""
        u1 = _rotation(self.palm, p[1]) @ _rotation(self.flex, p[0]) @ self.d
        u2 = _rotation(self.palm, p[3]) @ _rotation(self.flex, p[2]) @ u1
        return u1, u2

    def joints(self, p: Sequence[float], s: float):
        """``(J1, J2, J3, R1, R2)``: the joints and the bone rotations of the pose ``p`` at length scale ``s``."""
        u1, u2 = self.directions(p)
        r1 = swing(self.d, u1) @ self.hand_rot
        r2 = swing(u1, u2) @ r1
        j2 = self.knuckle + s * r1 @ self.a1
        j3 = j2 + s * r2 @ self.a2
        return self.knuckle, j2, j3, r1, r2

    def points(self, p: Sequence[float], s: float) -> np.ndarray:
        j1, j2, j3, r1, r2 = self.joints(p, s)
        parts = []
        for seg, joint, rot, home in zip(self.segments, (j1, j2, j3), (r1, r2, r2), self.rest):
            if len(seg):
                parts.append(joint + s * (seg - home) @ rot.T)
        return np.concatenate(parts) if parts else np.zeros((0, 3))


def _ranges(model: _Model, sign: float):
    """The search range of ``(flex 1, swing 1, flex 2, swing 2)`` in degrees, flex towards ``sign`` times the palm normal."""
    lo1, hi1 = (FLEX_MIN, FLEX_MAX) if sign > 0 else (-FLEX_MAX, -FLEX_MIN)
    lo2, hi2 = (FLEX_MIN, FLEX_MAX + 10.0) if sign > 0 else (-FLEX_MAX - 10.0, -FLEX_MIN)
    if model.thumb:  # the thumb turns every way; its swing is searched as far as its flex
        return [(lo1, hi1), (-60.0, 60.0), (lo2, hi2), (-60.0, 60.0)]
    return [(lo1, hi1), (-SWING_MAX, SWING_MAX), (lo2, hi2), (0.0, 0.0)]


def _palm_normal(rest: np.ndarray, bone_index: Dict[str, int], side: str, r_hand: np.ndarray) -> Optional[np.ndarray]:
    """The normal of the palm in the fitted pose (the sign is not known): square to the line across the knuckles and to the
    middle finger, from the sample's rest, turned by the hand's rotation."""
    names = ["%shand%s1" % (side, f) for f in ("index", "middle", "pinky")] + ["%shandmiddle2" % side]
    if any(n not in bone_index for n in names):
        return None
    index1, middle1, pinky1, middle2 = (rest[bone_index[n]] for n in names)
    n0 = np.cross(pinky1 - index1, middle2 - middle1)
    if np.linalg.norm(n0) < 1e-9:
        return None
    return _unit(r_hand @ _unit(n0))


def _distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))


class _Hand:
    """One hand's model (the five fingers, each a rigid piece of the sample's points) against the points of the user's mesh
    around that hand. Each finger has its own set of the user's points. Its cost is a two-way closest-point cost: every point
    of its set should be close to the finger's model (so the model reaches the fingertip and does not stop short) and every
    point of the model close to a point of the set (so the model stays inside the mesh, and a thick or a thin finger makes no
    difference: the model is in the middle of the points, not on them)."""

    def __init__(self, models: List[_Model], sets: List[np.ndarray]):
        self.models = models
        self.sets = sets
        self.scale0 = 1.0  # the hand's scale from the first pass; ``bend_fingers`` sets it
        self.poses: List[Tuple[Sequence[float], float]] = [((0.0, 0.0, 0.0, 0.0), 0.0) for _ in models]

    def set_pose(self, k: int, pose, scale: float) -> None:
        self.poses[k] = (list(pose), scale)

    def cost(self, k: int, pose, scale: float) -> float:
        """The cost of finger ``k`` in ``pose`` at ``scale``."""
        pts = self.models[k].points(pose, scale)
        if not len(pts) or not len(self.sets[k]):
            return np.inf
        d = _distances(self.sets[k], pts)
        total = float((np.minimum(d.min(axis=1), REACH) ** 2).mean() + (np.minimum(d.min(axis=0), REACH) ** 2).mean())
        total += SCALE_PRIOR * (scale / self.scale0 - 1.0) ** 2  # a short or long finger needs more than a few points
        anchor = self.models[k].anchor
        if anchor is not None:  # the end joint of this finger should be near the landmark the user set (or the guess)
            total += ANCHOR * min(float(np.linalg.norm(self.models[k].joints(pose, scale)[2] - anchor)), 2.0 * REACH) ** 2
        return total

    def fit_finger(self, k: int, sign: float, scale0: float, starts: Sequence[Sequence[float]]):
        """The best ``(cost, pose, scale)`` of finger ``k`` bending towards ``sign`` times the palm normal: coordinate search
        from ``starts`` on a grid whose step is halved until it is under 2 degrees."""
        ranges = _ranges(self.models[k], sign)
        best = (np.inf, None, scale0)
        for start in starts:
            p = [min(max(v * sign if i in (0, 2) else v, lo), hi) for i, (v, (lo, hi)) in enumerate(zip(start[:4], ranges))]
            s = start[4] if len(start) > 4 else scale0
            c = self.cost(k, p, s)
            step = 16.0
            while step >= 2.0:
                moved = True
                while moved:
                    moved = False
                    for i, (lo, hi) in enumerate(ranges):
                        if lo == hi:
                            continue
                        for delta in (-step, step):
                            q = list(p)
                            q[i] = min(max(q[i] + delta, lo), hi)
                            cq = self.cost(k, q, s)
                            if cq < c - 1e-12:
                                p, c, moved = q, cq, True
                    for factor in (1.0 - 0.01 * step, 1.0 + 0.01 * step):
                        t = min(max(s * factor, SCALE_LOW * scale0), SCALE_HIGH * scale0)
                        ct = self.cost(k, p, t)
                        if ct < c - 1e-12:
                            s, c, moved = t, ct, True
                step /= 2.0
            if c < best[0]:
                best = (c, p, s)
        return best


#: Where the search of a finger starts (flex 1, swing 1, flex 2, swing 2; the flex towards the bending side).
STARTS = ((0.0, 0.0, 0.0, 0.0), (30.0, 0.0, 40.0, 0.0), (60.0, 0.0, 70.0, 0.0))
#: The thumb moves in more ways than a finger, so it starts from more places (and its swings are free).
THUMB_STARTS = STARTS + ((30.0, 35.0, 30.0, 0.0), (30.0, -35.0, 30.0, 0.0), (10.0, 35.0, -20.0, 0.0), (10.0, -35.0, -20.0, 0.0))
#: The weight of the distance of a finger's end joint from its landmark, against the mean squared distance of its points.
ANCHOR = 1.0
#: The weight of the squared relative change of a finger's length scale (a few points may not decide the length).
SCALE_PRIOR = 0.004
#: At most this many of the user's points around a hand are used (an even sample keeps the cost and the shape).
MAX_OWNER = 500
#: In the first round the thumb gets the points this close (metres) to the line from its base to its tip landmark.
THUMB_RADIUS = 0.025
#: How many times the hand's frame is made to agree with its fingers (and the fingers fitted again), and the most it moves (degrees).
TURN_ROUNDS = 2
TURN_STEP_MAX = 20.0
RATIO_STEP_MAX = 0.25
#: The finger fit is repeated this many times, each time with the points given to the finger closest to them.
ROUNDS = 2


def bend_fingers(parents: Sequence[int], rest: np.ndarray, fit: FitResult, bone_index: Dict[str, int], sample_points: np.ndarray,
                 sample_weights: np.ndarray, user_points: np.ndarray):
    """``(hands, bent, info)``. ``bent`` maps each finger joint number (the middle and end joint of every chain) to its position;
    ``hands`` maps a hand joint number to ``(rotation, ratio)``: a turn about the wrist and a change of size that its first-pass
    rotation and scale get, because a curled middle finger is short and pulls the first pass's hand towards the fingertip. ``info`` maps ``"left index"`` to
    ``(bend at the knuckle, bend at the middle joint, length scale over the hand's)`` for the messages. ``fit`` is the first
    pass (straight fingers); ``sample_points`` and ``sample_weights`` are the sample body at the rest and its weights;
    ``user_points`` are the skin points of the user's mesh.

    The two hands are fitted on their own. A hand whose result explains the points of its side clearly worse than the other
    hand's is fitted again with the other hand's frame mirrored (the frame is the part of the fit that goes wrong when a hand is
    curled, and the two hands of a character are alike more often than not); the better result of the two is kept."""
    rest = np.asarray(rest, dtype=np.float64)
    dominant = np.where(sample_weights.sum(axis=1) > 0, sample_weights.argmax(axis=1), -1)
    sides = {}
    for side in ("left", "right"):
        found = _fit_side(side, rest, fit, bone_index, sample_points, dominant, user_points, None)
        if found is not None:
            sides[side] = found
    if len(sides) == 2:
        worse = max(sides, key=lambda name: sides[name].objective)
        better = "left" if worse == "right" else "right"
        if sides[worse].objective > MIRROR_WORSE * sides[better].objective:
            mirrored = (_MIRROR @ sides[better].turn @ _MIRROR, sides[better].ratio)
            again = _fit_side(worse, rest, fit, bone_index, sample_points, dominant, user_points, mirrored)
            if again is not None and again.objective < sides[worse].objective:
                sides[worse] = again
    hands: Dict[int, tuple] = {}
    bent: Dict[int, np.ndarray] = {}
    info: Dict[str, tuple] = {}
    for side, found in sides.items():
        hands[found.hand] = (found.turn, found.ratio)
        for k, (chain, model) in enumerate(zip(found.chains, found.hand_fit.models)):
            pose, scale = found.hand_fit.poses[k]
            if scale <= 0.0:
                continue
            _, j2, j3, _, _ = model.joints(pose, scale)
            bent[chain.joints[1]], bent[chain.joints[2]] = j2, j3
            info["%s %s" % (side, chain.name)] = (pose[0], pose[2], scale / found.s_hand)
    return hands, bent, info


#: The reflection across the plane x = 0 (the character faces +Z with Y up and its left on +X, as the symmetry of the landmarks).
_MIRROR = np.diag([-1.0, 1.0, 1.0])
#: A hand is fitted again with the other hand's mirrored frame when its objective is more than this times the other's.
MIRROR_WORSE = 1.25


@dataclasses.dataclass
class _Side:
    hand: int
    chains: List[Chain]
    hand_fit: _Hand
    turn: np.ndarray
    ratio: float
    fixed: np.ndarray
    owner: np.ndarray
    s_hand: float
    objective: float


def _objective(hand_fit: _Hand, fixed: np.ndarray, owner: np.ndarray) -> float:
    """How well the whole fitted hand (palm and fingers) explains the points of its side: the two-way closest-point cost
    between the points and the union of the finger models and the palm, no partition of the points needed."""
    parts = [fixed]
    for k, model in enumerate(hand_fit.models):
        pose, scale = hand_fit.poses[k]
        if scale > 0.0:
            parts.append(model.points(pose, scale))
    d = _distances(owner, np.concatenate(parts))
    return float((np.minimum(d.min(axis=1), REACH) ** 2).mean() + (np.minimum(d.min(axis=0), REACH) ** 2).mean())


def _fit_side(side: str, rest: np.ndarray, fit: FitResult, bone_index: Dict[str, int], sample_points: np.ndarray,
              dominant: np.ndarray, user_points: np.ndarray, frame: Optional[Tuple[np.ndarray, float]]) -> Optional[_Side]:
    """The fit of one hand: its frame (``frame`` = ``(turn, ratio)``, or found from the palm when None, then made to agree with
    the fingers) and the pose of each finger. None when the skeleton has no hand, too few fingers or no palm."""
    hand, chains = hand_chains(bone_index, side)
    if hand is None or len(chains) < 2:
        return None
    s_first = float(fit.scales[hand])
    r_hand = fit.linear[hand] / max(s_first, 1e-12)
    wrist = fit.positions[hand]
    palm = _palm_normal(rest, bone_index, side, r_hand)
    if palm is None:
        return None
    reach = REACH + max(float(np.linalg.norm(fit.positions[c.joints[2]] - wrist)) for c in chains)
    mine = user_points[np.linalg.norm(user_points - wrist, axis=1) < reach + 0.05]
    if len(mine) > MAX_OWNER:
        mine = mine[np.linspace(0, len(mine) - 1, MAX_OWNER).astype(int)]
    palm_rest = sample_points[dominant == hand]
    middle = next((c for c in chains if c.name == "middle"), chains[0])
    toward = rest[middle.joints[1]] - rest[middle.joints[0]]
    if frame is None:
        turn, ratio = _fit_turn(palm_rest, rest[hand], wrist, s_first, r_hand, palm, _unit(r_hand @ toward), mine), 1.0
    else:
        turn, ratio = frame
    poses = None
    for iteration in range(1 if frame is not None else TURN_ROUNDS + 1):
        s_hand = s_first * ratio
        r_new, palm_new = turn @ r_hand, turn @ palm
        models = []
        for chain in chains:
            segments = [sample_points[dominant == j] for j in chain.joints]
            knuckle = wrist + ratio * (turn @ (fit.positions[chain.joints[0]] - wrist))
            models.append(_Model(rest, chain, segments, knuckle, r_new, palm_new, fit.targets.get(chain.joints[2])))
        fixed = wrist + s_hand * (palm_rest - rest[hand]) @ r_new.T
        hand_fit = _Hand(models, _first_assignment(models, mine))
        hand_fit.scale0 = s_hand
        if poses is not None:  # the frame changed: the fingers start from the poses they had
            hand_fit.poses = poses
            hand_fit.sets = _assign(hand_fit, mine, fixed)
        for number in range(ROUNDS if poses is None else 1):
            if number:
                hand_fit.sets = _assign(hand_fit, mine, fixed)
            _fit_fingers(hand_fit, s_hand)
        poses = hand_fit.poses
        if iteration < TURN_ROUNDS and frame is None:
            dturn, dratio = _refine_frame(hand_fit, mine, fixed, wrist, palm_new, _unit(r_new @ toward))
            turn, ratio = dturn @ turn, ratio * dratio
            poses = [(pose, scale * dratio if scale > 0.0 else scale) for pose, scale in poses]
    return _Side(hand, chains, hand_fit, turn, ratio, fixed, mine, s_hand, _objective(hand_fit, fixed, mine))


def _refine_frame(hand_fit: _Hand, owner: np.ndarray, fixed: np.ndarray, wrist: np.ndarray, palm: np.ndarray,
                  forward: np.ndarray):
    """``(rotation, ratio)``: a small turn about the wrist and a change of size about it that put the whole fitted hand (palm
    and fingers, as they are) closest to all of the user's points, both ways. The first pass sizes and turns the hand from the
    fingertips, and a curled finger is short and points off the hand, so both are a little wrong. The hand's frame decides which
    points go to which finger, so it is made to agree with the fingers that were found in it, then the fingers are fitted again."""
    parts = [fixed]
    for k, model in enumerate(hand_fit.models):
        pose, scale = hand_fit.poses[k]
        if scale > 0.0:
            parts.append(model.points(pose, scale))
    hand = np.concatenate(parts)
    axes = (_unit(np.cross(forward, palm)), palm, forward)

    def turned(angles):
        rot = np.eye(3)
        for axis, a in zip(axes, angles):
            rot = _rotation(axis, a) @ rot
        return rot

    def cost(q):
        d = _distances(owner, wrist + q[3] * (hand - wrist) @ turned(q[:3]).T)
        return float((np.minimum(d.min(axis=1), REACH) ** 2).mean() + (np.minimum(d.min(axis=0), REACH) ** 2).mean())

    best, c = [0.0, 0.0, 0.0, 1.0], cost([0.0, 0.0, 0.0, 1.0])
    step = 8.0
    while step >= 1.0:
        moved = True
        while moved:
            moved = False
            for i in range(4):
                for delta in (-step, step):
                    q = list(best)
                    if i < 3:
                        q[i] = min(max(q[i] + delta, -TURN_STEP_MAX), TURN_STEP_MAX)
                    else:
                        q[3] = min(max(q[3] + delta * 0.005, 1.0 - RATIO_STEP_MAX), 1.0 + RATIO_STEP_MAX)
                    cq = cost(q)
                    if cq < c - 1e-12:
                        best, c, moved = q, cq, True
        step /= 2.0
    return turned(best[:3]), best[3]


def _fit_turn(palm_rest: np.ndarray, rest_wrist: np.ndarray, wrist: np.ndarray, s_hand: float, r_hand: np.ndarray,
              palm: np.ndarray, forward: np.ndarray, owner: np.ndarray) -> np.ndarray:
    """The rotation about the wrist (applied after the first pass's hand rotation) that puts the sample's palm points closest
    to the user's points: three angles (about the flexing axis, the palm normal, the finger direction) by coordinate search."""
    if len(palm_rest) < MIN_POINTS or len(owner) < MIN_POINTS:
        return np.eye(3)
    flex = _unit(np.cross(forward, palm))
    axes = (flex, palm, forward)
    limits = ((-70.0, 70.0), (-35.0, 35.0), (-35.0, 35.0))
    base = wrist + s_hand * (palm_rest - rest_wrist) @ r_hand.T

    def turned(angles):
        rot = np.eye(3)
        for axis, a in zip(axes, angles):
            rot = _rotation(axis, a) @ rot
        return rot

    def cost(angles):
        pts = wrist + (base - wrist) @ turned(angles).T
        return float((np.minimum(_distances(pts, owner).min(axis=1), REACH) ** 2).mean())

    best, c = [0.0, 0.0, 0.0], cost([0.0, 0.0, 0.0])
    step = 16.0
    while step >= 1.0:
        moved = True
        while moved:
            moved = False
            for i, (lo, hi) in enumerate(limits):
                for delta in (-step, step):
                    q = list(best)
                    q[i] = min(max(q[i] + delta, lo), hi)
                    cq = cost(q)
                    if cq < c - 1e-12:
                        best, c, moved = q, cq, True
        step /= 2.0
    return turned(best)


def _fit_fingers(hand_fit: _Hand, s_hand: float) -> None:
    """Fit every finger of ``hand_fit`` to its own set, in place. The four fingers bend the same way (the side that suits the
    middle finger better), the thumb picks its own. A finger with fewer than ``MIN_POINTS`` points stays as it is."""
    models = hand_fit.models
    order = sorted(range(len(models)), key=lambda k: (models[k].thumb, models[k].name != "middle"))
    signs: Dict[bool, float] = {}
    for k in order:
        if len(hand_fit.sets[k]) < MIN_POINTS or not len(models[k].points_rest):
            continue
        tries = (1.0, -1.0) if models[k].thumb not in signs else (signs[models[k].thumb],)
        starts = THUMB_STARTS if models[k].thumb else STARTS
        pose, scale = hand_fit.poses[k]
        found = []
        for sign in tries:
            here = ((pose[0] * sign, pose[1], pose[2] * sign, pose[3], scale),) if scale > 0.0 else ()
            found.append((hand_fit.fit_finger(k, sign, s_hand, starts + here), sign))
        (cost, pose, scale), sign = min(found, key=lambda item: item[0][0])
        signs[models[k].thumb] = sign
        hand_fit.set_pose(k, pose, scale)


def _first_assignment(models: List[_Model], points: np.ndarray) -> List[np.ndarray]:
    """The points of each finger in the first round, from where they lie across the hand. A finger curls in the plane of its
    own line and the palm normal, so a curled finger keeps its place across the hand. The across axis is measured on the
    points themselves, as the long axis of the points just beyond the knuckles seen along the hand (the four fingers lie side
    by side there), so a hand frame that is a little twisted does not mix the fingers up. The boundary between two fingers is
    half way between their knuckles; a point beyond the first finger by more than ``THUMB_EDGE`` finger gaps is the thumb's."""
    fingers = [k for k, m in enumerate(models) if not m.thumb]
    thumbs = [k for k, m in enumerate(models) if m.thumb]
    empty = [points[:0] for _ in models]
    if len(fingers) < 2:
        return empty
    forward = _unit(sum(models[k].d for k in fingers))
    centre = np.mean([models[k].knuckle for k in fingers], axis=0)
    along = (points - centre) @ forward
    band = points[(along > -0.005) & (along < 0.06)]
    if len(band) < 6:
        return empty
    flat = band - np.outer((band - centre) @ forward, forward)
    values, vectors = np.linalg.eigh(np.cov((flat - flat.mean(axis=0)).T))
    across = _unit(vectors[:, int(np.argmax(values))])
    anchor = models[thumbs[0]].knuckle if thumbs else models[fingers[0]].knuckle
    if (anchor - models[fingers[-1]].knuckle) @ across < 0:  # towards the thumb side (the first finger's side)
        across = -across
    where = np.array([float((models[k].knuckle - centre) @ across) for k in fingers])
    lateral = (points - centre) @ across
    nearest = np.array(fingers)[np.abs(lateral[:, None] - where[None, :]).argmin(axis=1)]
    behind = np.zeros(len(points), dtype=bool)
    for k in fingers:  # only beyond that finger's own knuckle
        behind |= (nearest == k) & (((points - models[k].knuckle) @ models[k].d) < -0.2 * float(np.linalg.norm(models[k].a1)))
    is_thumb = np.zeros(len(points), dtype=bool)
    for k in thumbs:  # the thumb lies along the line from its base to its tip landmark (the tip is the best-known point)
        m = models[k]
        tip = m.anchor if m.anchor is not None else m.knuckle + m.d * float(np.linalg.norm(m.a1 + m.a2))
        line = tip - m.knuckle
        t = np.clip(((points - m.knuckle) @ line) / max(float(line @ line), 1e-12), 0.0, 1.0)
        is_thumb |= np.linalg.norm(points - (m.knuckle + t[:, None] * line), axis=1) < THUMB_RADIUS
    out = empty
    for k in fingers:
        out[k] = points[(nearest == k) & ~behind & ~is_thumb]
    for k in thumbs:
        out[k] = points[is_thumb]
    return out


def _assign(hand_fit: _Hand, points: np.ndarray, fixed: np.ndarray) -> List[np.ndarray]:
    """The points of each finger from the poses just found: the closest finger model in 3D. The palm (``fixed``) is a model
    too, so the points of the palm are no finger's; a point farther than ``REACH`` from every model is nobody's."""
    best = np.minimum(_distances(points, fixed).min(axis=1), REACH) if len(fixed) else np.full(len(points), REACH)
    owner = np.full(len(points), -1)
    for k, model in enumerate(hand_fit.models):
        pose, scale = hand_fit.poses[k]
        if scale <= 0.0:  # not fitted (too few points)
            continue
        d = _distances(points, model.points(pose, scale)).min(axis=1)
        better = d < best
        best[better], owner[better] = d[better], k
    return [points[owner == k] for k in range(len(hand_fit.models))]

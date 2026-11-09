# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton frames. The rule is the one of docs/design/character-rigging.md 6.2 ("Joint orientation", task #86).
"""The one orientation rule for every skeleton the nodes output, with no Houdini (task #86).

A frame is a pure rotation (columns = the joint's x, y, z axes in world space). The rule, in full, is in
docs/design/character-rigging.md ("Joint orientation"); in short:

- **x** points at the main child. With several children the main child is named: Pelvis -> Spine, Spine3 -> neck,
  Hand -> HandMiddle1, HandRing -> HandRing1. Any other joint with several children aims at the one closest to the
  direction its own bone came in by (``fit.aim_targets``). A child in the joint's own place (Pelvis -> Spine) is looked
  through. A joint with no child takes its parent's frame.
- **z is the hinge** of elbows, knees, fingers and thumbs; **y** is the side reference squared to x, z = x cross y. The
  reference y is a vector that mirrors (a vector along the mirror plane's normal flips, one in the plane does not), so the left
  and right limbs get frames that mirror and a positive turn about z bends both sides the same way (it flexes). Reference
  by joint: arms, torso and head world +Z (forward); legs and feet world -Z (so a knee flexes positively too); the hand
  the palm normal of that hand; a finger or thumb the normal of the plane of its three joints when it is bent by 5
  degrees or more (its own bend, so the hinge is exact), else the palm normal.
- **Roll joints** (ArmRoll, ForeArmRoll, UpLegRoll, LegRoll) share their parent's twist: the parent's frame swung onto the
  roll joint's own x.

``frames`` is the rule; ``rule_targets`` gives the joint each x is aimed at, and ``hinge_chains`` the joints each hinge is
measured on, so the check (``digest/orient.py``) reads the same rule.
"""

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .fit import MIN_BONE, aim_targets, swing

__all__ = ["MAIN_CHILD", "HINGE_CHAINS", "FINGER_MIN_BEND", "ROLLS", "LEG_JOINTS", "frames", "rule_targets", "hinge_chains",
           "bend_normal", "palm_normal", "split_side"]

#: Joint (side prefix removed, lower case) -> its main child (same side), for joints with several children.
MAIN_CHILD = {"pelvis": "spine", "spine3": "neck", "hand": "handmiddle1", "handring": "handring1"}
#: Names (side prefix removed, lower case) of the roll joints.
ROLLS = ("armroll", "forearmroll", "uplegroll", "legroll")
#: The legs and feet: their reference y is world -Z so that a positive turn about z flexes the knee.
LEG_JOINTS = ("upleg", "uplegroll", "leg", "legroll", "foot", "toebase")
#: The five fingers; a finger joint is ``Hand<finger><1..3>``.
FINGERS = ("index", "middle", "ring", "pinky", "thumb")
#: A finger bent less than this (degrees) at its middle joint does not decide a plane; it takes the palm normal instead.
FINGER_MIN_BEND = 5.0
#: A bone this close to the reference direction (cosine) takes world +Y as the reference instead.
PARALLEL = 0.95
#: Sign of the palm normal of a left hand (the right hand's is the opposite, so the two mirror).
PALM_SIGN_LEFT = 1.0

#: The hinge chains: ``(name, first, middle, last)`` with the joint names without the side. ``first`` and ``middle`` carry
#: the hinge; the measure is the angle between local z and the normal of the plane of the three joints.
HINGE_CHAINS: Tuple[Tuple[str, str, str, str], ...] = (
    ("elbow", "Arm", "ForeArm", "Hand"),
    ("knee", "UpLeg", "Leg", "Foot"),
    ("index", "HandIndex1", "HandIndex2", "HandIndex3"),
    ("middle", "HandMiddle1", "HandMiddle2", "HandMiddle3"),
    ("ring", "HandRing1", "HandRing2", "HandRing3"),
    ("pinky", "HandPinky1", "HandPinky2", "HandPinky3"),
    ("thumb", "HandThumb1", "HandThumb2", "HandThumb3"),
)


def split_side(name: str) -> Tuple[str, str]:
    """``(side, rest)``: ``"Left"`` or ``"Right"`` and the name without it, or ``("", name)``."""
    for side in ("Left", "Right"):
        if name.startswith(side) and len(name) > len(side):
            return side, name[len(side):]
    return "", name


def _children(parents: Sequence[int]) -> List[List[int]]:
    out: List[List[int]] = [[] for _ in parents]
    for joint, parent in enumerate(parents):
        if parent >= 0:
            out[parent].append(joint)
    return out


def _reach(origin: int, joint: int, children: List[List[int]], positions: np.ndarray) -> Optional[int]:
    """``joint`` if it is away from ``origin``; else the first joint below it that is (a bone of no length is looked through)."""
    if np.linalg.norm(positions[joint] - positions[origin]) > MIN_BONE:
        return joint
    for child in children[joint]:
        found = _reach(origin, child, children, positions)
        if found is not None:
            return found
    return None


def rule_targets(names: Sequence[str], parents: Sequence[int], positions: np.ndarray) -> List[int]:
    """For every joint the joint its x axis is aimed at (-1 for a chain end): the named main child where the joint has one,
    else ``fit.aim_targets``."""
    positions = np.asarray(positions, dtype=np.float64)
    targets = list(aim_targets(parents, positions))
    children = _children(parents)
    index: Dict[str, int] = {}
    for number, name in enumerate(names):
        index.setdefault(name.lower(), number)
    for joint, name in enumerate(names):
        side, rest = split_side(name)
        wanted = MAIN_CHILD.get(rest.lower())
        child = index.get((side + wanted).lower()) if wanted else None
        if child is not None and parents[child] == joint:
            found = _reach(joint, child, children, positions)
            if found is not None:
                targets[joint] = found
    return targets


def _unit(vector: np.ndarray) -> Optional[np.ndarray]:
    length = float(np.linalg.norm(vector))
    return vector / length if length > 1e-12 else None


def palm_normal(side: str, hand: np.ndarray, middle: np.ndarray, index: np.ndarray, pinky: np.ndarray) -> Optional[np.ndarray]:
    """The palm normal of a hand from its joints: the hand's direction (hand to the middle finger's base) crossed with the way
    across the hand (little finger's base to index finger's base). The right hand's is the left's mirrored."""
    normal = _unit(np.cross(middle - hand, index - pinky))
    if normal is None:
        return None
    return normal * (PALM_SIGN_LEFT if side == "Left" else -PALM_SIGN_LEFT)


def bend_normal(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> Tuple[Optional[np.ndarray], float]:
    """``(normal, bend degrees)`` of the chain a, b, c: the normal turns the first bone onto the second (right hand rule)."""
    u, v = b - a, c - b
    n = np.cross(u, v)
    lu, lv, ln = np.linalg.norm(u), np.linalg.norm(v), np.linalg.norm(n)
    if lu < 1e-9 or lv < 1e-9:
        return None, 0.0
    return (n / ln if ln > 1e-12 else None), float(np.degrees(np.arcsin(min(1.0, ln / (lu * lv)))))


def hinge_chains(names: Sequence[str]) -> List[Tuple[str, str, Tuple[int, int, int]]]:
    """``(label, side, (a, b, c))`` for every hinge chain of ``HINGE_CHAINS`` whose three joints are in ``names``."""
    index = {name.lower(): number for number, name in enumerate(names)}
    found = []
    for side in ("Left", "Right"):
        for label, first, middle, last in HINGE_CHAINS:
            joints = [index.get((side + part).lower()) for part in (first, middle, last)]
            if None not in joints:
                found.append((label, side, tuple(joints)))
    return found


def frames(names: Sequence[str], parents: Sequence[int], positions: np.ndarray) -> np.ndarray:
    """The frame of every joint by the rule above: ``(n, 3, 3)`` pure rotations, columns x, y, z."""
    positions = np.asarray(positions, dtype=np.float64)
    count = len(parents)
    index: Dict[str, int] = {}
    for number, name in enumerate(names):
        index.setdefault(name.lower(), number)
    targets = rule_targets(names, parents, positions)
    out = np.tile(np.eye(3), (count, 1, 1))
    bend_plane: Dict[Tuple[str, str], Optional[np.ndarray]] = {}
    palm: Dict[str, Optional[np.ndarray]] = {}
    for side in ("Left", "Right"):
        parts = [index.get((side + part).lower()) for part in ("Hand", "HandMiddle1", "HandIndex1", "HandPinky1")]
        palm[side] = palm_normal(side, *(positions[j] for j in parts)) if None not in parts else None
        for finger in FINGERS:
            joints = [index.get((side + "Hand" + finger + str(k)).lower()) for k in (1, 2, 3)]
            bend_plane[(side, finger)] = None
            if None not in joints:
                normal, bend = bend_normal(*(positions[j] for j in joints))
                bend_plane[(side, finger)] = normal if bend >= FINGER_MIN_BEND else None
    for joint in range(count):
        parent = parents[joint]
        target = targets[joint]
        if target < 0:
            out[joint] = out[parent] if parent >= 0 else np.eye(3)
            continue
        x = _unit(positions[target] - positions[joint])
        side, rest = split_side(names[joint])
        key = rest.lower()
        if key in ROLLS and parent >= 0:
            out[joint] = swing(out[parent][:, 0], x) @ out[parent]
            continue
        z_hinge = None
        reference = np.array([0.0, 0.0, 1.0])
        if key in LEG_JOINTS:
            reference = -reference
        finger = next((f for f in FINGERS if key in ("hand%s1" % f, "hand%s2" % f, "hand%s3" % f)), None) if side else None
        if side and (key in ("hand", "handring") or finger):
            if finger and bend_plane[(side, finger)] is not None:
                z_hinge = bend_plane[(side, finger)]
            elif palm[side] is not None:
                reference = palm[side]
        if z_hinge is not None:
            z = z_hinge - (z_hinge @ x) * x
            y = np.cross(z / np.linalg.norm(z), x)
            z = np.cross(x, y)
        else:
            if abs(x @ reference) > PARALLEL:
                reference = np.array([0.0, 1.0, 0.0])
            y = reference - (reference @ x) * x
            y /= np.linalg.norm(y)
            z = np.cross(x, y)
        out[joint] = np.stack([x, y, z], axis=1)
    return out

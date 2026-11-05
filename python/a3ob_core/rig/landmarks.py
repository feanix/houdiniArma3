# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no auto-rigging. The landmark idea (about a dozen points, guessed, shown, corrected by the user, with
# symmetry) follows the usual template-fitting workflow (docs/design/character-rigging.md 1.2 and 1.5); the code is new.
"""Landmarks of a humanoid mesh for the Rig Fit node, with no Houdini (docs/design/character-rigging.md 1.5, 6.2).

A landmark is one named point of the body. Sixteen of the eighteen are joints (shoulder, elbow, wrist, hip, knee, ankle, and the
last joint of the middle finger and of the thumb, on each side); two are points on the skin (chin, groin). The mesh stands upright (Y up), faces +Z and has its arms away from
the body (A-pose, D37 Q5), like the Arma sample body.

The auto-guess has no tuned numbers. It runs the same measurements on the sample body, where the joints are known (the
skeleton), and on the user's mesh, and moves each joint by the difference. So a bias of a measurement (a joint is always a
little behind the middle of the limb) cancels. The only numbers that are not measured are the head: the sample body has no
head, so the chin is placed from the head joint (``CHIN_FROM_HEAD``) and on the user's mesh from the height of the head
(``HEAD_SHARE``). Both are estimates (unverified).

All of it is numpy (numpy 1.26 and 2.x).
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .fit import swing
from .transfer import closest_points

__all__ = ["Landmark", "LANDMARKS", "NAMES", "PAIRS", "CHIN_FROM_HEAD", "HEAD_SHARE", "Template", "make_template",
           "guess_landmarks", "find_groin", "symmetrize", "snap_to_surface", "facing_sign", "is_set", "mirror_x",
           "follow_mirror"]


@dataclasses.dataclass(frozen=True)
class Landmark:
    name: str
    #: The Arma joint the landmark stands for ("" for a point on the skin).
    bone: str
    #: True for a point on the skin (chin, groin): it is snapped to the mesh. A joint lies inside the body.
    surface: bool
    #: What the point is, in plain words (for the help).
    text: str


LANDMARKS: Tuple[Landmark, ...] = (
    Landmark("lm_chin", "", True, "The tip of the chin."),
    Landmark("lm_groin", "", True, "The crotch, the highest point between the legs."),
    Landmark("lm_shoulder_l", "LeftArm", False, "The left shoulder joint, inside the upper arm where it meets the body."),
    Landmark("lm_shoulder_r", "RightArm", False, "The right shoulder joint."),
    Landmark("lm_elbow_l", "LeftForeArm", False, "The left elbow joint, in the middle of the arm."),
    Landmark("lm_elbow_r", "RightForeArm", False, "The right elbow joint."),
    Landmark("lm_wrist_l", "LeftHand", False, "The left wrist joint, in the middle of the arm."),
    Landmark("lm_wrist_r", "RightHand", False, "The right wrist joint."),
    Landmark("lm_hip_l", "LeftUpLeg", False, "The left hip joint, inside the top of the thigh."),
    Landmark("lm_hip_r", "RightUpLeg", False, "The right hip joint."),
    Landmark("lm_knee_l", "LeftLeg", False, "The left knee joint, in the middle of the leg."),
    Landmark("lm_knee_r", "RightLeg", False, "The right knee joint."),
    Landmark("lm_ankle_l", "LeftFoot", False, "The left ankle joint, in the middle of the leg."),
    Landmark("lm_ankle_r", "RightFoot", False, "The right ankle joint."),
    # The hands (task #70). The middle finger gives the length and the direction of the hand, the thumb gives the turn of
    # the palm about that direction (two points and the wrist fix a hand). The other fingers follow the hand as a whole.
    Landmark("lm_finger_l", "LeftHandMiddle3", False,
             "The left middle finger, its last joint: inside the fingertip, in the middle of the finger."),
    Landmark("lm_finger_r", "RightHandMiddle3", False, "The right middle finger, its last joint."),
    Landmark("lm_thumb_l", "LeftHandThumb3", False,
             "The left thumb, its last joint: inside the tip of the thumb. With the wrist and the middle finger it "
             "says which way the palm faces."),
    Landmark("lm_thumb_r", "RightHandThumb3", False, "The right thumb, its last joint."),
)
NAMES: Tuple[str, ...] = tuple(item.name for item in LANDMARKS)
BY_NAME: Dict[str, Landmark] = {item.name: item for item in LANDMARKS}
#: ``(left, right)`` landmark names.
PAIRS: Tuple[Tuple[str, str], ...] = tuple(
    (name, name[:-1] + "r") for name in NAMES if name.endswith("_l"))

#: Where the chin is from the Arma ``head`` joint, in metres (Y up, +Z forward). The sample has no head: this is an
#: estimate from the usual proportions of an adult, not a measurement.
CHIN_FROM_HEAD = (0.0, -0.05, 0.075)
#: The height of the head (chin to the top) as a share of the height of the body, for the chin on the user's mesh.
HEAD_SHARE = 0.128

_BIN = 0.015  # height of a slice for the groin search, as a share of the body height


def _unit(v: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(v)
    return v / length if length > 1e-12 else v


# ---------------------------------------------------------------------------------------------------------------------
# Measurements of one body (the sample or the user's mesh)
# ---------------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Measure:
    """What the mesh says about itself: the sole height, the top, the midline and the crotch."""
    y0: float
    top: float
    xc: float
    zc: float
    groin_y: float
    groin: np.ndarray
    groin_found: bool
    #: The top of the shoulders: the highest point beside the neck and head.
    shoulder_y: float = 0.0

    @property
    def height(self) -> float:
        return self.top - self.y0


def find_groin(P: np.ndarray, y0: float, top: float, xc: float) -> Tuple[Optional[np.ndarray], float]:
    """The crotch: going up from the feet, the first slice where the middle of the body is filled again after the legs
    were apart. Returns ``(point on the skin or None, y)``; when the legs touch there is nothing to find."""
    height = top - y0
    gap = 0.012 * height
    band = 0.13 * height
    step = _BIN * height
    near = np.abs(P[:, 0] - xc) < band
    seen_gap = False
    y = y0 + 0.12 * height
    while y < y0 + 0.75 * height:
        slab = near & (P[:, 1] >= y) & (P[:, 1] < y + step)
        if slab.any():
            x = P[slab, 0] - xc
            filled = bool((np.abs(x) < gap).any())
            apart = bool((x < -gap).any() and (x > gap).any()) and not filled
            if apart:
                seen_gap = True
            elif filled and seen_gap:
                around = (np.abs(P[:, 0] - xc) < 2 * gap) & (np.abs(P[:, 1] - y) < 2 * step)
                z = float(P[around, 2].mean()) if around.any() else float(np.median(P[slab, 2]))
                return np.array([xc, y, z]), y
        y += step
    return None, y0 + 0.47 * height


def measure(P: np.ndarray) -> Measure:
    y0, top = float(P[:, 1].min()), float(P[:, 1].max())
    height = top - y0
    feet = P[P[:, 1] < y0 + 0.04 * height]
    xc = float((feet[:, 0].min() + feet[:, 0].max()) / 2.0)
    groin, groin_y = find_groin(P, y0, top, xc)
    found = groin is not None
    mid = P[(np.abs(P[:, 0] - xc) < 0.1 * height) & (P[:, 1] > groin_y) & (P[:, 1] < groin_y + 0.25 * height)]
    zc = float(np.median(mid[:, 2])) if len(mid) else float(np.median(P[:, 2]))
    if groin is None:
        groin = np.array([xc, groin_y, zc])
    beside = P[(np.abs(P[:, 0] - xc) > 0.1 * height) & (np.abs(P[:, 0] - xc) < 0.17 * height)
               & (P[:, 1] > groin_y + 0.2 * height)]
    shoulder_y = float(beside[:, 1].max()) if len(beside) else groin_y + 0.4 * height
    return Measure(y0, top, xc, zc, groin_y, groin, found, shoulder_y)


def facing_sign(P: np.ndarray) -> float:
    """+1 when the feet point towards +Z (the toes are in front of the shin), -1 when towards -Z."""
    y0, top = float(P[:, 1].min()), float(P[:, 1].max())
    height = top - y0
    sole = P[P[:, 1] < y0 + 0.03 * height]
    shin = P[(P[:, 1] > y0 + 0.12 * height) & (P[:, 1] < y0 + 0.22 * height)]
    if not len(sole) or not len(shin):
        return 1.0
    return 1.0 if sole[:, 2].mean() >= shin[:, 2].mean() else -1.0


# ---------------------------------------------------------------------------------------------------------------------
# Limbs
# ---------------------------------------------------------------------------------------------------------------------

def _leg_line(P: np.ndarray, side: float, m: Measure, y_low: float, y_high: float, expected_x):
    """A line through the middle of a leg: ``x(y)`` and ``z(y)`` fitted to the centres of slices between ``y_low`` and
    ``y_high``. ``expected_x(y)`` is the first guess of the leg's x (the sample leg, moved to this body); the slices keep
    only the points near it, and the fit is repeated with its own result. Returns a function ``y -> (x, z)``."""
    height = m.height
    line = (lambda y: (expected_x(y), m.zc))
    for _ in range(4):
        centres = []
        for y in np.linspace(y_low, y_high, 12):
            near = (np.abs(P[:, 1] - y) < 0.012 * height) & (side * (P[:, 0] - m.xc) > 0.012 * height) \
                & (np.abs(P[:, 0] - line(y)[0]) < 0.09 * height)
            if near.sum() >= 3:
                centres.append((y, P[near, 0].mean(), P[near, 2].mean()))
        if len(centres) < 3:
            break
        c = np.array(centres)
        fx = np.polyfit(c[:, 0], c[:, 1], 1)
        fz = np.polyfit(c[:, 0], c[:, 2], 1)
        line = (lambda y, fx=fx, fz=fz: (float(np.polyval(fx, y)), float(np.polyval(fz, y))))
    return line


def _arm_axis(P: np.ndarray, side: float, m: Measure, start: np.ndarray, end: np.ndarray, radius: float,
              low: float = 0.3, high: float = 1.3):
    """The axis of an arm part by repeated principal axis fits. ``start`` and ``end`` are the first guess; the points
    used are the ones between ``low`` and ``high`` times the length from ``start``. Returns ``(a point on the axis, unit
    direction pointing away from the body)``."""
    a = np.asarray(start, dtype=np.float64)
    length = float(np.linalg.norm(np.asarray(end, dtype=np.float64) - a))
    point, direction = a, _unit(np.asarray(end, dtype=np.float64) - a)
    clear = 0.13 * m.height  # the points of the arm are at least this far from the midline: the torso is left out
    for wide in (1.4, 1.2, 1.0, 1.0, 1.0, 1.0):  # a wide cylinder first, so an arm that is lowered is still caught
        along = (P - a) @ direction
        across = np.linalg.norm((P - a) - np.outer(along, direction), axis=1)
        # Armour sits on top of the arm (shoulder pads, a strap): points above the axis are left out. "Above" is the side
        # of the axis that faces up and out in the front view; the same rule runs on the sample, so the bias cancels.
        up = np.array([-direction[1], direction[0], 0.0]) * side
        above = (P - a) @ (up / max(np.linalg.norm(up), 1e-12))
        pick = (along > low * length) & (along < high * length) & (across < wide * radius) \
            & (side * (P[:, 0] - m.xc) > clear) & (above < 0.25 * radius)
        if pick.sum() < 12:
            break
        points = P[pick]
        point = points.mean(axis=0)
        values, vectors = np.linalg.eigh(np.cov((points - point).T))
        new = vectors[:, int(np.argmax(values))]
        direction = new if new @ direction >= 0 else -new
        a = point + direction * ((np.asarray(start, dtype=np.float64) - point) @ direction)  # the start, on the new axis
    return point, direction


def _hand_tip(P: np.ndarray, side: float, m: Measure, wrist: np.ndarray, direction: np.ndarray, length: float) -> np.ndarray:
    """The tip of the hand: the end of the mesh going from ``wrist`` along ``direction`` (about ``length`` long). Repeated
    three times with the direction to the tip found. Returns the mean of the points in the last tenth of ``length``, or
    the point ``length`` along ``direction`` when the mesh has too few points there."""
    d = _unit(np.asarray(direction, dtype=np.float64))
    tip = wrist + d * length
    clear = 0.13 * m.height  # the points of the hand are at least this far from the midline
    for _ in range(3):
        along = (P - wrist) @ d
        across = np.linalg.norm((P - wrist) - np.outer(along, d), axis=1)
        pick = (along > 0.3 * length) & (along < 1.6 * length) & (across < 0.6 * length) \
            & (side * (P[:, 0] - m.xc) > clear)
        if pick.sum() < 6:
            break
        end = pick & (along > along[pick].max() - 0.1 * length)
        tip = P[end].mean(axis=0)
        d = _unit(tip - wrist)
    return tip


# ---------------------------------------------------------------------------------------------------------------------
# Template (the sample body) and the guess
# ---------------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Template:
    """The sample body's side of the guess: where the landmarks are (``truth``), what the measurements say about the
    sample (``estimate``)."""
    truth: Dict[str, np.ndarray]
    measure: Measure
    estimate: Dict[str, np.ndarray]


def _estimate(P: np.ndarray, m: Measure, scale: float, template: Dict[str, np.ndarray], t_measure: Measure,
              known: Optional[Dict[str, np.ndarray]] = None) -> Dict[str, np.ndarray]:
    """The measured joints of one body. ``template`` holds the landmark positions of the sample, ``t_measure`` its
    measurements, ``scale`` this body's size against the sample. ``known`` holds landmarks that are placed already (by
    hand): the hand is measured from a known wrist (and elbow) instead of the guessed ones."""
    known = known or {}
    est: Dict[str, np.ndarray] = {}

    def mapped(t: np.ndarray) -> np.ndarray:
        return np.array([m.xc + scale * (t[0] - t_measure.xc), m.y0 + scale * (t[1] - t_measure.y0),
                         m.zc + scale * (t[2] - t_measure.zc)])

    for tag, side in (("l", 1.0), ("r", -1.0)):
        # legs
        hip, knee, ankle = (template["lm_%s_%s" % (j, tag)] for j in ("hip", "knee", "ankle"))
        ys = {j: float(mapped(p)[1]) for j, p in (("hip", hip), ("knee", knee), ("ankle", ankle))}
        first = mapped(hip)[0], mapped(ankle)[0]
        slope = (first[1] - first[0]) / (ys["ankle"] - ys["hip"])
        line = _leg_line(P, side, m, ys["ankle"] + 0.10 * scale, m.groin_y - 0.03 * scale,
                         lambda y, first=first, slope=slope, hip_y=ys["hip"]: first[0] + slope * (y - hip_y))
        for j in ("hip", "knee", "ankle"):
            if j == "hip":
                # The hip is far above the crotch in the Arma skeleton and legs may be apart: place it halfway between
                # the thigh (the leg line at the crotch) and the width of the pelvis at that height (gear on the hips
                # widens that), at the depth of the middle of the body.
                band = (np.abs(P[:, 1] - ys[j]) < 0.012 * m.height) & (np.abs(P[:, 0] - m.xc) < 0.17 * m.height)
                half = float(np.percentile(np.abs(P[band, 0] - m.xc), 90)) if band.sum() >= 3 else 0.1 * m.height
                thigh = line(m.groin_y - 0.03 * scale)[0]
                est["lm_hip_" + tag] = np.array([0.5 * (thigh + m.xc + side * 0.6 * half), ys[j], m.zc])
            else:
                x, z = line(ys[j])
                est["lm_%s_%s" % (j, tag)] = np.array([x, ys[j], z])
        # arms: one axis through the upper arm and forearm (the hand is left out, it often hangs down), the shoulder where
        # the axis passes the shoulder's side, the elbow and wrist at the sample's distances (scaled)
        shoulder, elbow, wrist = (template["lm_%s_%s" % (j, tag)] for j in ("shoulder", "elbow", "wrist"))
        start, end = mapped(shoulder), mapped(wrist)
        point, direction = _arm_axis(P, side, m, start, end, 0.085 * scale, 0.25, 0.85)
        axis = 0 if abs(direction[0]) >= abs(direction[1]) else 1  # the better conditioned of the two planes
        joint = point + direction * ((start[axis] - point[axis]) / direction[axis])
        est["lm_shoulder_" + tag] = joint
        est["lm_elbow_" + tag] = joint + direction * scale * float(np.linalg.norm(elbow - shoulder))
        est["lm_wrist_" + tag] = joint + direction * scale * float(np.linalg.norm(wrist - shoulder))
        # hand: the tip of the middle finger is measured on the mesh; the thumb is the sample's, turned with the hand
        wrist_here = np.asarray(known.get("lm_wrist_" + tag, est["lm_wrist_" + tag]), dtype=np.float64)
        if "lm_wrist_" + tag in known and "lm_elbow_" + tag in known:
            direction = _unit(wrist_here - np.asarray(known["lm_elbow_" + tag], dtype=np.float64))
        finger, thumb = template["lm_finger_" + tag], template["lm_thumb_" + tag]
        sample_axis = _unit(finger - wrist)
        arm_turn = swing(_unit(wrist - shoulder), direction)
        tip = _hand_tip(P, side, m, wrist_here, arm_turn @ sample_axis, scale * float(np.linalg.norm(finger - wrist)))
        est["lm_finger_" + tag] = tip
        est["lm_thumb_" + tag] = wrist_here + scale * (swing(sample_axis, _unit(tip - wrist_here)) @ (thumb - wrist))
    return est


def make_template(joints: Dict[str, np.ndarray], body: np.ndarray) -> Template:
    """The sample side. ``joints`` maps lower-case Arma joint names to positions, ``body`` is the sample's skin points
    (only points that belong to polygons). Needs the joints of the landmarks and ``head`` (for the chin)."""
    truth: Dict[str, np.ndarray] = {}
    for item in LANDMARKS:
        if item.bone:
            if item.bone.lower() not in joints:
                raise ValueError("the skeleton has no joint %s" % item.bone)
            truth[item.name] = np.asarray(joints[item.bone.lower()], dtype=np.float64)
    if "head" not in joints:
        raise ValueError("the skeleton has no joint head")
    m = measure(body)
    truth["lm_groin"] = m.groin
    truth["lm_chin"] = np.asarray(joints["head"], dtype=np.float64) + np.array(CHIN_FROM_HEAD)
    return Template(truth, m, _estimate(body, m, 1.0, truth, m))


def guess_landmarks(P: np.ndarray, template: Template, known: Optional[Dict[str, np.ndarray]] = None
                    ) -> Tuple[Dict[str, np.ndarray], Dict[str, str]]:
    """Guess all landmarks on the user's points ``P``. Returns ``(positions, notes)``; ``notes`` maps a landmark name to
    a short text when the guess is weaker than usual (for example the crotch was not found). ``known`` are landmarks that
    are placed already: the hand landmarks are guessed from a known wrist and elbow (no other guess is changed)."""
    m = measure(P)
    t = template.measure
    notes: Dict[str, str] = {}
    # The size against the sample: the crotch height is pushed up when the legs are apart, the shoulder height is not
    # affected by the arms or legs. The two are averaged.
    scale = float(np.sqrt((m.groin_y - m.y0) / (t.groin_y - t.y0) * (m.shoulder_y - m.y0) / (t.shoulder_y - t.y0)))
    est = _estimate(P, m, scale, template.truth, t, known)
    out: Dict[str, np.ndarray] = {}
    for name, value in est.items():
        out[name] = value + scale * (template.truth[name] - template.estimate[name])
    out["lm_groin"] = m.groin
    if not m.groin_found:
        notes["lm_groin"] = "the legs touch, so the crotch was placed at 47 % of the height"
    # chin: the head is HEAD_SHARE of the height; the front of the face at that height
    height = m.height
    y_chin = m.top - HEAD_SHARE * height
    face = P[(np.abs(P[:, 1] - y_chin) < 0.02 * height) & (np.abs(P[:, 0] - m.xc) < 0.03 * height)]
    z_chin = float(face[:, 2].max()) if len(face) else m.zc
    out["lm_chin"] = np.array([m.xc, y_chin, z_chin])
    return out, notes


# ---------------------------------------------------------------------------------------------------------------------
# Symmetry and snapping
# ---------------------------------------------------------------------------------------------------------------------

def symmetrize(points: Dict[str, np.ndarray], given: Sequence[str]) -> Dict[str, np.ndarray]:
    """Make left and right match about the mirror plane (the plane ``x = mid`` through the middle of the pairs). A pair
    of which both were ``given`` (from Rig Landmarks) is averaged; when only one was given, the other is its mirror image; a
    pair of two guesses is averaged. Points on the skin in the middle (chin, groin) move onto the plane."""
    out = {name: np.array(value, dtype=np.float64) for name, value in points.items()}
    mids = [(out[a][0] + out[b][0]) / 2.0 for a, b in PAIRS if a in out and b in out]
    centre = [out[name][0] for name in ("lm_chin", "lm_groin") if name in out and name in given]
    mid = float(np.mean(mids + centre)) if (mids or centre) else 0.0
    mirror = np.array([-1.0, 1.0, 1.0])
    shift = np.array([2.0 * mid, 0.0, 0.0])
    for a, b in PAIRS:
        if a not in out or b not in out:
            continue
        a_given, b_given = a in given, b in given
        if a_given and not b_given:
            out[b] = out[a] * mirror + shift
        elif b_given and not a_given:
            out[a] = out[b] * mirror + shift
        else:
            left = (out[a] + out[b] * mirror + shift) / 2.0
            out[a], out[b] = left, left * mirror + shift
    for name in ("lm_chin", "lm_groin"):
        if name in out:
            out[name][0] = mid
    return out


def is_set(value) -> bool:
    """A hand-placed landmark is "not set" while all three numbers are exactly zero (the default of the Rig Landmarks
    fields). A landmark never really sits exactly on the world origin, so the zero vector can stand for "none"."""
    return any(float(v) != 0.0 for v in value)


def mirror_x(value) -> Tuple[float, float, float]:
    """The mirror image about the X = 0 plane (Left is +X)."""
    return (-float(value[0]), float(value[1]), float(value[2]))


def follow_mirror(left, right) -> Tuple[tuple, tuple]:
    """``(left, right)`` with the pair made mirror images. The left one rules; when only the right one is set, the left
    one follows it. A pair of two unset landmarks stays unset."""
    left, right = tuple(float(v) for v in left), tuple(float(v) for v in right)
    if is_set(left):
        return left, mirror_x(left)
    if is_set(right):
        return mirror_x(right), right
    return left, right


def snap_to_surface(points: Dict[str, np.ndarray], P: np.ndarray, tris: np.ndarray) -> Dict[str, np.ndarray]:
    """Move the landmarks that lie on the skin (chin, groin) onto the closest point of the surface."""
    out = dict(points)
    names = [name for name in points if BY_NAME[name].surface]
    if not names or not len(tris):
        return out
    query = np.array([points[name] for name in names])
    tri, bary, _ = closest_points(query, P, tris)
    corner = tris[tri]
    at = (bary[:, :, None] * P[corner]).sum(1)
    for name, value in zip(names, at):
        out[name] = value
    return out

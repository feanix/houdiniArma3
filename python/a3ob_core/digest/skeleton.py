# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the measures follow the landmark joints of a3ob_core.rig.landmarks (docs/design/digests.md).
"""The skeleton and skeleton-compare digests, with no Houdini (task #81).

A skeleton here is ``Skel``: joint names, parent numbers and positions in metres (what ``rig_weights.read_skeleton`` reads).
Every figure is printed in centimetres or degrees beside its reference value (the Arma rest skeleton for ``skeleton``, the
other skeleton for ``skeleton-compare``). Nothing is judged.

The body measures use the Arma joints of the landmarks (``a3ob_core.rig.BONE_OF``): the hips are ``LeftUpLeg`` and
``RightUpLeg``, the shoulders ``LeftArm`` and ``RightArm``, the elbow joint is ``LeftForeArm``, the knee joint ``LeftLeg``.
"""

import dataclasses
from typing import Dict, List, Optional, Tuple

import numpy as np

from a3ob_core.rig import BONE_OF
from a3ob_core.rig.fit import MIN_BONE as MIN_BONE_LENGTH

from .page import NR, Page, clip, cm, deg, num, percentiles, row

__all__ = ["Skel", "Source", "bone_lengths", "bone_vectors", "measure_values", "MEASURES", "CHAINS", "skeleton_page",
           "compare_page", "joint_offsets", "direction_differences", "angle_at"]


@dataclasses.dataclass
class Skel:
    names: List[str]
    parents: List[int]
    positions: np.ndarray  # (n, 3) metres
    #: (n, 3, 3) joint frames, column-vector form (columns = the x, y, z axes), or None when the points have no transform.
    frames: Optional[np.ndarray] = None

    def __post_init__(self):
        self.positions = np.asarray(self.positions, dtype=np.float64).reshape(-1, 3)
        if self.frames is not None:
            self.frames = np.asarray(self.frames, dtype=np.float64).reshape(-1, 3, 3)

    def index(self) -> Dict[str, int]:
        """Lower-case joint name -> joint number (the first of two joints with the same name ignoring case)."""
        found: Dict[str, int] = {}
        for number, name in enumerate(self.names):
            found.setdefault(name.lower(), number)
        return found


@dataclasses.dataclass
class Source:
    """What a page says it read: the scene file, the node path and the part (empty for the whole output)."""
    scene: str
    node: str
    part: str = ""

    def line(self, label: str = "") -> str:
        tail = "; " + ("part " + self.part if self.part else "whole output")
        return "%s%s%s" % (label, clip(self.node, 80 - len(label) - len(tail)), tail)

    def scene_name(self) -> str:
        return clip(self.scene, 40)


def bone_vectors(skel: Skel) -> np.ndarray:
    """The vector from the parent to each joint (a root has NaN)."""
    out = np.full((len(skel.names), 3), np.nan)
    for joint, parent in enumerate(skel.parents):
        if parent >= 0:
            out[joint] = skel.positions[joint] - skel.positions[parent]
    return out


def bone_lengths(skel: Skel) -> np.ndarray:
    """The length of each bone in metres (the distance to the parent; NaN for a root)."""
    return np.linalg.norm(bone_vectors(skel), axis=1)


def angle_at(apex: np.ndarray, a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """The angle in degrees at ``apex`` between the directions to ``a`` and ``b`` (180 is straight); None for a zero arm."""
    u, v = np.asarray(a) - apex, np.asarray(b) - apex
    nu, nv = np.linalg.norm(u), np.linalg.norm(v)
    if nu < 1e-9 or nv < 1e-9:
        return None
    return float(np.degrees(np.arccos(np.clip(u @ v / (nu * nv), -1.0, 1.0))))


def _joint(skel: Skel, bone: str) -> Optional[np.ndarray]:
    found = skel.index().get(bone.lower())
    return None if found is None else skel.positions[found]


def _dist(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    return None if a is None or b is None else float(np.linalg.norm(a - b))


def _hip_middle(skel: Skel) -> Optional[np.ndarray]:
    left, right = _joint(skel, BONE_OF["lm_hip_l"]), _joint(skel, BONE_OF["lm_hip_r"])
    return None if left is None or right is None else (left + right) / 2.0


def _angle(skel: Skel, apex: str, a: str, b: str) -> Optional[float]:
    p, q, r = _joint(skel, BONE_OF[apex]), _joint(skel, BONE_OF[a]), _joint(skel, BONE_OF[b])
    return None if p is None or q is None or r is None else angle_at(p, q, r)


#: ``(key, label, unit)``; the unit is ``cm`` (stored in metres) or ``deg``.
MEASURES: Tuple[Tuple[str, str, str], ...] = (
    ("pelvis_hips", "pelvis-to-hips", "cm"), ("pelvis_head", "pelvis-to-head", "cm"),
    ("elbow_l", "elbow angle L", "deg"), ("elbow_r", "elbow angle R", "deg"),
    ("knee_l", "knee angle L", "deg"), ("knee_r", "knee angle R", "deg"),
    ("hip_width", "hip width", "cm"), ("shoulder_width", "shoulder width", "cm"),
)


def measure_values(skel: Skel) -> Dict[str, Optional[float]]:
    """The body measures of ``MEASURES``: metres or degrees, None when the skeleton lacks a joint the measure needs.

    pelvis-to-hips is the distance from the Pelvis joint to the middle of the two hip joints; pelvis-to-head the distance
    from the Pelvis to the head joint; an elbow (knee) angle is the angle at the elbow (knee) joint between the shoulder
    (hip) and the wrist (ankle), 180 straight; the widths are the distances between the two hip and the two shoulder joints."""
    pelvis = _joint(skel, "Pelvis")
    return {
        "pelvis_hips": _dist(pelvis, _hip_middle(skel)),
        "pelvis_head": _dist(pelvis, _joint(skel, "head")),
        "elbow_l": _angle(skel, "lm_elbow_l", "lm_shoulder_l", "lm_wrist_l"),
        "elbow_r": _angle(skel, "lm_elbow_r", "lm_shoulder_r", "lm_wrist_r"),
        "knee_l": _angle(skel, "lm_knee_l", "lm_hip_l", "lm_ankle_l"),
        "knee_r": _angle(skel, "lm_knee_r", "lm_hip_r", "lm_ankle_r"),
        "hip_width": _dist(_joint(skel, BONE_OF["lm_hip_l"]), _joint(skel, BONE_OF["lm_hip_r"])),
        "shoulder_width": _dist(_joint(skel, BONE_OF["lm_shoulder_l"]), _joint(skel, BONE_OF["lm_shoulder_r"])),
    }


def _fmt(value: Optional[float], unit: str) -> str:
    return deg(value) if unit == "deg" else cm(value)


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else b - a


# ---- matching two skeletons by joint name ------------------------------------------------------------------------------

def _matched(a: Skel, b: Skel) -> List[Tuple[int, int]]:
    """``(joint in a, joint in b)`` for each name the two share (ignoring case), in the order of ``a``."""
    other = b.index()
    return [(i, other[name.lower()]) for i, name in enumerate(a.names) if name.lower() in other]


def joint_offsets(a: Skel, b: Skel) -> Tuple[List[str], np.ndarray]:
    """``(names, offsets)``: the distance in metres between the two positions of every joint both skeletons have."""
    pairs = _matched(a, b)
    return [a.names[i] for i, _ in pairs], np.array([np.linalg.norm(b.positions[j] - a.positions[i]) for i, j in pairs])


def direction_differences(a: Skel, b: Skel) -> Tuple[List[str], np.ndarray]:
    """``(names, degrees)``: the angle between the bone of a joint in ``a`` and in ``b`` (NaN when either bone is a root or
    shorter than ``MIN_BONE_LENGTH``, which decides no direction)."""
    pairs = _matched(a, b)
    va, vb = bone_vectors(a), bone_vectors(b)
    out = np.full(len(pairs), np.nan)
    for k, (i, j) in enumerate(pairs):
        la, lb = np.linalg.norm(va[i]), np.linalg.norm(vb[j])
        if np.isfinite(la) and np.isfinite(lb) and la > MIN_BONE_LENGTH and lb > MIN_BONE_LENGTH:
            out[k] = np.degrees(np.arccos(np.clip(va[i] @ vb[j] / (la * lb), -1.0, 1.0)))
    return [a.names[i] for i, _ in pairs], out


# ---- the skeleton digest -----------------------------------------------------------------------------------------------

#: The body chains of the page, from the root down. A chain is a name for the line and its joints.
CHAINS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("spine", ("Pelvis", "Spine", "Spine1", "Spine2", "Spine3", "neck", "neck1", "head")),
    ("left arm", ("LeftShoulder", "LeftArm", "LeftArmRoll", "LeftForeArm", "LeftForeArmRoll", "LeftHand")),
    ("right arm", ("RightShoulder", "RightArm", "RightArmRoll", "RightForeArm", "RightForeArmRoll", "RightHand")),
    ("left leg", ("LeftUpLeg", "LeftUpLegRoll", "LeftLeg", "LeftLegRoll", "LeftFoot", "LeftToeBase")),
    ("right leg", ("RightUpLeg", "RightUpLegRoll", "RightLeg", "RightLegRoll", "RightFoot", "RightToeBase")),
)


def _roots(skel: Skel) -> int:
    return sum(1 for p in skel.parents if p < 0)


def skeleton_page(skel: Skel, rest: Optional[Skel], source: Source, rest_source: str = "", show_all: bool = False) -> Page:
    """The ``skeleton`` digest: joints, parents and bone lengths beside the Arma rest skeleton ``rest`` (None: not given)."""
    lengths = bone_lengths(skel)
    total = float(np.nansum(lengths))
    rest_len = {}
    missing: List[str] = []
    rest_total = None
    if rest is not None:
        r_len = bone_lengths(rest)
        rest_len = {name.lower(): r_len[k] for k, name in enumerate(rest.names)}
        shared = [k for k, name in enumerate(skel.names) if name.lower() in rest_len]
        rest_total = float(np.nansum([rest_len[skel.names[k].lower()] for k in shared]))
        total = float(np.nansum(lengths[shared]))  # like with like: the joints the rest has too
    else:
        missing.append("the Arma rest skeleton (give one with --rest <Rig Skeleton node>)")
    summary = "skeleton: %d joints, %d root%s; bone lengths sum %s cm" % (
        len(skel.names), _roots(skel), "" if _roots(skel) == 1 else "s", cm(total))
    if rest is not None:
        summary += " (Arma rest %s cm)" % cm(rest_total)
    context = ["scene: " + source.scene_name(), source.line("node: "), "Arma rest: " + (rest_source if rest is not None else NR)]
    body: List[str] = []
    index = skel.index()
    if show_all:
        widths = (16, 16, 7, 7, 6)
        body.append(row(("joint", "parent", "bone cm", "Arma cm", "ratio"), widths, 2))
        for k, name in enumerate(skel.names):
            reference = rest_len.get(name.lower())
            ratio = None if reference is None or not reference > 1e-9 or not np.isfinite(lengths[k]) else lengths[k] / reference
            body.append(row((name, skel.names[skel.parents[k]] if skel.parents[k] >= 0 else "(root)", cm(lengths[k]),
                             cm(reference), num(ratio, 2)), widths, 2))
        return Page(summary, context, body, missing, "skeleton-compare A B; weights; landmarks")
    shown = set()
    body.append("chains, each joint under its parent; bone cm as this/Arma rest:")
    for label, bones in CHAINS:
        present = [b for b in bones if b.lower() in index]
        if not present:
            continue
        side = "Left" if label.startswith("left") else "Right" if label.startswith("right") else ""
        names, values = [], []
        previous = None
        for bone in present:
            k = index[bone.lower()]
            shown.add(k)
            parent = skel.parents[k]
            text = bone[len(side):] if side and bone.startswith(side) else bone
            if previous is None:
                start = " from " + skel.names[parent] if parent >= 0 else ""
            elif parent != previous:  # any parent other than the one before it in the chain is named
                text += "<" + (skel.names[parent] if parent >= 0 else "root")
            names.append(text)
            previous = k
            reference = rest_len.get(bone.lower())
            if parent < 0:
                values.append("root")
            else:
                values.append("%s/%s" % (cm(lengths[k]), cm(reference)) if rest is not None else cm(lengths[k]))
        body.append("%s%s: %s" % (label, start, " > ".join(names)))
        body.append("  " + " ".join(values))
    others = [k for k in range(len(skel.names)) if k not in shown]
    if others:
        mine = lengths[others]
        theirs = np.array([rest_len.get(skel.names[k].lower(), np.nan) for k in others])
        def spread(values):
            good = np.isfinite(values)
            return "%s/%s" % (cm(np.median(values[good])), cm(values[good].max())) if good.any() else NR

        body.append("other %d joints (fingers...): bone cm median/longest %s; Arma %s" % (
            len(others), spread(mine), spread(theirs)))
    if rest is not None:
        absent = [name for name in skel.names if name.lower() not in rest_len]
        if absent:
            body.append("joints the Arma rest does not have: %s" % ", ".join(absent[:6]) + (" ..." if len(absent) > 6 else ""))
    return Page(summary, context, body, missing, "skeleton A --all (every joint); skeleton-compare A B; landmarks")


# ---- the skeleton-compare digest ---------------------------------------------------------------------------------------

def _stat_row(label: str, names: List[str], values: np.ndarray, unit: str, widths) -> str:
    finite = np.isfinite(values)
    if not finite.any():
        return row((label, NR, NR, NR, NR), widths, 1)
    low, mid, high = percentiles(values, (50, 99, 100))
    at = names[int(np.nanargmax(np.abs(values)))]
    digits = 1
    fmt = (lambda v: cm(v, digits)) if unit == "cm" else (lambda v: deg(v, digits))
    return row((label, fmt(low), fmt(mid), fmt(high), at), widths, 1)


def compare_page(a: Skel, b: Skel, source_a: Source, source_b: Source, show_all: bool = False) -> Page:
    """The ``skeleton-compare`` digest: how skeleton ``b`` differs from skeleton ``a``, joint by joint and in body measures."""
    names, offsets = joint_offsets(a, b)
    pairs = _matched(a, b)
    moves = np.array([b.positions[j] - a.positions[i] for i, j in pairs]).reshape(-1, 3)
    shift = moves.mean(axis=0) if len(moves) else None
    shifted = np.linalg.norm(moves - shift, axis=1) if len(moves) else offsets  # the offset left once the mean move is removed
    len_a, len_b = bone_lengths(a), bone_lengths(b)
    dlen = np.array([len_b[j] - len_a[i] for i, j in pairs])
    dnames, dirs = direction_differences(a, b)
    missing: List[str] = []
    if len(names) < len(a.names) or len(names) < len(b.names):
        missing.append("%d joints of A and %d of B have no partner by name" % (len(a.names) - len(names), len(b.names) - len(names)))
    if not len(names):
        missing.append("no joint name is in both skeletons")
    unaimed = int(np.isnan(dirs).sum())
    if unaimed:
        missing.append("bone direction of %d roots or zero-length bones" % unaimed)
    top = names[int(np.argmax(offsets))] if len(names) else None
    summary = "skeleton-compare: %d joints; offset median %s cm, max %s cm (%s)" % (
        len(names), cm(float(np.median(offsets))) if len(names) else NR, cm(float(offsets.max())) if len(names) else NR,
        top or NR)
    context = ["scene: %s; B minus A; cm and degrees" % source_a.scene_name() if source_a.scene == source_b.scene else
               "scene: A %s, B %s" % (source_a.scene_name(), source_b.scene_name()), source_a.line("A: "),
               source_b.line("B: ")]
    widths = (21, 7, 7, 7, 16)
    body: List[str] = []
    if shift is not None:
        body.append("mean move of the joints, B minus A: x %s, y %s, z %s cm" % tuple(cm(v) for v in shift))
    if show_all:
        body.append(row(("joint", "off cm", "len d cm", "dir deg", ""), (16, 8, 8, 8, 0), 1))
        for k, name in enumerate(names):
            body.append(row((name, cm(offsets[k]), cm(dlen[k]), deg(dirs[k]) if np.isfinite(dirs[k]) else NR, ""),
                            (16, 8, 8, 8, 0), 1))
    else:
        body.append(row(("per joint", "median", "p99", "max", "at (max)"), widths, 1))
        body.append(_stat_row("position offset cm", names, offsets, "cm", widths))
        body.append(_stat_row("  less mean shift cm", names, shifted, "cm", widths))
        body.append(_stat_row("bone length diff cm", names, np.abs(dlen), "cm", widths))
        body.append(_stat_row("bone direction deg", dnames, dirs, "deg", widths))
    ma, mb = measure_values(a), measure_values(b)
    mwidths = (21, 9, 9, 9)
    body.append(row(("body measure", "A", "B", "B minus A"), mwidths, 1))
    for key, label, unit in MEASURES:
        body.append(row(("%s %s" % (label, unit), _fmt(ma[key], unit), _fmt(mb[key], unit), _fmt(_diff(ma[key], mb[key]), unit)),
                        mwidths, 1))
    gone = [label for key, label, _ in MEASURES if ma[key] is None or mb[key] is None]
    if gone:
        missing.append("%s (a joint it needs is missing in A or B)" % ", ".join(gone))
    return Page(summary, context, body, missing, "skeleton A; skeleton B; deform-compare A B; landmarks")

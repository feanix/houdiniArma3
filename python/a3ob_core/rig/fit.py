# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting. The rigid fit down the hierarchy is the one of docs/design/character-rigging.md 1.5 and
# 6.2; the rotation between two sets of vectors is Kabsch's (SVD) solution.
"""Fit the Arma skeleton and the sample body to the landmarks of a user's mesh, with no Houdini (task #66).

The Arma skeleton is never changed (D37 Q7). The fit makes a working copy, ``A'``, and poses the sample body with it.

- **Targets.** Each landmark that is a joint is a target for that joint. The joints of the torso and head have no landmark
  of their own: they come from a torso frame (the hips and shoulders) and the chin, mapped from the sample to the user.
- **Rigid fit down the hierarchy.** Going from the pelvis down, each joint is turned so that the vectors to the target
  joints below it point where the targets are, and scaled to their distance. A joint with one target below it is swung the
  shortest way from the rotation it inherits (its twist stays); with several (the pelvis, the upper spine) the best
  rotation for all of them is found. A joint with no target below it keeps the rotation and scale of its parent.
- **Scale.** ``bone``: every joint takes its own scale and a target joint is placed on its landmark (the bone gets the
  fitted length). ``body``: one scale for all, the joints are not moved onto the landmarks.
- **Quality.** For each target joint, how far the rigid fit left it from its landmark (the distance before a joint is
  moved onto the landmark). In ``body`` mode that is the real distance.

Rotations are 3x3 column-vector matrices here; the Houdini node writes the transposed (row-vector) form to ``transform``.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence

import numpy as np

__all__ = ["TORSO_BONES", "FitResult", "joint_targets", "fit_skeleton", "skin_points", "body_scale", "swing"]

#: The torso joints that are placed from the torso frame, from the pelvis up to the neck.
TORSO_BONES = ("Pelvis", "Spine", "Spine1", "Spine2", "Spine3", "neck")
#: Joints shorter than this from their parent (metres) do not decide a direction.
MIN_BONE = 1e-3


@dataclasses.dataclass
class FitResult:
    positions: np.ndarray  # fitted joint positions (n, 3)
    linear: np.ndarray  # (n, 3, 3): rotation times scale, column-vector form: x' = q + L (x - p)
    scales: np.ndarray  # (n,)
    #: Joint number -> distance (metres) the rigid fit left between the joint and its target (before it was moved).
    residual: Dict[int, float]
    targets: Dict[int, np.ndarray]


def swing(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The rotation that turns direction ``a`` onto direction ``b`` by the shortest way (no twist about the axis)."""
    a = a / max(np.linalg.norm(a), 1e-20)
    b = b / max(np.linalg.norm(b), 1e-20)
    cos = float(np.clip(a @ b, -1.0, 1.0))
    axis = np.cross(a, b)
    sin = float(np.linalg.norm(axis))
    if sin < 1e-9:
        if cos > 0:
            return np.eye(3)
        helper = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, helper)
        axis /= np.linalg.norm(axis)
        return 2.0 * np.outer(axis, axis) - np.eye(3)
    axis = axis / sin
    k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    return np.eye(3) + sin * k + (1.0 - cos) * (k @ k)


def _kabsch(v: np.ndarray, w: np.ndarray) -> Optional[np.ndarray]:
    """The rotation R that best maps the rows of ``v`` onto the rows of ``w`` (least squares), or None when the vectors
    are all along one line (the rotation about it is then not decided)."""
    h = v.T @ w
    u, s, vt = np.linalg.svd(h)
    if s[1] < 1e-3 * max(s[0], 1e-20):
        return None
    d = np.sign(np.linalg.det(vt.T @ u.T))
    return vt.T @ np.diag([1.0, 1.0, d if d != 0 else 1.0]) @ u.T


def body_scale(user: Dict[str, np.ndarray], sample: Dict[str, np.ndarray]) -> float:
    """One size ratio from all the landmarks both sides have: the spread of the user's points over the spread of the
    sample's."""
    names = [n for n in user if n in sample]
    u = np.array([user[n] for n in names])
    s = np.array([sample[n] for n in names])
    spread_s = float(((s - s.mean(0)) ** 2).sum())
    return float(np.sqrt(((u - u.mean(0)) ** 2).sum() / spread_s)) if spread_s > 0 else 1.0


def _frame(hips: Sequence[np.ndarray], shoulders: Sequence[np.ndarray]):
    """Origin and axes (columns x = left, y = up, z = forward) of the torso from the hips and shoulders."""
    origin = (hips[0] + hips[1]) / 2.0
    ex = hips[0] - hips[1]
    ex = ex / max(np.linalg.norm(ex), 1e-20)
    up = (shoulders[0] + shoulders[1]) / 2.0 - origin
    up = up - (up @ ex) * ex
    ey = up / max(np.linalg.norm(up), 1e-20)
    ez = np.cross(ex, ey)
    return origin, np.stack([ex, ey, ez], axis=1)


def _share_places(rest: np.ndarray, targets: Dict[int, np.ndarray]) -> None:
    """Joints that share a place in the rest (a zero-length bone, as Pelvis and Spine in the Arma skeleton) share one
    target: the average of their targets. Else the bone between them would get a length and a direction from noise."""
    done = set()
    for a in sorted(targets):
        if a in done:
            continue
        group = [b for b in sorted(targets) if np.linalg.norm(rest[b] - rest[a]) <= MIN_BONE]
        done.update(group)
        if len(group) > 1:
            mean = np.mean([targets[b] for b in group], axis=0)
            for b in group:
                targets[b] = mean


def joint_targets(bone_index: Dict[str, int], rest: np.ndarray, user: Dict[str, np.ndarray],
                  sample: Dict[str, np.ndarray], bones: Dict[str, str]) -> Dict[int, np.ndarray]:
    """The target position of every joint that has one. ``bone_index`` maps lower-case joint names to joint numbers,
    ``rest`` is the Arma rest positions, ``user`` and ``sample`` map landmark names to positions, ``bones`` maps a
    landmark name to its Arma joint name (``landmarks.LANDMARKS``). Raises ValueError when the torso cannot be framed."""
    targets: Dict[int, np.ndarray] = {}
    for name, bone in bones.items():
        if bone and name in user and bone.lower() in bone_index:
            targets[bone_index[bone.lower()]] = np.asarray(user[name], dtype=np.float64)

    def has(*names: str) -> bool:
        return all(n in user and n in sample for n in names)

    if not has("lm_hip_l", "lm_hip_r", "lm_shoulder_l", "lm_shoulder_r"):
        return targets
    hips_u = (user["lm_hip_l"], user["lm_hip_r"])
    sho_u = (user["lm_shoulder_l"], user["lm_shoulder_r"])
    hips_s = (sample["lm_hip_l"], sample["lm_hip_r"])
    sho_s = (sample["lm_shoulder_l"], sample["lm_shoulder_r"])
    if np.linalg.norm(hips_u[0] - hips_u[1]) < 1e-4 or np.linalg.norm(hips_s[0] - hips_s[1]) < 1e-4:
        raise ValueError("the two hip landmarks are in the same place")
    o_u, e_u = _frame(hips_u, sho_u)
    o_s, e_s = _frame(hips_s, sho_s)
    kx = 0.5 * (np.linalg.norm(hips_u[0] - hips_u[1]) / np.linalg.norm(hips_s[0] - hips_s[1])
                + np.linalg.norm(sho_u[0] - sho_u[1]) / max(np.linalg.norm(sho_s[0] - sho_s[1]), 1e-9))
    height_u = float((((sho_u[0] + sho_u[1]) / 2.0 - o_u) @ e_u[:, 1]))
    height_s = float((((sho_s[0] + sho_s[1]) / 2.0 - o_s) @ e_s[:, 1]))
    ky = height_u / max(height_s, 1e-9)
    linear = e_u @ np.diag([kx, ky, float(np.sqrt(kx * ky))]) @ e_s.T  # sample directions -> user directions

    def torso(p: np.ndarray) -> np.ndarray:
        return o_u + linear @ (p - o_s)

    for bone in TORSO_BONES:
        if bone.lower() in bone_index:
            targets[bone_index[bone.lower()]] = torso(rest[bone_index[bone.lower()]])
    if has("lm_groin") and "pelvis" in bone_index:  # the pelvis: the average of the torso frame and the crotch
        number = bone_index["pelvis"]
        shift = 0.5 * (user["lm_groin"] + linear @ (rest[number] - sample["lm_groin"]) - targets[number])
        for bone in TORSO_BONES:  # the bones in the pelvis' place (Spine) take the same shift, or the chain kinks
            joint = bone_index.get(bone.lower())
            if joint is not None and np.linalg.norm(rest[joint] - rest[number]) <= MIN_BONE:
                targets[joint] = targets[joint] + shift
    _share_places(rest, targets)
    if has("lm_chin") and "head" in bone_index and "neck" in bone_index:
        head, neck = bone_index["head"], bone_index["neck"]
        targets[head] = user["lm_chin"] + linear @ (rest[head] - sample["lm_chin"])
        if "neck1" in bone_index:
            mid = bone_index["neck1"]
            along = rest[head] - rest[neck]
            share = float((rest[mid] - rest[neck]) @ along) / max(float(along @ along), 1e-12)
            targets[mid] = targets[neck] + share * (targets[head] - targets[neck])
    return targets


def _nearest_targets(joint: int, children: List[List[int]], rest: np.ndarray, targets: Dict[int, np.ndarray]) -> List[int]:
    """The target joints below ``joint``: going down, the first target on each branch that is not in the same place as
    ``joint`` (a zero-length bone does not decide a direction; the walk goes on below it)."""
    found: List[int] = []
    stack = list(children[joint])
    while stack:
        c = stack.pop()
        if c in targets and np.linalg.norm(rest[c] - rest[joint]) > MIN_BONE:
            found.append(c)
        else:
            stack.extend(children[c])
    return sorted(found)


def fit_skeleton(parents: Sequence[int], rest: np.ndarray, targets: Dict[int, np.ndarray], mode: str = "bone",
                 size: float = 1.0) -> FitResult:
    """Fit the skeleton (``rest`` positions, ``parents`` with parents before children) to ``targets`` (joint number ->
    position). ``mode`` is ``"bone"`` or ``"body"``; ``size`` is the one scale of ``body`` mode."""
    count = len(parents)
    children: List[List[int]] = [[] for _ in range(count)]
    for joint, parent in enumerate(parents):
        if parent >= 0:
            children[parent].append(joint)
    pos = np.zeros((count, 3))
    rot = np.tile(np.eye(3), (count, 1, 1))
    scale = np.ones(count)
    residual: Dict[int, float] = {}
    body = mode == "body"
    for a in range(count):
        parent = parents[a]
        if parent < 0:
            pos[a] = targets.get(a, rest[a])
            r_in, s_in = np.eye(3), size if body else 1.0
        else:
            predicted = pos[parent] + scale[parent] * rot[parent] @ (rest[a] - rest[parent])
            if a in targets:
                residual[a] = float(np.linalg.norm(predicted - targets[a]))
            pos[a] = targets[a] if (a in targets and not body) else predicted
            r_in, s_in = rot[parent], scale[parent]
        keys = _nearest_targets(a, children, rest, targets)
        if not keys:
            rot[a], scale[a] = r_in, s_in
            continue
        v = np.array([r_in @ (rest[k] - rest[a]) for k in keys])
        w = np.array([targets[k] - pos[a] for k in keys])
        delta = _kabsch(v, w) if len(keys) > 1 else None
        if delta is None:
            delta = swing(v.sum(axis=0), w.sum(axis=0))
        rot[a] = delta @ r_in
        if body:
            scale[a] = size
        else:
            turned = v @ delta.T
            scale[a] = max(float((w * turned).sum() / max((v * v).sum(), 1e-20)), 1e-3)
    linear = scale[:, None, None] * rot
    return FitResult(pos, linear, scale, residual, {k: np.asarray(t) for k, t in targets.items()})


def skin_points(points: np.ndarray, weights: np.ndarray, rest: np.ndarray, result: FitResult) -> np.ndarray:
    """Move ``points`` with linear blend skinning: each joint moves a point from the Arma rest to the fitted pose by
    ``x' = q + L (x - p)``. Weights are scaled to a sum of 1; a point with no weight takes the matrix of the closest
    point that has some."""
    total = weights.sum(axis=1)
    weighted = total > 1e-9
    w = np.zeros_like(weights)
    w[weighted] = weights[weighted] / total[weighted, None]
    if not weighted.all() and weighted.any():
        index = np.nonzero(weighted)[0]
        for start in range(0, int((~weighted).sum()), 256):
            lost = np.nonzero(~weighted)[0][start:start + 256]
            d = ((points[lost, None, :] - points[None, index, :]) ** 2).sum(axis=2)
            w[lost] = w[index[d.argmin(axis=1)]]
    shift = result.positions - np.einsum("jab,jb->ja", result.linear, rest)  # q - L p
    blended = np.einsum("nj,jab->nab", w, result.linear)
    return np.einsum("nab,nb->na", blended, points) + w @ shift

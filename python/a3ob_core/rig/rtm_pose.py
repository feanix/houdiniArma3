# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The meaning of an RTM matrix follows A3OB io/import_rtm.py (``import_keyframes``, v2.5.1, 9f2b8bc); the code is our own.
"""Poses from an RTM, with no Houdini (character-rigging.md, Phase 4, tests 4.2 and 4.3).

**What an RTM matrix is.** The matrix of a bone is the move of that bone from the rest pose to the pose, absolute, in model
space. A vertex with the whole weight on one bone lands at ``matrix * vertex``; the rest pose is the identity. A3OB shows
this in ``import_keyframes``: the Blender pose of a bone is ``matrix * rest``, so the *skin* matrix, pose times the inverse
rest, is the RTM matrix. It does not depend on how the bones' own axes are turned.

**What the KineFX skeleton needs.** Joint Deform skins with ``rest^-1 * joint`` (row vectors), where ``rest`` is the
joint's rest frame written into the capture (``weights.inverse_rest_floats``, which includes the frame's rotation) and
``joint`` its transform in the animated skeleton. So ``joint = rest * skin``: ``pose_world`` multiplies the RTM matrix onto
whatever rest frames the skeleton has. With any rest frames the skinned mesh is the same, and equals the linear blend
skinning of the RTM matrices applied to the vertices directly (``blend``).

Axes: the matrices are turned into Houdini's axes with the data model map ``(x, y, z) -> (x, y, -z)``.
"""

import dataclasses
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .rtm import Rtm

__all__ = ["Clip", "houdini_matrices", "houdini_motion", "build_clip", "sample", "cycle_position", "pose_world", "skin_pose",
           "blend", "KEY_TOLERANCE"]

#: A phase this close to a key frame (the file's phases are float32) returns that frame's matrices as stored.
KEY_TOLERANCE = 1e-6

# A3OB matrices are in Blender axes (X, Y, Z) = Arma (x, z, y); Houdini is Arma (x, y, -z) = Blender (X, Z, -Y).
_TO_HOUDINI = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])


def houdini_matrices(matrices) -> np.ndarray:
    """A3OB 4x4 matrices (``Transform.matrix``, any leading shape) in Houdini axes, still column-vector form (``x' = M x``)."""
    m = np.asarray(matrices, dtype=np.float64)
    q = np.eye(4)
    q[:3, :3] = _TO_HOUDINI
    return q @ m @ q.T


def houdini_motion(motion: Sequence[float]) -> np.ndarray:
    """The motion vector of one cycle in Houdini axes. ``Rtm.motion`` is A3OB's reading ``(f0, f2, f1)`` of the file's three
    floats; the file holds Arma axes ``(x, y, z)`` = ``(f0, f1, f2)``, so Houdini's ``(x, y, -z)`` is ``(f0, f1, -f2)``.
    Task #88 checked it on the game's walk and run: the file's ``f2`` is the way the character goes (-1.62 m for the rifle
    walk), the other two are 0, and the foot that stands on the ground slides the opposite way."""
    return np.array([motion[0], motion[2], -motion[1]], dtype=np.float64)


@dataclasses.dataclass
class Clip:
    """An RTM matched to a skeleton: the matrices of every frame for every joint, in Houdini axes."""
    names: List[str]
    phases: np.ndarray
    #: (frames, joints, 4, 4), column-vector form. A joint with no record in the file has the identity (it stays at rest).
    matrices: np.ndarray
    #: The motion of one cycle, Houdini axes.
    motion: np.ndarray
    #: Per joint: has a record in the RTM.
    found: List[bool]
    #: The RTM bones that are not joints of the skeleton, in file order (Weapon and launcher, face bones, slots, Hips).
    extra: List[str]
    #: The parent joint of each joint (-1: none) and the rest positions (joints, 3). With them ``sample`` blends the key frames
    #: in the space of the parent (a forearm keeps turning about the elbow); without them, in model space.
    parents: Optional[List[int]] = None
    rest: Optional[np.ndarray] = None


def build_clip(rtm: Rtm, names: Sequence[str], parents: Optional[Sequence[int]] = None,
               rest: Optional[np.ndarray] = None) -> Clip:
    """Match the bones of ``rtm`` to the skeleton joints ``names`` ignoring case (the engine does the same). ``parents`` (a
    joint's parent number, -1 for a root; parents come first) and ``rest`` (joint positions) make ``sample`` blend in the
    space of the parent."""
    index = {}
    for number, name in enumerate(names):
        index.setdefault(name.lower(), number)
    column = {}  # joint -> position in the file's bone list
    extra: List[str] = []
    for place, bone in enumerate(rtm.bones):
        joint = index.get(bone.lower())
        if joint is None:
            extra.append(bone)
        elif joint not in column:
            column[joint] = place
    if parents is not None and (len(parents) != len(names) or any(p >= j for j, p in enumerate(parents))):
        parents = None  # not parents first: blend in model space instead
    frames = max(len(rtm.frames), 1)
    matrices = np.tile(np.eye(4), (frames, len(names), 1, 1))
    for f, frame in enumerate(rtm.frames):
        for joint, place in column.items():
            matrices[f, joint] = houdini_matrices(frame.transforms[place].matrix)
    phases = np.array([frame.phase for frame in rtm.frames] or [0.0], dtype=np.float64)
    return Clip(list(names), phases, matrices, houdini_motion(rtm.motion), [j in column for j in range(len(names))], extra,
                None if parents is None or rest is None else list(parents),
                None if parents is None or rest is None else np.asarray(rest, dtype=np.float64).reshape(-1, 3))


def _quaternions(rotation: np.ndarray) -> np.ndarray:
    """Unit quaternions ``(w, x, y, z)`` (n, 4) of rotation matrices (n, 3, 3), column-vector form."""
    m = rotation
    trace = m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2]
    out = np.empty((len(m), 4))
    for i in range(len(m)):
        a = m[i]
        if trace[i] > 0:
            s = 2.0 * np.sqrt(trace[i] + 1.0)
            out[i] = [0.25 * s, (a[2, 1] - a[1, 2]) / s, (a[0, 2] - a[2, 0]) / s, (a[1, 0] - a[0, 1]) / s]
        elif a[0, 0] >= a[1, 1] and a[0, 0] >= a[2, 2]:
            s = 2.0 * np.sqrt(1.0 + a[0, 0] - a[1, 1] - a[2, 2])
            out[i] = [(a[2, 1] - a[1, 2]) / s, 0.25 * s, (a[0, 1] + a[1, 0]) / s, (a[0, 2] + a[2, 0]) / s]
        elif a[1, 1] >= a[2, 2]:
            s = 2.0 * np.sqrt(1.0 + a[1, 1] - a[0, 0] - a[2, 2])
            out[i] = [(a[0, 2] - a[2, 0]) / s, (a[0, 1] + a[1, 0]) / s, 0.25 * s, (a[1, 2] + a[2, 1]) / s]
        else:
            s = 2.0 * np.sqrt(1.0 + a[2, 2] - a[0, 0] - a[1, 1])
            out[i] = [(a[1, 0] - a[0, 1]) / s, (a[0, 2] + a[2, 0]) / s, (a[1, 2] + a[2, 1]) / s, 0.25 * s]
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def _matrices(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    out = np.empty((len(q), 3, 3))
    out[:, 0] = np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], axis=1)
    out[:, 1] = np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], axis=1)
    out[:, 2] = np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], axis=1)
    return out


def _blend_pair(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Two sets of 4x4 matrices (n, 4, 4) blended: rotation by quaternion slerp, position linear."""
    qa, qb = _quaternions(a[:, :3, :3]), _quaternions(b[:, :3, :3])
    dot = (qa * qb).sum(axis=1, keepdims=True)
    qb = np.where(dot < 0, -qb, qb)
    angle = np.arccos(np.clip(np.abs(dot), -1.0, 1.0))
    sine = np.sin(angle)
    near = sine < 1e-6
    wa = np.where(near, 1.0 - t, np.sin((1.0 - t) * angle) / np.where(near, 1.0, sine))
    wb = np.where(near, t, np.sin(t * angle) / np.where(near, 1.0, sine))
    q = wa * qa + wb * qb
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    out = np.tile(np.eye(4), (len(a), 1, 1))
    out[:, :3, :3] = _matrices(q)
    out[:, :3, 3] = (1.0 - t) * a[:, :3, 3] + t * b[:, :3, 3]
    return out


def _blend_in_parent_space(clip: Clip, a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Blend two key frames the way A3OB's curves do: the pose of each joint relative to its parent is blended, then the
    joints are put together again from the root down. (Blending the absolute matrices would shorten a bone that turns
    about its parent: the elbow would slide.) The joints' own axes do not matter here, only their positions."""
    count = len(a)
    shift = np.tile(np.eye(4), (count, 1, 1))
    shift[:, :3, 3] = clip.rest
    unshift = np.tile(np.eye(4), (count, 1, 1))
    unshift[:, :3, 3] = -clip.rest
    world_a, world_b = a @ shift, b @ shift  # the joint's pose in model space, axes along the model's
    parents = clip.parents
    local_a, local_b = world_a.copy(), world_b.copy()
    for j in range(count):
        if parents[j] >= 0:
            local_a[j] = np.linalg.inv(world_a[parents[j]]) @ world_a[j]
            local_b[j] = np.linalg.inv(world_b[parents[j]]) @ world_b[j]
    local = _blend_pair(local_a, local_b, t)
    world = local.copy()
    for j in range(count):
        if parents[j] >= 0:
            world[j] = world[parents[j]] @ local[j]
    return world @ unshift


def sample(clip: Clip, phase: float) -> np.ndarray:
    """The skin matrices (joints, 4, 4) at ``phase`` (0 to 1): the stored matrices on a key frame, else the two neighbours
    blended (rotation by quaternion slerp, position linear; in the space of the parent when the clip knows the parents).
    Before the first and after the last key the end frame holds."""
    phases = clip.phases
    if phase <= phases[0] + KEY_TOLERANCE:
        return clip.matrices[0].copy()
    if phase >= phases[-1] - KEY_TOLERANCE:
        return clip.matrices[-1].copy()
    upper = int(np.searchsorted(phases, phase, side="right"))
    lower = upper - 1
    span = phases[upper] - phases[lower]
    t = (phase - phases[lower]) / span if span > 0 else 0.0
    if abs(t) <= KEY_TOLERANCE:
        return clip.matrices[lower].copy()
    if abs(t - 1.0) <= KEY_TOLERANCE:
        return clip.matrices[upper].copy()
    a, b = clip.matrices[lower], clip.matrices[upper]
    if clip.parents is not None and clip.rest is not None:
        return _blend_in_parent_space(clip, a, b, t)
    return _blend_pair(a, b, t)


def cycle_position(position: float, loop: bool) -> Tuple[int, float]:
    """``(cycle number, phase)`` of a position in cycles (1.5 is half way through the second cycle). Looping wraps; holding
    stays at the first or the last frame."""
    if loop:
        cycle = int(np.floor(position))
        return cycle, float(position - cycle)
    return 0, float(min(max(position, 0.0), 1.0))


def skin_pose(skin: np.ndarray, rest: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """``(G, q)`` for ``check.pose_skin`` (``x' = q + G (x - rest)``) from skin matrices (joints, 4, 4) and the joints' rest
    positions (joints, 3): ``G`` is the rotation part and ``q = G rest + t``."""
    g = skin[:, :3, :3]
    return g, np.einsum("jab,jb->ja", g, np.asarray(rest, dtype=np.float64)) + skin[:, :3, 3]


def pose_world(rest_positions: np.ndarray, rest_rotations: Optional[np.ndarray], skin: np.ndarray,
               shift: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray]:
    """The animated skeleton: ``(positions (n, 3), transforms (n, 9))`` in the form of the KineFX attributes ``P`` and
    ``transform`` (row-vector 3x3, row-major).

    ``rest_rotations`` are the 9-float ``transform`` of the rest joints reshaped to (n, 3, 3) (None: identity). In row-vector
    form ``joint = rest * skin`` (module text); ``shift`` moves every joint by a vector (the displacement of the motion)."""
    count = len(rest_positions)
    rest = np.tile(np.eye(4), (count, 1, 1))
    if rest_rotations is not None:
        rest[:, :3, :3] = np.asarray(rest_rotations, dtype=np.float64).reshape(count, 3, 3)
    rest[:, 3, :3] = rest_positions
    joint = rest @ np.transpose(skin, (0, 2, 1))
    positions = joint[:, 3, :3].copy()
    if shift is not None:
        positions = positions + np.asarray(shift, dtype=np.float64)
    return positions, joint[:, :3, :3].reshape(count, 9).copy()


def blend(points: np.ndarray, weights: np.ndarray, skin: np.ndarray) -> np.ndarray:
    """Linear blend skinning with the skin matrices applied to the points directly: ``sum_j w_j (R_j x + t_j)``. The weights
    are normalised per point (a point with no weight stays where it is). This is the reference of test 4.2."""
    w = np.asarray(weights, dtype=np.float64)
    total = w.sum(axis=1, keepdims=True)
    w = np.divide(w, total, out=np.zeros_like(w), where=total > 0)
    rotation = np.einsum("nj,jab->nab", w, skin[:, :3, :3])
    shift = w @ skin[:, :3, 3]
    moved = np.einsum("nab,nb->na", rotation, points) + shift
    return np.where(total > 0, moved, points)

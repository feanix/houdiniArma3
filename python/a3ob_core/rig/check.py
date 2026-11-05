# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no rig check; the problems and the stress test are the ones of docs/design/character-rigging.md 5.2 and 5.3.
"""The measures of the Rig Check node, with no Houdini (task #68).

Everything here takes numpy arrays: ``points`` (n, 3), ``tris`` (m, 3) point numbers, ``weights`` (n, bones), and a skeleton
as ``parents`` (bones,) and ``rest`` (bones, 3) joint positions. Nothing here changes its inputs.

- **Weight problems** (5.2): ``weight_problems`` gives the weight jump, the number of influences, the weight-sum error and
  the heaviest bone of every point; ``straddle`` the share of a rigid piece that favours another bone; ``joint_depth`` how
  far inside the mesh each joint is.
- **Pose test** (5.3): ``POSES`` are the built-in test poses (bone rotations about the joints). ``pose_skin`` poses the mesh
  with **linear blend skinning**, as Arma does (never dual quaternion). ``edge_stress`` is the stretch and compression of
  the edges; ``volume_change`` the loss or gain of volume around each point.
- **The volume measure** (task #68, owner request): for each point, the volume of the "cone" from a reference point to the
  triangles around the point, summed over its triangles (a third of each, so every triangle is shared by its three
  corners). The reference point is the point's own blend of joints, ``sum(weight * joint)``, and in the pose it is
  skinned like a point. The ratio posed over rest, minus 1, is ``a3ob_rig_volume_change``: negative is lost volume, positive is gained.
  Why this and not a plain area or edge ratio: it grows or shrinks with the area of the skin patch and with its
  distance from the joint (the thickness of the limb there), so it sees a candy-wrapped, pinched or flattened joint
  that edge lengths alone miss. A rigid move of the bone changes nothing. The reference being the joint (not the mesh
  centre) keeps the value local and independent of where the character stands.
"""

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .fit import aim_targets, normalised_weights

__all__ = ["POSES", "WORST", "RAMPS", "ramp_for", "edges_of", "weight_problems", "straddle", "joint_depth", "pose_skin",
           "pose_matrices", "pose_rotations", "edge_stress", "volume_change", "worst_joints", "PoseResult", "run_pose", "combine",
           "INFLUENCE_MIN", "NO_WEIGHT"]

#: A weight at or below this is no influence (the grid of the Arma file, 1/254).
INFLUENCE_MIN = 1.0 / 254.0
#: The value of ``a3ob_rig_sumerr`` of a point with no weight at all (the colour is black).
NO_WEIGHT = -1.0
WORST = "worst"

# ---- test poses ---------------------------------------------------------------------------------------------------------
# A pose is a list of (bone names, target, degrees). The bones rotate about their own joints by that angle, towards the
# world direction ``target`` (the axis is bone direction x target); a target of None turns the bone about its own axis
# (a twist). Bones a skeleton does not have are skipped. Left is +X, up is +Y, forward is +Z.
UP, FORWARD, BACK = (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)
POSES: Dict[str, Tuple[str, list]] = {
    "arms_up": ("Arms raised 90", [(("leftarm", "rightarm"), UP, 90.0)]),
    "arms_forward": ("Arms forward 90", [(("leftarm", "rightarm"), FORWARD, 90.0)]),
    "elbows": ("Elbows bent 90", [(("leftforearm", "rightforearm"), FORWARD, 90.0)]),
    "hips": ("Hips flexed 90 (sitting)", [(("leftupleg", "rightupleg"), FORWARD, 90.0)]),
    "knees": ("Knees bent 90", [(("leftleg", "rightleg"), BACK, 90.0)]),
    "spine": ("Spine bent 30 forward", [(("spine", "spine1", "spine2"), FORWARD, 10.0)]),
    "neck": ("Neck turned 45", [(("neck",), None, 45.0)]),
    "wrists": ("Wrists bent 45", [(("lefthand", "righthand"), UP, 45.0)]),
}


def _rotation(axis: np.ndarray, degrees: float) -> np.ndarray:
    """Rodrigues: the rotation about the unit ``axis`` by ``degrees`` (column-vector form)."""
    angle = np.radians(degrees)
    x, y, z = axis
    k = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + np.sin(angle) * k + (1.0 - np.cos(angle)) * (k @ k)


def pose_rotations(pose: str, names: Sequence[str], parents: Sequence[int], rest: np.ndarray):
    """``(local (bones, 3, 3), used)``: the rotation of every bone in the test pose ``pose``, in the rest axes of its parent
    (the identity for a bone the pose does not move), and the names of the bones that moved."""
    rest = np.asarray(rest, dtype=np.float64)
    local = np.tile(np.eye(3), (len(names), 1, 1))
    ends = aim_targets(parents, rest)
    index = {name.lower(): i for i, name in enumerate(names)}
    used = []
    for bones, target, degrees in POSES[pose][1]:
        for bone in bones:
            joint = index.get(bone)
            if joint is None or ends[joint] < 0:
                continue
            direction = rest[ends[joint]] - rest[joint]
            direction = direction / np.linalg.norm(direction)
            if target is None:
                axis = direction
            else:
                goal = np.asarray(target)
                axis = np.cross(direction, goal)
                if np.linalg.norm(axis) < 0.3:  # the bone already points along (or against) the goal: swing about Z
                    axis = np.array([0.0, 0.0, 1.0]) * (1.0 if rest[joint][0] >= 0 else -1.0)
                axis = axis / np.linalg.norm(axis)
            local[joint] = _rotation(axis, degrees)
            used.append(names[joint])
    return local, used


def pose_matrices(parents: Sequence[int], rest: np.ndarray, local: np.ndarray):
    """``(G, q)``: the rotation (bones, 3, 3) and the position (bones, 3) of every joint in the pose. A bone's rotation is
    its parent's times its ``local`` one; its joint is carried by the parent. The skin matrix of a bone is
    ``x' = q + G (x - rest)``."""
    count = len(parents)
    rest = np.asarray(rest, dtype=np.float64)
    rotation = np.tile(np.eye(3), (count, 1, 1))
    position = rest.copy()
    done = [False] * count

    def visit(joint: int) -> None:
        if done[joint]:
            return
        parent = parents[joint]
        if parent < 0:
            rotation[joint], position[joint] = local[joint], rest[joint]
        else:
            visit(parent)
            rotation[joint] = rotation[parent] @ local[joint]
            position[joint] = position[parent] + rotation[parent] @ (rest[joint] - rest[parent])
        done[joint] = True

    for joint in range(count):
        visit(joint)
    return rotation, position


def pose_skin(points: np.ndarray, w: np.ndarray, rest: np.ndarray, rotation: np.ndarray, position: np.ndarray
              ) -> np.ndarray:
    """Linear blend skinning: ``sum(w * (q + G (x - p)))`` per point. ``w`` must already sum to 1 (``normalised_weights``)."""
    blend = (w @ rotation.reshape(len(rotation), 9)).reshape(-1, 3, 3)
    shift = w @ (position - np.einsum("jab,jb->ja", rotation, rest))
    return np.einsum("nab,nb->na", blend, points) + shift


def edges_of(tris: np.ndarray) -> np.ndarray:
    """The unique edges (e, 2) of a triangle list, each with the smaller point number first."""
    if not len(tris):
        return np.zeros((0, 2), dtype=np.int64)
    both = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    return np.unique(np.sort(both, axis=1), axis=0)


def _per_point_mean(count: int, edges: np.ndarray, value: np.ndarray) -> np.ndarray:
    total = np.bincount(edges[:, 0], weights=value, minlength=count) + np.bincount(edges[:, 1], weights=value, minlength=count)
    uses = np.bincount(edges.reshape(-1), minlength=count)
    return np.where(uses > 0, total / np.maximum(uses, 1), 0.0)


# ---- the weight problems ------------------------------------------------------------------------------------------------

def weight_problems(weights: np.ndarray, edges: np.ndarray):
    """``(jump, infl, sumerr, dominant)`` per point.

    - ``jump``: the largest weight difference to an edge neighbour: half the L1 distance of the weight rows (0 same, 1 other
      bones altogether).
    - ``infl``: the number of bones with a weight above 1/254.
    - ``sumerr``: ``abs(sum - 1)``; ``NO_WEIGHT`` (-1) for a point with no weight.
    - ``dominant``: the heaviest bone, -1 for a point with no weight.
    """
    count = len(weights)
    jump = np.zeros(count)
    for start in range(0, len(edges), 20000):
        part = edges[start:start + 20000]
        step = 0.5 * np.abs(weights[part[:, 0]] - weights[part[:, 1]]).sum(axis=1)
        np.maximum.at(jump, part[:, 0], step)
        np.maximum.at(jump, part[:, 1], step)
    infl = (weights > INFLUENCE_MIN).sum(axis=1).astype(np.int64)
    total = weights.sum(axis=1)
    sumerr = np.where(total > 0, np.abs(total - 1.0), NO_WEIGHT)
    dominant = np.where(total > 0, weights.argmax(axis=1), -1).astype(np.int64)
    return jump, infl, sumerr, dominant


def straddle(dominant: np.ndarray, piece: np.ndarray) -> np.ndarray:
    """For every point of a rigid piece (``piece`` >= 0): the share of the piece's points whose heaviest bone is not the
    piece's most common heaviest bone. Other points get 0."""
    out = np.zeros(len(piece))
    for number in np.unique(piece[piece >= 0]):
        members = np.nonzero(piece == number)[0]
        bones = dominant[members]
        bones = bones[bones >= 0]
        if not len(bones):
            continue
        share = 1.0 - float(np.bincount(bones).max()) / len(members)
        out[members] = share
    return out


def _closest_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The distance (k,) from every point ``p`` (k, 3) to the closest of the triangles ``a, b, c`` (m, 3 each)."""
    ab, ac = b - a, c - a
    ap = p[:, None, :] - a[None]
    d1, d2 = (ab * ap).sum(-1), (ac * ap).sum(-1)
    bp = p[:, None, :] - b[None]
    d3, d4 = (ab * bp).sum(-1), (ac * bp).sum(-1)
    cp = p[:, None, :] - c[None]
    d5, d6 = (ab * cp).sum(-1), (ac * cp).sum(-1)
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v, w = vb / denom, vc / denom  # inside the face
        t_ab = d1 / (d1 - d3)
        t_ac = d2 / (d2 - d6)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
    result = a[None] + ab[None] * v[..., None] + ac[None] * w[..., None]  # the face; the regions below are disjoint
    edge_ab = a[None] + ab[None] * t_ab[..., None]
    edge_ac = a[None] + ac[None] * t_ac[..., None]
    edge_bc = b[None] + (c - b)[None] * t_bc[..., None]
    result = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None], edge_ab, result)
    result = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None], edge_ac, result)
    result = np.where(((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0))[..., None], edge_bc, result)
    result = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c[None], result)
    result = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b[None], result)
    result = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a[None], result)
    distance = np.linalg.norm(p[:, None, :] - result, axis=-1)
    return np.where(np.isfinite(distance), distance, np.inf).min(axis=1)  # a degenerate triangle gives NaN: skipped


def _winding(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The generalised winding number of the triangle soup around every point ``p`` (1 inside a closed mesh, 0 outside; the
    sign follows the orientation of the polygons)."""
    ra, rb, rc = a[None] - p[:, None, :], b[None] - p[:, None, :], c[None] - p[:, None, :]
    la, lb, lc = (np.linalg.norm(r, axis=-1) for r in (ra, rb, rc))
    triple = (ra * np.cross(rb, rc)).sum(-1)
    bottom = la * lb * lc + (ra * rb).sum(-1) * lc + (rb * rc).sum(-1) * la + (rc * ra).sum(-1) * lb
    return (2.0 * np.arctan2(triple, bottom)).sum(axis=1) / (4.0 * np.pi)


def joint_depth(joints: np.ndarray, points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """The signed distance of every joint to the mesh surface, in metres: positive inside the mesh (winding number above a half),
    negative outside. A mesh with no triangle gives NaN."""
    joints = np.asarray(joints, dtype=np.float64)
    if not len(tris):
        return np.full(len(joints), np.nan)
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    out = np.zeros(len(joints))
    step = max(1, 400000 // len(tris))
    for start in range(0, len(joints), step):
        chunk = joints[start:start + step]
        distance = _closest_distance(chunk, a, b, c)
        inside = np.abs(_winding(chunk, a, b, c)) > 0.5
        out[start:start + step] = np.where(inside, distance, -distance)
    return out


# ---- the pose stress test -----------------------------------------------------------------------------------------------

def edge_stress(points: np.ndarray, posed: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Per point, the average over its edges of ``log(posed length / rest length)``, as a fraction (``exp(mean) - 1``, so 0.3 is
    30 % longer and -0.3 is 30 % shorter): positive stretched, negative compressed."""
    rest_len = np.linalg.norm(points[edges[:, 0]] - points[edges[:, 1]], axis=1)
    pose_len = np.linalg.norm(posed[edges[:, 0]] - posed[edges[:, 1]], axis=1)
    good = (rest_len > 1e-9) & (pose_len > 1e-12)
    value = np.zeros(len(edges))
    value[good] = np.log(pose_len[good] / rest_len[good])
    return np.expm1(_per_point_mean(len(points), edges, value))


def _cone_volumes(points: np.ndarray, reference: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per point: a third of the volume of the tetrahedra (reference of that point, triangle) over its triangles."""
    total = np.zeros(len(points))
    for corner in range(3):
        owner = tris[:, corner]
        origin = reference[owner]
        u, v, w = (points[tris[:, k]] - origin for k in range(3))
        total += np.bincount(owner, weights=(u * np.cross(v, w)).sum(-1) / 18.0, minlength=len(points))
    return total


def volume_change(points: np.ndarray, posed: np.ndarray, tris: np.ndarray, w: np.ndarray, rest: np.ndarray,
                  rotation: np.ndarray, position: np.ndarray) -> np.ndarray:
    """Per point, the local volume change of a pose against the rest mesh (module text): posed over rest, minus 1; negative is
    loss. ``w`` sums to 1 per point; ``rest`` are the joints at rest, ``rotation`` and ``position`` the pose (``pose_matrices``).
    The reference point of a point is its blend of the joints; in the pose it is skinned like a point. A point whose own rest
    volume is small (a flat patch seen edge-on from its joint) is measured against a quarter of the typical volume, so it
    cannot show a huge ratio from a tiny number. The value is clipped to -1 .. +3."""
    if not len(tris):
        return np.zeros(len(points))
    reference = w @ rest
    before = _cone_volumes(points, reference, tris)
    after = _cone_volumes(posed, pose_skin(reference, w, rest, rotation, position), tris)
    size = np.abs(before)
    typical = float(np.median(size[size > 0])) if (size > 0).any() else 0.0
    base = np.maximum(size, 0.25 * typical)  # a point whose own volume is tiny is measured against a quarter of the typical one
    out = np.where(base > 1e-15, (after - before) * np.sign(before) / np.maximum(base, 1e-15), 0.0)
    return np.clip(out, -1.0, 3.0)


@dataclasses.dataclass
class PoseResult:
    name: str
    label: str
    posed: np.ndarray
    stress: np.ndarray
    volume: np.ndarray
    used: List[str]
    position: np.ndarray


def run_pose(pose: str, names, parents, rest: np.ndarray, points: np.ndarray, tris: np.ndarray, edges: np.ndarray,
             w: np.ndarray) -> PoseResult:
    """One test pose: the posed mesh, the stress and the volume change."""
    local, used = pose_rotations(pose, names, parents, rest)
    rotation, position = pose_matrices(parents, rest, local)
    posed = pose_skin(points, w, rest, rotation, position)
    return PoseResult(pose, POSES[pose][0], posed, edge_stress(points, posed, edges),
                      volume_change(points, posed, tris, w, rest, rotation, position), used, position)


def combine(results: Sequence[PoseResult], field: str) -> np.ndarray:
    """The value of ``field`` (stress or volume) with the largest size over the poses, with its sign."""
    stack = np.stack([getattr(r, field) for r in results])
    return np.take_along_axis(stack, np.abs(stack).argmax(axis=0)[None], axis=0)[0]


def worst_joints(volume: np.ndarray, w: np.ndarray, top: int = 3, limit: float = -0.01) -> List[Tuple[int, float]]:
    """The joints with the worst volume loss: ``(joint, loss)`` for the ``top`` joints whose weighted mean of the loss is below
    ``limit`` (negative), worst first. Each point counts for a joint as much as it is weighted to it."""
    loss = np.minimum(volume, 0.0)
    share = w.sum(axis=0)
    mean = np.where(share > 0, (w * loss[:, None]).sum(axis=0) / np.maximum(share, 1e-12), 0.0)
    order = np.argsort(mean)[:top]
    return [(int(j), float(mean[j])) for j in order if mean[j] < limit]


# ---- colour ramps (value, (r, g, b)) -------------------------------------------------------------------------------------

_BLACK, _WHITE, _GREY = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.55, 0.55, 0.55)
_RED, _YELLOW, _ORANGE = (1.0, 0.1, 0.1), (1.0, 0.9, 0.1), (1.0, 0.55, 0.0)
_BLUE, _GREEN, _MAGENTA, _PURPLE, _TEAL = (0.2, 0.4, 1.0), (0.2, 0.8, 0.3), (1.0, 0.1, 0.9), (0.55, 0.1, 0.8), (0.1, 0.8, 0.6)
_HUES = [(1, 0.2, 0.2), (1, 0.55, 0.1), (1, 0.9, 0.1), (0.6, 0.9, 0.1), (0.1, 0.8, 0.3), (0.1, 0.8, 0.8), (0.2, 0.5, 1),
         (0.4, 0.3, 1), (0.7, 0.2, 0.9), (1, 0.2, 0.7), (0.6, 0.4, 0.3), (0.6, 0.6, 0.6)]

#: Show entry -> (the point attribute it colours, a label for the visualizer).
RAMPS = {
    "origin": ("a3ob_rig_origin", "Origin"), "jump": ("a3ob_rig_jump", "Weight jump"),
    "influences": ("a3ob_rig_infl", "Influences"), "sum": ("a3ob_rig_sumerr", "Weight sum"),
    "straddle": ("a3ob_rig_straddle", "Straddle"), "stress": ("a3ob_rig_stress", "Stress"),
    "heaviest": ("a3ob_rig_dominant", "Heaviest bone"), "volume": ("a3ob_rig_volume_change", "Volume"),
}


def ramp_for(show: str, stress_range: float, bones: int):
    """``(stops, low, high)`` for the node visualizer of the Show entry ``show``: stops ``(position 0..1, colour)``, and the
    attribute values at the two ends of the ramp."""
    stress_range = max(float(stress_range), 1e-6)
    if show == "origin":  # stepped: 0 black, 1 grey, 2 orange, 3 blue, 4 green
        colours, low, high = [_BLACK, _GREY, _ORANGE, _BLUE, _GREEN], -0.5, 4.5
        values = [(v + s * 0.45, colours[v]) for v in range(5) for s in (-1, 1)]
    elif show == "jump":
        low, high, values = 0.0, 0.5, [(0.0, _WHITE), (0.5, _RED)]
    elif show == "influences":
        low, high = 0.0, 6.0
        values = [(1.0, _GREY), (4.0, _WHITE), (4.55, _WHITE), (4.65, _MAGENTA), (5.4, _MAGENTA), (5.6, _RED), (6.0, _RED)]
    elif show == "sum":  # -1 is a point with no weight: black; 0 white; 0.1 yellow; more red
        low, high = -1.0, 0.2
        values = [(-1.0, _BLACK), (-0.02, _BLACK), (0.0, _WHITE), (0.1, _YELLOW), (0.2, _RED)]
    elif show == "straddle":
        low, high, values = 0.0, 0.5, [(0.0, _WHITE), (0.5, _RED)]
    elif show == "stress":
        low, high = -stress_range, stress_range
        values = [(-stress_range, _BLUE), (0.0, _WHITE), (stress_range, _RED)]
    elif show == "volume":  # loss purple, none white, gain teal (stress is blue and red)
        low, high = -stress_range, stress_range
        values = [(-stress_range, _PURPLE), (0.0, _WHITE), (stress_range, _TEAL)]
    elif show == "heaviest":  # a repeating run of 12 hues; -1 (no weight) black
        low, high = -1.0, float(max(bones - 1, 1))
        values = [(-1.0, _BLACK), (-0.55, _BLACK)]
        for bone in range(bones):
            values += [(bone - 0.45, _HUES[bone % len(_HUES)]), (bone + 0.45, _HUES[bone % len(_HUES)])]
    else:
        raise KeyError(show)
    span = high - low
    return [(min(1.0, max(0.0, (v - low) / span)), colour) for v, colour in values], low, high

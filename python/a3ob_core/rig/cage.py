# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no unpose. The method is the advisor's prototype of 2026-11-06 (D44, task #80): a cage from the wrapped body
# to the Arma sample body, and one rigid transform for each rigid piece.
"""The pieces of the cage Unpose that need no Houdini (task #80, D44).

The cage itself (Point Deform: rest lattice = the sample body wrapped onto the user's body, deformed lattice = the sample
body in the Arma rest) is the Houdini verb; it runs in ``a3ob_houdini.nodes.unpose``. Here are the two parts around it:

- **The cage.** ``point_deform`` is the maths of the Point Deform SOP (default parameters), in numpy. Houdini has no verb
  for that SOP (``nodeVerb("pointdeform")`` is None; it is a subnet of wrangles), and a Python SOP cannot make nodes while it
  cooks, so the wrangles' maths is ported and checked against the real node (mean 1 mm apart on the owner scene).
- **Which points are a rigid piece.** A piece is the ``a3ob_rig_piece`` of Accessory Weights in Rigid mode, or a connected
  island of points outside the body that follow one bone only (the helmet).
- **Loose accessories** (task #82): points outside the body that bend (``surface_follow``) keep their offset from the
  closest point of the wrapped body, turned with the surface there, so they are carried by the body at their own distance
  and not bent by the cage.
- **Where a piece goes.** The cage moves the points of the piece to new places. The piece is then put at the rigid
  transform (rotation and shift, no size) that fits those places best (Kabsch). So the piece stays on the body where the
  cage puts it, and none of its edges changes.
"""

import re
from typing import List, Optional, Sequence

import numpy as np

from .foot import find_feet
from .transfer import closest_points

__all__ = ["point_deform", "lattice_transforms", "single_bone_pieces", "rigid_fit", "place_pieces", "surface_follow", "SMOOTH_RADIUS",
           "hold_orientation", "piece_bones", "anchor_pieces", "carry_body", "beyond_cage", "stem_share", "ONE_BONE_SHARE", "BEYOND_CAGE", "STEM_LINKS", "STEM_SOLID", "STEM_HOLD", "STEM_REACH", "STEM_DEPTH", "STEM_WIDTH","finger_columns", "keep_finger_shape", "FINGER_SMOOTHING", "sole_roll", "keep_sole_tilt", "FINGER_BONE"]

#: The reach (metres) of the average of the moves of loose accessory points.
SMOOTH_RADIUS = 0.5

#: A weight below this is no weight when a point is checked for following one bone only.
WEIGHT_EPS = 1e-4


def _rotation_of(matrices: np.ndarray) -> np.ndarray:
    """The closest rotation of each 3x3 matrix (polar decomposition, as VEX ``polardecomp``)."""
    u, _, vt = np.linalg.svd(matrices)
    rot = u @ vt
    flip = np.linalg.det(rot) < 0
    u[flip, :, 2] *= -1.0
    return u @ vt


def _neighbour_rings(count: int, polys) -> np.ndarray:
    """``(n, K)`` neighbour point numbers of every point (padded with -1), in the order Houdini's ``neighbours()`` mostly
    gives: for each polygon corner at the point (in polygon order), the previous point and then the next one, without
    repeats. Successive neighbours are what the Point Deform frames are built from."""
    lists = [[] for _ in range(count)]
    for ring in polys:
        ring = list(ring)
        for k, a in enumerate(ring):
            for q in (ring[k - 1], ring[(k + 1) % len(ring)]):
                if q != a and q not in lists[a]:
                    lists[a].append(q)
    table = -np.ones((count, max(max((len(x) for x in lists), default=0), 1)), dtype=np.int64)
    for i, x in enumerate(lists):
        table[i, :len(x)] = x
    return table


def _frames(vectors: np.ndarray, ups: np.ndarray) -> np.ndarray:
    """VEX ``maketransform(normalize(z), normalize(y))`` per row: a matrix whose rows are the x, y and z axes."""
    z = vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-300)
    y = ups / np.maximum(np.linalg.norm(ups, axis=-1, keepdims=True), 1e-300)
    return np.stack([np.cross(y, z), y, z], axis=-2)


def lattice_transforms(rest: np.ndarray, deformed: np.ndarray, polys) -> np.ndarray:
    """The 3x3 matrix of every lattice point that takes a row vector of the rest lattice to the deformed one: the average
    of the frames built from successive pairs of neighbours, made a rotation (the ``compute_xforms_sequential`` wrangle of
    the Point Deform SOP, Rigid Projection on)."""
    table = _neighbour_rings(len(rest), polys)
    count, width = table.shape
    degree = (table >= 0).sum(axis=1)
    rows = np.arange(count)
    total = np.zeros((count, 3, 3))
    weight = np.zeros(count)
    for k in range(width):
        use = (degree > k) & (degree >= 2)
        if not use.any():
            continue
        now = table[:, k].clip(0)
        last = table[rows, (k - 1) % np.maximum(degree, 1)].clip(0)
        dp, ldp = deformed[now] - deformed, deformed[last] - deformed
        rdp, lrdp = rest[now] - rest, rest[last] - rest
        up, rup = np.cross(dp, ldp), np.cross(rdp, lrdp)
        w = np.sqrt(np.linalg.norm(up, axis=1) * np.linalg.norm(rup, axis=1))
        good = use & (w > 1e-24)
        if not good.any():
            continue
        fd, fr = _frames(dp[good], up[good]), _frames(rdp[good], rup[good])
        total[good] += w[good, None, None] * (fr.transpose(0, 2, 1) @ fd)
        weight[good] += w[good]
    out = np.tile(np.eye(3), (count, 1, 1))
    some = weight > 0
    out[some] = _rotation_of(total[some] / weight[some, None, None])
    return out


def point_deform(points: np.ndarray, rest: np.ndarray, deformed: np.ndarray, polys, radius: float = 0.1,
                 minpt: int = 1, maxpt: int = 100, chunk: int = 512) -> np.ndarray:
    """``points`` moved by a cage: the same maths as the Point Deform SOP with its default parameters. Each point finds the
    lattice points of ``rest`` within ``radius`` (at most ``maxpt``, at least ``minpt`` whatever the distance), with the
    weight ``(1 - (d / radius)^2)^2``. Each lattice point moves it as its own rigid transform and shift would, and the
    results are averaged by weight. ``polys`` are the polygons of the lattice (lists of point numbers); ``rest`` and
    ``deformed`` have the same point order."""
    points, rest, deformed = (np.asarray(a, dtype=np.float64) for a in (points, rest, deformed))
    xform = lattice_transforms(rest, deformed, polys)
    shift = deformed - rest
    out = points.copy()
    keep = min(maxpt, len(rest))
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk]
        d2 = ((p[:, None, :] - rest[None, :, :]) ** 2).sum(axis=2)
        near = np.argpartition(d2, keep - 1, axis=1)[:, :keep] if keep < len(rest) else np.tile(np.arange(len(rest)), (len(p), 1))
        nd2 = np.take_along_axis(d2, near, axis=1)
        scale = np.full(len(p), radius * radius)
        inside = nd2 <= radius * radius
        few = inside.sum(axis=1) < minpt  # too few in the radius: take the minpt closest, radius a bit more than the farthest
        if few.any():
            order = np.argsort(nd2[few], axis=1)[:, :minpt]
            far = np.take_along_axis(nd2[few], order, axis=1)[:, -1]
            scale[few] = 1.21 * far
            inside[few] = False
            rows = np.nonzero(few)[0]
            for r, o in zip(rows, order):
                inside[r, o] = True
        r2 = nd2 / scale[:, None]
        w = np.where(inside, (1.0 - r2) ** 2, 0.0)
        local = p[:, None, :] - rest[near]  # (m, k, 3)
        moved = np.einsum("mka,mkab->mkb", local, xform[near]) + rest[near] + shift[near]
        total = w.sum(axis=1)
        delta = np.einsum("mk,mka->ma", w, moved - p[:, None, :])
        ok = total > 0
        out[start:start + chunk][ok] = p[ok] + delta[ok] / total[ok, None]
    return out


def single_bone_pieces(edges: np.ndarray, weights: np.ndarray, skip: np.ndarray, first: int) -> np.ndarray:
    """The piece number of every point (-1 for none) of the islands of points that follow one bone only.

    ``edges`` is (m, 2) point pairs of the polygons, ``weights`` points x bones, ``skip`` a bool per point for the points
    that are never a piece (the body, the points already in a piece, points not unposed). An island is the points joined by
    edges that are not skipped; it is a piece when every point of it has weight on one bone and the same bone. The numbers
    start at ``first``."""
    count = len(weights)
    present = weights > WEIGHT_EPS
    one = (present.sum(axis=1) == 1) & ~skip
    bone = np.where(one, present.argmax(axis=1), -1)
    parent = np.arange(count)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    for a, b in edges[~(skip[edges[:, 0]] | skip[edges[:, 1]])]:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    root = np.array([find(i) for i in range(count)], dtype=np.int64)
    piece = -np.ones(count, dtype=np.int64)
    number = first
    for r in np.unique(root[~skip]):
        members = (root == r) & ~skip
        if members.sum() < 2:
            continue
        if one[members].all() and len(np.unique(bone[members])) == 1:
            piece[members] = number
            number += 1
    return piece


def rigid_fit(points: np.ndarray, target: np.ndarray):
    """``(R, c, t)``: the rotation ``R`` and centres that take ``points`` closest to ``target`` with no size change:
    ``x' = t + R (x - c)``. Rows are points; the result of the fit is a rotation (never a mirror)."""
    c, t = points.mean(axis=0), target.mean(axis=0)
    u, _, vt = np.linalg.svd((target - t).T @ (points - c))
    sign = np.sign(np.linalg.det(u @ vt)) or 1.0
    return u @ np.diag([1.0, 1.0, sign]) @ vt, c, t


def place_pieces(points: np.ndarray, caged: np.ndarray, piece: np.ndarray, out: Optional[np.ndarray] = None) -> np.ndarray:
    """``caged`` (the points moved by the cage) with every piece replaced by its points moved by one rigid transform that
    fits the cage best. ``piece`` is an int per point, -1 for none."""
    out = caged.copy() if out is None else out
    for number in np.unique(piece[piece >= 0]):
        members = piece == number
        rotation, c, t = rigid_fit(points[members], caged[members])
        out[members] = t + (points[members] - c) @ rotation.T
    return out


def hold_orientation(points: np.ndarray, placed: np.ndarray, members: np.ndarray, seam: np.ndarray,
                     shift: Optional[np.ndarray] = None) -> np.ndarray:
    """``placed`` with the ``members`` (a bool per point) moved by a shift only, so they keep the orientation they have in
    ``points`` (task #83). The cage turns a head with the neck-top of the sample body it is carried by; a head is a rigid mass
    that has its own tilt, and Unpose is not to nod it. The shift is the average move the cage gives the ``seam`` points (the
    members that share an edge with a non-member, the base of the head), so the head stays seated on the neck where the cage
    puts it. With no seam point all the members count. Nothing changes when there is no member.

    ``shift`` (task #91) replaces that average: the move of the head's joint, from the fitted skeleton to the Arma one. The sample
    body has no head, so the cage above its neck is extrapolated and gave a helmet a place 11.5 cm low and 9.6 cm forward of the
    joint it turns about in the game; the joint's own move keeps the head's offset to the joint."""
    out = placed.copy()
    members = np.asarray(members, dtype=bool)
    if not members.any():
        return out
    if shift is not None:
        out[members] = points[members] + np.asarray(shift, dtype=np.float64)
        return out
    seam = np.asarray(seam, dtype=bool) & members
    use = seam if seam.any() else members
    out[members] = points[members] + (placed[use] - points[use]).mean(axis=0)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# One-bone pieces keep their offset to the joint (task #91)
# ---------------------------------------------------------------------------------------------------------------------

#: A piece follows one bone when this share of its average weight is on its heaviest bone.
ONE_BONE_SHARE = 0.9
#: A piece is beyond the cage when the median distance (metres) of its points to the wrapped body is more than this (the
#: radius of the Point Deform cage, 10 cm). The cage then has no body to follow there. On the owner scene the helmet is at
#: 13.7 cm, the pauldrons, belt gear and hand plates at 1 to 6 cm.
BEYOND_CAGE = 0.10
#: The body is carried along with a piece put at its joint: the stem is the chain of ``STEM_LINKS`` bones from the piece's
#: bone towards the root (head, neck1, neck, spine3, spine2). A follower within ``STEM_HOLD`` (metres) of the stem takes all
#: of the piece's move, none beyond ``STEM_REACH``, a smooth fade between; along the stem the first ``STEM_SOLID`` links carry
#: all of it and the rest fade to nothing at the far end.
STEM_LINKS = 4
STEM_SOLID = 2
STEM_HOLD = 0.08
STEM_REACH = 0.16
#: Task #98: the stem of the head lies in the middle of the body, so the front of the neck and chest is farther from it than the
#: sides are, and the share dropped from 1 to 0.5 over 3 cm there (neck edges 2.4 times their posed length). Within ``STEM_WIDTH``
#: (metres) to the side of the stem the front-back offset counts only ``STEM_DEPTH`` times; the count grows back to 1 at
#: ``STEM_HOLD`` to the side, so the shoulders (beside the neck) are measured as before. World axes: +x sideways, +y up, +z forward.
STEM_DEPTH = 0.4
STEM_WIDTH = 0.04


def beyond_cage(points: np.ndarray, lattice: np.ndarray, radius: float = BEYOND_CAGE, chunk: int = 256) -> bool:
    """True when the median distance of ``points`` to the nearest point of ``lattice`` (the wrapped body) is more than ``radius``:
    the cage does not reach them, and Point Deform only extrapolates the move of its nearest lattice points."""
    if not len(points):
        return False
    nearest = np.empty(len(points))
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk]
        nearest[start:start + chunk] = np.sqrt(((p[:, None, :] - lattice[None, :, :]) ** 2).sum(axis=2).min(axis=1))
    return bool(np.median(nearest) > radius)


def piece_bones(weights: np.ndarray, piece: np.ndarray, active: np.ndarray, share: float = ONE_BONE_SHARE) -> dict:
    """``{piece number: bone column}`` for the pieces (among the ``active`` points) that follow one bone: at least ``share`` of
    the average weight of the piece is on its heaviest bone."""
    found = {}
    total = weights.sum(axis=1)
    for number in np.unique(piece[active & (piece >= 0)]):
        members = active & (piece == number) & (total > 0)
        if not members.any():
            continue
        mean = (weights[members] / total[members, None]).mean(axis=0)
        if mean.max() >= share:
            found[int(number)] = int(mean.argmax())
    return found


def anchor_pieces(points: np.ndarray, placed: np.ndarray, piece: np.ndarray, bones: dict, fitted: np.ndarray,
                  rest: np.ndarray):
    """``(placed, rotations)``: ``placed`` with every piece in ``bones`` ({piece number: bone column}) put at its joint (task
    #91), and the rotation of each piece. The piece keeps the rotation that fits the cage best (``rigid_fit``) but its offset
    to the joint of its bone is the one it has in the fitted pose: ``x' = rest_j + R (x - fitted_j)``. Where the cage has no
    body to follow (above the neck of a headless sample) it gives a piece a shift of its own, 10 cm away from where its bone
    goes; the joint does not. ``fitted`` and ``rest`` are the joint positions (bones x 3) of the fitted and the Arma skeleton."""
    out = placed.copy()
    rotations = {}
    for number, column in bones.items():
        members = piece == number
        rotation, _, _ = rigid_fit(points[members], placed[members])
        rotations[number] = rotation
        out[members] = rest[column] + (points[members] - fitted[column]) @ rotation.T
    return out, rotations


def stem_share(points: np.ndarray, joints: np.ndarray, parents: Sequence[int], column: int, links: int = STEM_LINKS,
               solid: int = STEM_SOLID, hold: float = STEM_HOLD, reach: float = STEM_REACH, fade: Optional[float] = None,
               depth: float = 1.0, width: float = STEM_WIDTH, ramp: float = STEM_HOLD) -> np.ndarray:
    """How much of the move of a piece at bone ``column`` each of ``points`` takes (0 to 1; task #91). The stem is the chain of
    ``links`` bones from the bone towards the root, through the fitted ``joints`` (bones x 3): for the head, head to neck1,
    neck1 to neck, neck to spine3, spine3 to spine2. A point takes all of the move when it is within ``hold`` of the stem and
    none when it is farther than ``reach``, with ``(1 - t^2)^2`` between (``t`` the way from ``hold`` to ``reach``). Along the
    stem the first ``solid`` links take all of it and the share falls smoothly to 0 over the other links. So the neck that
    leaves the piece moves with it, the chest below is carried less and less, and the shoulders beside the neck follow the
    cage.

    ``fade`` (metres, task #98) sets the length of the fade along the stem, counted from the end of the ``solid`` links. The
    stem then reaches down the bones (as far as the root) until it is that long; ``links`` is not used. None: the fade runs to
    the end of the ``links`` bones (the length it had in task #91).

    ``depth`` below 1 (task #98, ``STEM_DEPTH``) lets the front-back offset (z) of a point within ``width`` to the side (x) of the
    stem count that much only; at ``hold`` to the side it counts fully again."""
    chain = [column]
    if fade is None:
        while len(chain) <= links and parents[chain[-1]] >= 0:
            chain.append(int(parents[chain[-1]]))
    else:
        total, steps_taken = 0.0, 0
        while parents[chain[-1]] >= 0 and (steps_taken < solid or total < fade):
            parent = int(parents[chain[-1]])
            if steps_taken >= solid:
                total += float(np.linalg.norm(joints[parent] - joints[chain[-1]]))
            chain.append(parent)
            steps_taken += 1
    if len(chain) < 2:
        return np.zeros(len(points))
    nodes = joints[chain]
    starts, ends = nodes[:-1], nodes[1:]
    steps = ends - starts
    length = np.linalg.norm(steps, axis=1)
    arc0 = np.concatenate([[0.0], np.cumsum(length)[:-1]])
    best = np.full(len(points), np.inf)
    arc = np.zeros(len(points))
    for k in range(len(steps)):
        t = np.clip(((points - starts[k]) @ steps[k]) / max(float(length[k]) ** 2, 1e-12), 0.0, 1.0)
        offset = points - (starts[k] + t[:, None] * steps[k])
        if depth < 1.0:  # within the width of the neck the front-back offset counts for less (the stem lies deep in the body)
            side = np.clip((np.abs(offset[:, 0]) - width) / max(ramp - width, 1e-9), 0.0, 1.0)
            offset = offset.copy()
            offset[:, 2] *= depth + (1.0 - depth) * side * side * (3.0 - 2.0 * side)
        distance = np.linalg.norm(offset, axis=1)
        closer = distance < best
        best[closer] = distance[closer]
        arc[closer] = arc0[k] + t[closer] * length[k]
    across = np.clip((best - hold) / max(reach - hold, 1e-9), 0.0, 1.0)
    along = np.ones(len(points))
    if len(steps) > solid:
        start, end = float(length[:solid].sum()), float(length.sum())
        if fade is not None:
            end = start + max(fade, 1e-9)
        u = np.clip((arc - start) / max(end - start, 1e-9), 0.0, 1.0)
        along = 1.0 - u * u * (3.0 - 2.0 * u)
    return (1.0 - across ** 2) ** 2 * along


def carry_body(placed: np.ndarray, followers: np.ndarray, groups: Sequence) -> np.ndarray:
    """``placed`` with the ``followers`` (a bool per point: the body that follows the cage) drawn to the pieces that were put at
    their joint (task #91). ``groups`` is a list of ``(share, target)``: for each piece (or the head) the share each point
    takes (``stem_share``, one value per point) and the place each point would have if it moved with the piece (a shift for
    the head, the piece's own move for another piece; points x 3). A point goes ``share`` of the way from where the cage put it
    to its target, so the neck that reaches the helmet moves with the helmet (the gap between them stays as it was) and the
    body further down follows the cage as before. Several groups: the target is their average, weighted by the shares, and
    the share is the largest of them."""
    out = placed.copy()
    index = np.nonzero(followers)[0]
    if not len(index) or not groups:
        return out
    weight = np.array([np.asarray(share)[index] for share, _ in groups]).T  # (followers, groups)
    targets = np.array([np.asarray(target)[index] for _, target in groups]).transpose(1, 0, 2)  # (followers, groups, 3)
    total = weight.sum(axis=1)
    average = np.divide(np.einsum("ng,ngc->nc", weight, targets), total[:, None], out=placed[index].copy(), where=total[:, None] > 0)
    share = weight.max(axis=1)[:, None]
    out[index] = (1.0 - share) * placed[index] + share * average
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Fingers and soles (task #84)
# ---------------------------------------------------------------------------------------------------------------------

#: The bones of a finger segment, lower case: ``lefthandindex1`` to ``righthandthumb3``.
FINGER_BONE = re.compile(r"^(left|right)hand(thumb|index|middle|ring|pinky)[123]$")


def finger_columns(names: Sequence[str]) -> List[int]:
    """The columns of ``names`` (bone names) that are finger segment bones."""
    return [i for i, name in enumerate(names) if FINGER_BONE.match(name.lower())]


#: How many times the finger weights are averaged with their neighbours' before the segments are blended (``keep_finger_shape``).
FINGER_SMOOTHING = 8


def _smooth_over_edges(values: np.ndarray, edges: np.ndarray, inside: np.ndarray, rounds: int) -> np.ndarray:
    """``values`` (points x k) with each row inside ``inside`` replaced, ``rounds`` times, by the average of itself and the mean
    of its neighbours along ``edges`` (both ends inside)."""
    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    edges = edges[inside[edges[:, 0]] & inside[edges[:, 1]]]
    out = values.copy()
    if not len(edges) or rounds <= 0:
        return out
    degree = np.bincount(edges.ravel(), minlength=len(values)).astype(np.float64)
    has = inside & (degree > 0)
    for _ in range(rounds):
        total = np.zeros_like(out)
        np.add.at(total, edges[:, 0], out[edges[:, 1]])
        np.add.at(total, edges[:, 1], out[edges[:, 0]])
        out[has] = 0.5 * out[has] + 0.5 * total[has] / degree[has, None]
    return out


def keep_finger_shape(points: np.ndarray, caged: np.ndarray, weights: np.ndarray, columns: Sequence[int], lattice: np.ndarray,
                      target: np.ndarray, lattice_weights: np.ndarray, edges: Optional[np.ndarray] = None,
                      smoothing: int = FINGER_SMOOTHING, min_points: int = 3) -> np.ndarray:
    """``caged`` with the finger points moved segment by segment, so a finger keeps its cross-section (task #84).

    Why: the cage lets every lattice point of the wrapped body within 10 cm pull a point. A hand is small and its fingers
    are packed: a point of the middle finger is pulled by the lattice points of the ring and index fingers, and by the
    segments of its own finger that bend differently. On the owner scene that made the middle and ring fingers 20 to 40 %
    wider and up to 15 % thinner (thickness / width 1.05 to 0.67). Each finger segment is a rigid piece instead: the rigid
    transform (rotation and shift, no size) that takes the lattice points of the segment (the wrapped body's points that
    follow the bone most) to the same points of the sample body in the Arma rest, found as for any rigid piece
    (``rigid_fit``). A point goes where the segments it follows would put it, by its own weights; the weight that is not on a
    finger segment (the palm) follows the cage as before, so the knuckles stay joined. The segments stand where the sample's
    segments stand, so the finger's bend and place are the sample's; the size of the segment is the user's.

    ``points`` and ``caged`` are the points before and after the cage, ``weights`` points x bones, ``columns`` the finger
    segment columns, ``lattice``/``target`` the rest and deformed cage lattice (same point order) and ``lattice_weights`` the
    bone weights of the lattice points. A segment with fewer than ``min_points`` lattice points keeps the cage.

    Blending by the raw weights tears the finger at its joints (the edges across a joint came out at 0.36 to 0.5 of their length
    on the owner scene, the cage's own 0.6 to 0.7), because the mesh's weights change from one segment to the next over one or two
    rows of points. With ``edges`` (point pairs of the mesh) the finger weights are first averaged with their neighbours'
    ``smoothing`` times, among the finger points only, so the segments blend over a few rows (0.56 and 0.69 after 8 rounds)."""
    out = caged.copy()
    columns = list(columns)
    if not columns or lattice_weights is None or not len(lattice_weights):
        return out
    heaviest = lattice_weights.argmax(axis=1)
    heaviest[lattice_weights.sum(axis=1) <= 0] = -1
    fits = {}
    for column in columns:
        mine = heaviest == column
        if mine.sum() >= min_points:
            fits[column] = rigid_fit(lattice[mine], target[mine])
    total = weights.sum(axis=1)
    used = (weights[:, columns].sum(axis=1) > 0) & (total > 0)
    if not used.any() or not fits:
        return out
    if edges is not None and smoothing > 0:
        weights = _smooth_over_edges(weights, edges, used, smoothing)
        total = weights.sum(axis=1)
    acc = np.zeros((len(points), 3))
    own = np.zeros(len(points))
    for column, (rotation, c, t) in fits.items():
        w = weights[:, column]
        acc += w[:, None] * (t + (points - c) @ rotation.T)
        own += w
    rest = total - own
    acc += rest[:, None] * caged
    out[used] = acc[used] / total[used, None]
    return out


def sole_roll(points: np.ndarray, axis: np.ndarray, side: int) -> float:
    """The sideways tilt of a sole in degrees, outer edge higher positive: the slope of the lowest point against the sideways
    direction, over the middle 60 % of the foot's length (six slices from the 5th to the 95th percentile across).
    ``points`` are the foot's points, ``axis`` its unit forward axis (``find_feet``), ``side`` +1 for the left foot (+X)
    and -1 for the right. NaN when the foot has too few points."""
    axis = axis / np.linalg.norm(axis)
    across = np.cross([0.0, 1.0, 0.0], axis)
    across /= np.linalg.norm(across)
    if across[0] * side < 0:
        across = -across
    if len(points) < 12:
        return float("nan")
    along = (points - points.mean(axis=0)) @ axis
    mid = points[(along > np.percentile(along, 20)) & (along < np.percentile(along, 80))]
    sideways = (mid - mid.mean(axis=0)) @ across
    edges = np.linspace(np.percentile(sideways, 5), np.percentile(sideways, 95), 7)
    xs, ys = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        part = mid[(sideways >= a) & (sideways < b)]
        if len(part):
            xs.append(0.5 * (a + b))
            ys.append(part[:, 1].min())
    if len(xs) < 2:
        return float("nan")
    return float(np.degrees(np.arctan(np.polyfit(xs, ys, 1)[0])))


#: A foot rolled more than this (degrees) is not a standing foot; ``keep_sole_tilt`` leaves it alone.
MAX_ROLL = 20.0


def keep_sole_tilt(points: np.ndarray, placed: np.ndarray, share: np.ndarray, side: int, body: Optional[np.ndarray] = None,
                   front: float = 1.0, tries: int = 3) -> np.ndarray:
    """``placed`` with the foot turned back about its forward axis by the roll that the move added (task #84).

    Both soles lie flat on the wrapped body and on the sample body, but the points of the wrapped foot correspond to the
    sample's with a roll of 3 to 4 degrees about the foot (the best rigid fit of the wrapped sole band to the sample's has it,
    and the local turns of the cage add up to the same), so the cage rolled the owner's feet onto their outer edges by 5 to 6
    degrees. The roll is measured by ``sole_roll`` on the foot points (``share`` > 0.5: the weight on the foot and toe-base
    bones of this ``side``) before and after, and the foot is turned about its forward axis, through the middle of its sole,
    by the difference. ``share`` (0 to 1 per point) scales the turn, so the ankle and the shin above it follow smoothly.
    Pitch and toe-out do not change (the turn is about the axis of the foot). The measure is repeated up to ``tries`` times.
    ``body`` (a bool per point; default all) names the points ``find_feet`` looks at: the feet are found on the whole standing
    body, as for the digests."""
    out = placed.copy()
    body = np.ones(len(points), dtype=bool) if body is None else np.asarray(body, dtype=bool)
    foot = (share > 0.5) & body
    if foot.sum() < 12:
        return out
    before_feet = find_feet(points[body], front)
    if side not in before_feet:
        return out
    wanted = sole_roll(points[foot], before_feet[side].axis, side)
    if not np.isfinite(wanted) or abs(wanted) > MAX_ROLL:  # not a standing body: nothing to keep
        return out
    for _ in range(tries):
        feet = find_feet(out[body], front)
        if side not in feet:
            break
        axis = feet[side].axis / np.linalg.norm(feet[side].axis)
        error = sole_roll(out[foot], feet[side].axis, side) - wanted
        if not np.isfinite(error) or abs(error) < 0.1 or abs(error) > 2.0 * MAX_ROLL:
            break
        pivot = out[foot].mean(axis=0)
        pivot[1] = feet[side].low
        moving = share > 0
        # A turn by +a about the forward axis (+Z) lifts +X: the outer edge of the left foot (side +1), the inner edge of the
        # right foot. So the turn that takes ``error`` degrees of outward roll away is -error on the left and +error on the right.
        angle = -np.radians(error) * side * share[moving]
        c, s = np.cos(angle), np.sin(angle)
        v = out[moving] - pivot
        k = axis
        # Rodrigues about the axis k, one angle per point
        kv = np.cross(k, v)
        kd = (v @ k)[:, None] * k
        out[moving] = pivot + v * c[:, None] + kv * s[:, None] + kd * (1.0 - c)[:, None]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Loose accessories (task #82, D41, D44)
# ---------------------------------------------------------------------------------------------------------------------

def surface_follow(points: np.ndarray, rest: np.ndarray, deformed: np.ndarray, polys, radius: float = SMOOTH_RADIUS,
                   chunk: int = 64) -> np.ndarray:
    """Loose accessory points that follow a surface at their own distance (task #82). ``rest`` is the surface the points sit
    on (the sample body wrapped onto the user's body) and ``deformed`` the same surface moved (the sample body in the Arma
    rest pose); same points, same ``polys``. Each point finds its closest point ``c`` on ``rest`` (a place in a triangle) and
    keeps its offset ``x - c`` there, turned with the frame of that place: the rotation of the three corners that the cage
    uses (``lattice_transforms``), blended by the place's barycentric weights and made a rotation again. On ``deformed`` the
    point goes to the same place ``c'`` of the same triangle: ``x' = c' + (x - c) R``. The offset along the normal is kept
    (a rotation does not change a length), and the frame is smooth, so neighbouring points of a piece turn alike and keep
    their distances as far as the surface under them does not stretch: the piece is carried by the body, not bent by it.

    The surface itself is stretched and squashed where the sample's proportions differ from the user's (hands, collar). A
    piece that sat tight on such a surface would be stretched with it, so every point takes the average of the moves of the
    loose points within ``radius`` of it, each applied to this point (weight ``(1 - (d / radius)^2)^2``, as the cage): the
    piece follows the body at the scale of the body and not at the scale of a few vertices, its edges keep their lengths, and
    where the surface only turns and moves, all the moves are the same and the average is exact."""
    points, rest, deformed = (np.asarray(a, dtype=np.float64) for a in (points, rest, deformed))
    tris = np.array([(ring[0], ring[k], ring[k + 1]) for ring in polys for k in range(1, len(ring) - 1)],
                    dtype=np.int64).reshape(-1, 3)
    xform = lattice_transforms(rest, deformed, polys)
    tri, bary, _ = closest_points(points, rest, tris)
    corner = tris[tri]
    c = (bary[:, :, None] * rest[corner]).sum(1)
    c2 = (bary[:, :, None] * deformed[corner]).sum(1)
    turn = _rotation_of((bary[:, :, None, None] * xform[corner]).sum(1))
    if radius <= 0.0 or len(points) < 2:
        return c2 + np.einsum("na,nab->nb", points - c, turn)
    out = np.empty_like(points)
    for start in range(0, len(points), chunk):
        p = points[start:start + chunk]
        d2 = ((p[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)
        weight = np.where(d2 < radius * radius, (1.0 - d2 / (radius * radius)) ** 2, 0.0)  # a point counts for itself
        # where each neighbour's own move (its place on the surface and its turn) would put this point
        there = c2[None, :, :] + np.einsum("mna,nab->mnb", p[:, None, :] - c[None, :, :], turn)
        out[start:start + chunk] = np.einsum("mn,mnb->mb", weight, there) / weight.sum(axis=1, keepdims=True)
    return out

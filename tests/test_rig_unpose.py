"""The unpose maths of ``a3ob_core.rig.unpose`` (task #67): plain numpy on a made-up skeleton; runs under CPython (numpy
1.26) and hython (numpy 2.x). The tests on the real Samples character are in ``test_node_unpose``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import bone_frames, bone_moves, unpose_points

#: A small skeleton: a root, a spine going up to a head, and an arm going out to the side (with a hand at its end).
PARENTS = [-1, 0, 1, 2, 1, 4, 5]
REST = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 2.0, 0.0], [0.0, 3.0, 0.0],
                 [0.5, 1.9, 0.0], [1.5, 1.9, 0.0], [2.2, 1.9, 0.0]])


def rotation_z(degrees):
    a = np.radians(degrees)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


def pose(degrees, along, size=1.0, move=(0.0, 0.0, 0.0)):
    """``(joint positions, 4x4 skin matrices)`` of the rest skeleton posed: a local rotation about Z for each joint, the
    body ``size`` everywhere and, for each joint, ``along[j]`` times longer along its bone (a longer bone is not a thicker
    one). A chain end has no bone: it takes the body size only."""
    count = len(PARENTS)
    ends = {3, 6}
    axes = bone_frames(PARENTS, REST)[:, :, 0]
    world, stretch, pos, mats = [None] * count, [None] * count, np.zeros((count, 3)), np.zeros((count, 4, 4))
    for j in range(count):
        parent = PARENTS[j]
        r = size if j in ends else along[j] * size
        stretch[j] = size * np.eye(3) + (r - size) * np.outer(axes[j], axes[j])
        if parent < 0:
            world[j], pos[j] = rotation_z(degrees[j]), REST[j] + np.asarray(move)
        else:
            world[j] = world[parent] @ rotation_z(degrees[j])
            pos[j] = pos[parent] + world[parent] @ stretch[parent] @ (REST[j] - REST[parent])
        linear = world[j] @ stretch[j]
        mats[j, :3, :3] = linear
        mats[j, :3, 3] = pos[j] - linear @ REST[j]
        mats[j, 3, 3] = 1.0
    return pos, mats


def mesh():
    """Points round the skeleton, with weights between neighbouring bones, and the rows of their weights."""
    rng = np.random.default_rng(5)
    points = rng.uniform(-0.3, 0.3, (60, 3)) + REST[rng.integers(0, len(REST), 60)]
    weights = np.zeros((60, len(REST)))
    for n in range(60):
        j = int(rng.integers(1, len(REST)))
        t = rng.uniform()
        weights[n, j], weights[n, PARENTS[j]] = t, 1.0 - t
    return points, weights


def skin(points, weights, mats):
    blend = np.einsum("nb,bij->nij", weights, mats)
    return np.einsum("nij,nj->ni", blend[:, :3, :3], points) + blend[:, :3, 3]


class TestBoneMoves(unittest.TestCase):
    def test_a_perfect_fit_moves_nothing(self):
        moves = bone_moves(PARENTS, REST, REST.copy())
        np.testing.assert_allclose(moves.rotation, np.tile(np.eye(3), (len(REST), 1, 1)), atol=1e-12)
        np.testing.assert_allclose(moves.along, 1.0, atol=1e-12)
        self.assertEqual(moves.size, 1.0)
        points, weights = mesh()
        np.testing.assert_allclose(unpose_points(points, weights, moves), points, atol=1e-12)

    def test_the_stored_frames_of_the_skeletons_do_not_matter(self):
        # task #76: A' has aimed frames, A keeps its own. Only the joint positions are read, so a turned Arma frame is no move
        frames = bone_frames(PARENTS, REST)
        turned = np.einsum("jab,bc->jac", frames, rotation_z(37.0))  # a different convention for the same joints
        self.assertGreater(float(np.abs(turned - frames).max()), 0.1)
        moves = bone_moves(PARENTS, REST, REST.copy())
        np.testing.assert_allclose(moves.rotation, np.tile(np.eye(3), (len(REST), 1, 1)), atol=1e-12)

    def test_a_pose_with_bone_lengths_is_taken_back_exactly(self):
        degrees = [10.0, 5.0, -8.0, 0.0, -30.0, 25.0, 0.0]
        along = [1.0, 1.0, 1.0, 1.0, 1.10, 1.10, 1.0]
        fitted, mats = pose(degrees, along, size=1.05, move=(0.2, -0.1, 0.3))
        points, weights = mesh()
        moves = bone_moves(PARENTS, REST, fitted)
        np.testing.assert_allclose(moves.along[[4, 5]], 1.10 * 1.05, atol=1e-9)  # the arm bones are longer along themselves
        self.assertAlmostEqual(moves.size, 1.05)  # the body size: the median of the bone length ratios
        posed = skin(points, weights, mats)
        self.assertGreater(float(np.linalg.norm(posed - points, axis=1).max()), 0.1)
        np.testing.assert_allclose(unpose_points(posed, weights, moves), points, atol=1e-9)

    def test_a_point_with_no_weight_follows_the_closest_weighted_point(self):
        fitted, mats = pose([0.0, 0.0, 0.0, 0.0, -30.0, 0.0, 0.0], [1.0] * 7)
        moves = bone_moves(PARENTS, REST, fitted)
        points = np.array([[0.9, 1.9, 0.0], [1.0, 1.95, 0.0]])
        weights = np.array([[0, 0, 0, 0, 0, 1.0, 0], [0, 0, 0, 0, 0, 0, 0]])
        out = unpose_points(skin(points, np.array([[0, 0, 0, 0, 0, 1.0, 0]] * 2), mats), weights, moves)
        np.testing.assert_allclose(out, points, atol=1e-9)


class TestRigidPieces(unittest.TestCase):
    def test_a_piece_moves_by_one_matrix_and_keeps_its_edges(self):
        fitted, mats = pose([0.0, 0.0, 0.0, 0.0, -20.0, 60.0, 0.0], [1.0] * 7)
        moves = bone_moves(PARENTS, REST, fitted)
        rng = np.random.default_rng(2)
        cuff = np.array([1.5, 1.9, 0.0]) + rng.uniform(-0.1, 0.1, (30, 3))
        t = rng.uniform(0.2, 0.8, 30)  # per-point weights between the upper arm and the forearm bone: they stretch the cuff
        weights = np.zeros((30, len(REST)))
        weights[:, 4], weights[:, 5] = t, 1.0 - t
        # the posed cuff is rigid on bone 5 (its truth), with the per-point weights of a first pass in front of it
        posed = skin(cuff, np.tile(np.eye(len(REST))[5], (30, 1)), mats)
        piece = np.zeros(30, dtype=np.int64)
        loose = unpose_points(posed, weights, moves)
        fixed = unpose_points(posed, weights, moves, piece)
        edges = np.array([(a, b) for a in range(0, 30, 3) for b in range(1, 30, 4) if a != b])

        def length_ratio(rest):
            return np.linalg.norm(rest[edges[:, 0]] - rest[edges[:, 1]], axis=1) / np.linalg.norm(
                cuff[edges[:, 0]] - cuff[edges[:, 1]], axis=1)

        self.assertGreater(float(np.abs(length_ratio(loose) - 1).max()), 0.01)  # per-point weights stretch it
        ratio = length_ratio(fixed)
        self.assertLess(float(np.abs(ratio - ratio[0]).max()), 1e-9)  # one matrix: every edge changes by the same factor
        # a point outside every piece (-1) keeps its own weights
        piece[:15] = -1
        mixed = unpose_points(posed, weights, moves, piece)
        np.testing.assert_allclose(mixed[:15], loose[:15], atol=1e-12)


if __name__ == "__main__":
    unittest.main()

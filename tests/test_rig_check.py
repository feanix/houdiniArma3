"""The measures of the Rig Check node (task #68; design tests 2.6, 2.7 and the volume test). Plain numpy, no Houdini.

Purpose: made-up cases with known answers for the problem attributes, and a bent two-bone cylinder for the stress (stretch
outside, compression inside) and for the volume (loss inside the bend, growing with the angle, none at zero bend).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import check, normalised_weights


def cylinder(length=2.0, radius=0.1, rings=21, segments=12):
    """A capped tube along +X from 0 to ``length``. Returns (points, triangles, ring number of every point)."""
    pts, ring_of = [], []
    for r in range(rings):
        x = length * r / (rings - 1)
        for s in range(segments):
            angle = 2 * np.pi * s / segments
            pts.append((x, radius * np.sin(angle), radius * np.cos(angle)))
            ring_of.append(r)
    tris = []
    for r in range(rings - 1):
        for s in range(segments):
            a, b = r * segments + s, r * segments + (s + 1) % segments
            c, d = a + segments, b + segments
            tris += [(a, b, c), (b, d, c)]
    for r, x, flip in ((0, 0.0, True), (rings - 1, length, False)):
        centre = len(pts)
        pts.append((x, 0.0, 0.0))
        ring_of.append(r)
        for s in range(segments):
            a, b = r * segments + s, r * segments + (s + 1) % segments
            tris.append((a, centre, b) if flip else (a, b, centre))
    return np.array(pts), np.array(tris), np.array(ring_of)


def sphere(radius=1.0, rings=12, segments=16):
    pts = [(0.0, radius, 0.0)]
    for i in range(1, rings):
        t = np.pi * i / rings
        for s in range(segments):
            p = 2 * np.pi * s / segments
            pts.append((radius * np.sin(t) * np.cos(p), radius * np.cos(t), radius * np.sin(t) * np.sin(p)))
    pts.append((0.0, -radius, 0.0))
    tris = [(0, 1 + (s + 1) % segments, 1 + s) for s in range(segments)]
    for i in range(rings - 2):
        for s in range(segments):
            a, b = 1 + i * segments + s, 1 + i * segments + (s + 1) % segments
            tris += [(a, b, a + segments), (b, b + segments, a + segments)]
    last, base = len(pts) - 1, 1 + (rings - 2) * segments
    tris += [(base + s, base + (s + 1) % segments, last) for s in range(segments)]
    return np.array(pts), np.array(tris)


class TestProblems(unittest.TestCase):
    def test_one_point_with_five_bones_has_five_influences_and_the_sum_error_and_jump_are_known(self):
        w = np.array([[0.2] * 5 + [0.0], [1.0, 0, 0, 0, 0, 0], [0.5, 0.5, 0, 0, 0, 0], [0.45, 0.45, 0, 0, 0, 0], [0.0] * 6])
        edges = np.array([[1, 2], [2, 3]])
        jump, infl, sumerr, dominant = check.weight_problems(w, edges)
        self.assertEqual(infl.tolist(), [5, 1, 2, 2, 0])
        np.testing.assert_allclose(sumerr, [0.0, 0.0, 0.0, 0.1, check.NO_WEIGHT], atol=1e-12)
        np.testing.assert_allclose(jump, [0.0, 0.5, 0.5, 0.05, 0.0], atol=1e-12)  # 1|2: half of 0.5+0.5; 2|3: half of 0.1
        self.assertEqual(dominant.tolist(), [0, 0, 0, 0, -1])

    def test_completely_different_neighbours_have_a_jump_of_1(self):
        jump = check.weight_problems(np.array([[1.0, 0.0], [0.0, 1.0]]), np.array([[0, 1]]))[0]
        np.testing.assert_allclose(jump, [1.0, 1.0])

    def test_a_piece_whose_points_favour_two_bones_has_the_right_straddle(self):
        dominant = np.array([0] * 7 + [1] * 3 + [2, 2])
        piece = np.array([4] * 10 + [-1, -1])
        value = check.straddle(dominant, piece)
        np.testing.assert_allclose(value[:10], 0.3)
        np.testing.assert_allclose(value[10:], 0.0)

    def test_a_joint_outside_a_sphere_has_a_negative_depth_and_one_inside_a_positive_depth(self):
        points, tris = sphere()
        depth = check.joint_depth(np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [0.5, 0.0, 0.0]]), points, tris)
        self.assertAlmostEqual(depth[0], 1.0, delta=0.03)
        self.assertAlmostEqual(depth[1], -2.0, delta=0.03)
        self.assertAlmostEqual(depth[2], 0.5, delta=0.03)

    def test_a_flipped_mesh_gives_the_same_depth(self):
        points, tris = sphere()
        depth = check.joint_depth(np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0]]), points, tris[:, ::-1])
        self.assertGreater(depth[0], 0.9)
        self.assertLess(depth[1], -1.9)


class TestPoses(unittest.TestCase):
    NAMES = ["Pelvis", "LeftArm", "LeftForeArm", "LeftHand"]
    PARENTS = [-1, 0, 1, 2]
    REST = np.array([[0.0, 1.0, 0.0], [0.2, 1.4, 0.0], [0.5, 1.4, 0.0], [0.8, 1.4, 0.0]])

    def test_arms_raised_turns_the_arm_up_about_the_shoulder(self):
        local, used = check.pose_rotations("arms_up", self.NAMES, self.PARENTS, self.REST)
        rotation, position = check.pose_matrices(self.PARENTS, self.REST, local)
        self.assertEqual(used, ["LeftArm"])
        np.testing.assert_allclose(position[3], [0.2, 2.0, 0.0], atol=1e-9)
        np.testing.assert_allclose(position[0], self.REST[0])

    def test_a_pose_for_bones_the_skeleton_lacks_moves_nothing(self):
        local, used = check.pose_rotations("knees", self.NAMES, self.PARENTS, self.REST)
        self.assertEqual(used, [])
        np.testing.assert_allclose(local, np.tile(np.eye(3), (4, 1, 1)))

    def test_every_test_pose_is_a_pure_rotation_so_linear_skinning_of_one_bone_keeps_lengths(self):
        local, _ = check.pose_rotations("arms_forward", self.NAMES, self.PARENTS, self.REST)
        rotation, position = check.pose_matrices(self.PARENTS, self.REST, local)
        points = self.REST[1:] + 0.05
        w = np.zeros((3, 4))
        w[:, 1] = 1.0
        posed = check.pose_skin(points, w, self.REST, rotation, position)
        np.testing.assert_allclose(np.linalg.norm(posed[0] - posed[1]), np.linalg.norm(points[0] - points[1]))


class TestBentCylinder(unittest.TestCase):
    """Two bones, plain linear weights along the length, bent at the middle: the design test 2.7 and the volume test."""

    def setUp(self):
        self.points, self.tris, self.ring = cylinder()
        self.edges = check.edges_of(self.tris)
        self.rest = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        self.parents = [-1, 0]
        wb = np.clip((self.points[:, 0] - 0.8) / 0.4, 0.0, 1.0)  # linear across the elbow, 0.4 long
        self.w = normalised_weights(self.points, np.stack([1.0 - wb, wb], axis=1))
        self.middle = np.nonzero((self.ring == 10) & (np.arange(len(self.points)) < 21 * 12))[0]
        self.inside = self.middle[self.points[self.middle, 1] > 0.09]   # +Y is the inside of a bend towards +Y
        self.outside = self.middle[self.points[self.middle, 1] < -0.09]

    def bend(self, degrees):
        local = np.tile(np.eye(3), (2, 1, 1))
        angle = np.radians(degrees)
        local[1] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
        rotation, position = check.pose_matrices(self.parents, self.rest, local)
        posed = check.pose_skin(self.points, self.w, self.rest, rotation, position)
        stress = check.edge_stress(self.points, posed, self.edges)
        volume = check.volume_change(self.points, posed, self.tris, self.w, self.rest, rotation, position)
        return stress, volume

    def test_design_2_7_stretch_on_the_outside_and_compression_on_the_inside(self):
        stress, _ = self.bend(60.0)
        self.assertGreater(float(stress[self.outside].mean()), 0.02)
        self.assertLess(float(stress[self.inside].mean()), -0.02)

    def test_volume_is_lost_at_the_inside_of_the_bend_and_the_loss_grows_with_the_angle(self):
        loss = []
        for degrees in (0.0, 30.0, 60.0, 90.0):
            _, volume = self.bend(degrees)
            loss.append(float(volume[self.inside].mean()))
        print("   volume change inside the bend at 0, 30, 60, 90 degrees:", ", ".join("%.3f" % v for v in loss))
        self.assertAlmostEqual(loss[0], 0.0, places=9)
        self.assertLess(loss[1], -0.005)
        self.assertLess(loss[2], loss[1])
        self.assertLess(loss[3], loss[2])

    def test_no_bend_changes_no_volume_anywhere(self):
        stress, volume = self.bend(0.0)
        self.assertLess(float(np.abs(volume).max()), 1e-9)
        self.assertLess(float(np.abs(stress).max()), 1e-9)

    def test_the_inside_loses_more_than_the_outside(self):
        _, volume = self.bend(90.0)
        self.assertLess(float(volume[self.inside].mean()), float(volume[self.outside].mean()))

    def test_the_joints_with_the_worst_loss_are_named_with_the_bones_that_hold_the_weights(self):
        _, volume = self.bend(90.0)
        found = check.worst_joints(volume, self.w)
        self.assertTrue(found)
        self.assertLess(found[0][1], -0.01)


class TestRamps(unittest.TestCase):
    def test_every_show_entry_has_a_ramp_with_stops_from_0_to_1(self):
        for show in check.RAMPS:
            stops, low, high = check.ramp_for(show, 0.3, 40)
            self.assertLess(low, high, show)
            positions = [p for p, _ in stops]
            self.assertEqual(positions, sorted(positions), show)
            self.assertTrue(all(0.0 <= p <= 1.0 for p in positions), show)

    def test_volume_colours_are_not_the_stress_colours(self):
        stress = check.ramp_for("stress", 0.3, 1)[0]
        volume = check.ramp_for("volume", 0.3, 1)[0]
        self.assertNotEqual([c for _, c in stress], [c for _, c in volume])
        self.assertNotEqual(stress[0][1], volume[0][1])
        self.assertNotEqual(stress[-1][1], volume[-1][1])


if __name__ == "__main__":
    unittest.main()

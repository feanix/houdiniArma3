"""The rig fit maths of ``a3ob_core.rig.fit`` and ``a3ob_core.rig.landmarks`` (design tests 2.3 and 2.4, task #66). Plain
numpy on made-up bodies; runs under CPython (numpy 1.26) and hython (numpy 2.x). The test on the real Samples character
is in ``test_node_rig_fit``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import (LANDMARK_NAMES, body_scale, facing_sign, find_groin, fit_skeleton, joint_targets,
                           skin_points, snap_to_surface, swing, symmetrize)
from tests.test_rig_transfer import tube


def rotation_z(degrees):
    a = np.radians(degrees)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


class TestSwing(unittest.TestCase):
    def test_turns_a_onto_b_and_handles_equal_and_opposite(self):
        a, b = np.array([0.0, 1.0, 0.0]), np.array([1.0, 1.0, 0.0])
        np.testing.assert_allclose(swing(a, b) @ a, b / np.linalg.norm(b), atol=1e-12)
        np.testing.assert_allclose(swing(a, 3 * a), np.eye(3), atol=1e-12)
        np.testing.assert_allclose(swing(a, -a) @ a, -a, atol=1e-12)
        self.assertAlmostEqual(float(np.linalg.det(swing(a, -a))), 1.0)


class TestFitSkeleton(unittest.TestCase):
    def test_a_chain_is_turned_and_given_the_fitted_lengths(self):
        rest = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 2.0, 0.0], [0.0, 3.0, 0.0]])
        targets = {0: np.array([1.0, 1.0, 0.0]), 2: np.array([1.0, 3.5, 0.0]), 3: np.array([2.5, 4.5, 0.0])}
        fit = fit_skeleton([-1, 0, 1, 2], rest, targets, "bone")
        for joint, target in targets.items():
            np.testing.assert_allclose(fit.positions[joint], target, atol=1e-12)
        # joint 1 has no landmark: it lies on the way from joint 0 to joint 2, scaled with its bone
        np.testing.assert_allclose(fit.positions[1], [1.0, 2.25, 0.0], atol=1e-12)
        self.assertAlmostEqual(float(fit.scales[0]), 2.5 / 2.0)
        self.assertEqual(max(fit.residual.values()) < 1e-9, True)

    def test_two_children_get_the_best_rotation_and_the_misfit_is_reported(self):
        rest = np.array([[0.0, 0.0, 0.0], [-1.0, -1.0, 0.0], [1.0, -1.0, 0.0]])
        turned = [1.2 * rotation_z(30) @ p for p in rest]
        fit = fit_skeleton([-1, 0, 0], rest, {0: turned[0], 1: turned[1], 2: turned[2]}, "bone")
        np.testing.assert_allclose(fit.positions, turned, atol=1e-9)
        np.testing.assert_allclose(fit.linear[0], 1.2 * rotation_z(30), atol=1e-9)
        # a child that is 5 cm from where the rigid bone puts it: the distance is the quality number
        off = dict(enumerate(turned))
        off[2] = turned[2] + np.array([0.0, 0.05, 0.0])
        fit = fit_skeleton([-1, 0, 0], rest, off, "bone")
        self.assertGreater(fit.residual[2], 0.01)
        np.testing.assert_allclose(fit.positions[2], off[2], atol=1e-12)  # snapped on the landmark all the same

    def test_whole_body_mode_keeps_one_size_and_does_not_snap(self):
        rest = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 2.0, 0.0]])
        targets = {0: np.array([0.0, 0.0, 0.0]), 1: np.array([0.0, 1.5, 0.0]), 2: np.array([0.0, 3.0, 0.0])}
        fit = fit_skeleton([-1, 0, 1], rest, targets, "body", size=1.5)
        np.testing.assert_allclose(fit.scales, 1.5)
        np.testing.assert_allclose(fit.positions[:, 1], [0.0, 1.5, 3.0], atol=1e-12)
        targets[1] = np.array([0.0, 1.8, 0.0])  # the landmark is 3 cm off the whole-body size: shown, not hidden
        fit = fit_skeleton([-1, 0, 1], rest, targets, "body", size=1.5)
        self.assertAlmostEqual(fit.residual[1], 0.3, places=9)
        self.assertAlmostEqual(float(fit.positions[1][1]), 1.5)

    def test_the_torso_targets_of_an_unchanged_body_are_the_rest_positions(self):
        names = ["Pelvis", "Spine", "Spine1", "Spine2", "Spine3", "neck", "neck1", "head", "LeftArm", "RightArm",
                 "LeftUpLeg", "RightUpLeg"]
        rest = np.array([[0, 0, 0], [0, 0, 0], [0, 0.15, 0], [0, 0.28, 0], [0, 0.43, 0], [0, 0.55, 0], [0, 0.62, 0],
                         [0, 0.66, 0], [0.2, 0.47, 0], [-0.2, 0.47, 0], [0.11, -0.06, 0], [-0.11, -0.06, 0]], dtype=float)
        index = {n.lower(): i for i, n in enumerate(names)}
        sample = {"lm_hip_l": rest[10], "lm_hip_r": rest[11], "lm_shoulder_l": rest[8], "lm_shoulder_r": rest[9],
                  "lm_groin": np.array([0.0, -0.27, 0.0]), "lm_chin": np.array([0.0, 0.61, 0.07])}
        bones = {"lm_hip_l": "LeftUpLeg", "lm_hip_r": "RightUpLeg", "lm_shoulder_l": "LeftArm", "lm_shoulder_r": "RightArm",
                 "lm_groin": "", "lm_chin": ""}
        targets = joint_targets(index, rest, sample, sample, bones)
        for name in names:
            np.testing.assert_allclose(targets[index[name.lower()]], rest[index[name.lower()]], atol=1e-9, err_msg=name)
        # the same body 10 % bigger and moved: the targets follow
        bigger = {k: 1.1 * v + np.array([0.3, 0.2, 0.1]) for k, v in sample.items()}
        targets = joint_targets(index, rest, bigger, sample, bones)
        np.testing.assert_allclose(targets[index["head"]], 1.1 * rest[index["head"]] + [0.3, 0.2, 0.1], atol=1e-9)
        self.assertAlmostEqual(body_scale(bigger, sample), 1.1)


class TestSkinPoints(unittest.TestCase):
    def test_each_joint_moves_its_points_and_a_blend_is_halfway(self):
        rest = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        fit = fit_skeleton([-1, 0], rest, {0: np.array([0.0, 1.0, 0.0]), 1: np.array([0.0, 3.0, 0.0])}, "bone")
        points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.5, 0.0, 0.0]])
        weights = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
        moved = skin_points(points, weights, rest, fit)
        np.testing.assert_allclose(moved[0], [0.0, 1.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(moved[1], [0.0, 3.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(moved[2], 0.5 * (fit.linear[0] @ [0.5, 0, 0] + [0, 1, 0]) + 0.5 * (
            fit.linear[1] @ [-0.5, 0, 0] + [0, 3, 0]), atol=1e-12)

    def test_a_point_with_no_weight_follows_the_closest_weighted_point(self):
        rest = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        fit = fit_skeleton([-1, 0], rest, {0: np.array([0.0, 0.0, 5.0]), 1: np.array([10.0, 0.0, 5.0])}, "bone")  # a move
        points = np.array([[0.0, 0.0, 0.0], [9.0, 0.0, 0.0], [8.5, 1.0, 0.0]])
        weights = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
        moved = skin_points(points, weights, rest, fit)
        np.testing.assert_allclose(moved[2] - points[2], moved[1] - points[1], atol=1e-12)


def legs_and_torso():
    """Two legs 0.9 high, 0.1 either side of the middle, and a torso on top (a made-up body, 1.6 high with a 'head' tube)."""
    left, tris = tube(10, 12, 0.07, 0.9, 0.0)
    right = left.copy()
    left = left + [0.1, 0.0, 0.0]
    right = right + [-0.1, 0.0, 0.0]
    torso, torso_tris = tube(8, 16, 0.2, 0.7, 0.9, first_point=2 * len(left))
    P = np.concatenate([left, right, torso])
    all_tris = np.concatenate([tris, tris + len(left), torso_tris])
    return P, all_tris


class TestLandmarkHelpers(unittest.TestCase):
    def test_the_crotch_is_where_the_legs_meet_and_a_single_tube_has_none(self):
        P, _ = legs_and_torso()
        point, y = find_groin(P, float(P[:, 1].min()), float(P[:, 1].max()), 0.0)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(y, 0.9, delta=0.04)
        one, _ = tube(20, 16, 0.2, 1.6)
        self.assertIsNone(find_groin(one, 0.0, 1.6, 0.0)[0])

    def test_feet_that_point_forward_or_back(self):
        sole, _ = tube(3, 12, 0.1, 0.02)
        shin, _ = tube(8, 12, 0.05, 0.4, 0.0)
        P = np.concatenate([sole + [0.0, 0.0, 0.1], shin])
        self.assertEqual(facing_sign(P), 1.0)
        P = np.concatenate([sole - [0.0, 0.0, 0.1], shin])
        self.assertEqual(facing_sign(P), -1.0)

    def test_symmetry_averages_groups_mirrors_a_single_group_and_centres_the_skin_points(self):
        points = {"lm_knee_l": np.array([0.20, 0.5, 0.0]), "lm_knee_r": np.array([-0.10, 0.5, 0.1]),
                  "lm_hip_l": np.array([0.12, 0.9, 0.0]), "lm_hip_r": np.array([-0.30, 0.8, 0.0]),
                  "lm_groin": np.array([0.03, 0.8, 0.0])}
        out = symmetrize(points, ["lm_knee_l", "lm_knee_r", "lm_hip_l"])
        # the middle plane is the mean of the pair middles: the knees 0.05, the hips -0.09
        mid = (0.05 + (-0.09)) / 2
        np.testing.assert_allclose(out["lm_knee_l"][0] + out["lm_knee_r"][0], 2 * mid, atol=1e-12)
        np.testing.assert_allclose(out["lm_knee_l"][1:], [0.5, 0.05], atol=1e-12)  # averaged
        np.testing.assert_allclose(out["lm_hip_l"], points["lm_hip_l"], atol=1e-12)  # the only given one stays
        np.testing.assert_allclose(out["lm_hip_r"], [2 * mid - 0.12, 0.9, 0.0], atol=1e-12)  # its mirror
        self.assertAlmostEqual(float(out["lm_groin"][0]), mid)

    def test_skin_landmarks_snap_to_the_surface_and_joints_do_not(self):
        P, tris = tube(10, 16, 0.5, 1.0)
        out = snap_to_surface({"lm_chin": np.array([0.0, 0.5, 0.2]), "lm_knee_l": np.array([0.0, 0.5, 0.2])}, P, tris)
        self.assertAlmostEqual(float(np.linalg.norm(out["lm_chin"][[0, 2]])), 0.5, delta=0.02)
        np.testing.assert_allclose(out["lm_knee_l"], [0.0, 0.5, 0.2])

    def test_the_help_names_every_landmark_group(self):
        from a3ob_houdini.docs_common import NODE_INTRO
        text = " ".join(NODE_INTRO["rig_fit"])
        for name in LANDMARK_NAMES:
            self.assertIn(name, text)

    def test_every_landmark_has_a_name_and_a_side(self):
        self.assertEqual(len(LANDMARK_NAMES), 14)
        for name in LANDMARK_NAMES:
            self.assertTrue(name.startswith("lm_"))
            if name.endswith("_l"):
                self.assertIn(name[:-1] + "r", LANDMARK_NAMES)


if __name__ == "__main__":
    unittest.main()

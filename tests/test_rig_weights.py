"""The weight maths of ``a3ob_core.rig.weights`` (design test 1.6, task #62). Plain numpy; runs under CPython and hython.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import (GRID, capture_lists, clean_weights, grid_units, inverse_rest_floats, match_bone_names,
                           matrix_from_capture)


class TestNames(unittest.TestCase):
    def test_match_ignores_case_and_leaves_other_names(self):
        matches, clashes = match_bone_names(["Pelvis", "head", "Arm"], ["Head", "Component01", "ARM", "pelvis"])
        self.assertEqual(matches, {0: "pelvis", 1: "Head", 2: "ARM"})
        self.assertEqual(clashes, [])

    def test_two_spellings_of_one_bone_clash(self):
        matches, clashes = match_bone_names(["head"], ["Head", "head"])
        self.assertEqual(matches, {})
        self.assertEqual(clashes, [("Head", "head")])


class TestCaptureLists(unittest.TestCase):
    def test_lists_are_heaviest_first_and_skip_zeros(self):
        w = np.array([[0.25, 0.0, 0.75], [0.0, 0.0, 0.0], [0.5, 0.5, 0.0]])
        indices, data = capture_lists(w)
        self.assertEqual(indices, [[2, 0], [], [0, 1]])
        self.assertEqual(data, [[0.75, 0.25], [], [0.5, 0.5]])
        back = matrix_from_capture(indices, data, 3)
        np.testing.assert_array_equal(back, w)

    def test_padding_and_repeats(self):
        back = matrix_from_capture([[1, -1, 1]], [[0.25, 1.0, 0.5]], 2)
        np.testing.assert_array_equal(back, [[0.0, 0.75]])

    def test_inverse_rest_has_the_negated_position_in_the_last_row(self):
        floats = inverse_rest_floats((1.0, 2.0, 3.0))
        self.assertEqual(len(floats), 20)
        self.assertEqual(floats[:16], [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -1, -2, -3, 1])
        self.assertEqual(floats[16:], [1.0, 1.0, 1.0, 1.0])

    def test_inverse_rest_with_a_rotation_undoes_the_rest_matrix(self):
        c, s = np.cos(0.5), np.sin(0.5)
        rotation = (c, s, 0.0, -s, c, 0.0, 0.0, 0.0, 1.0)
        inverse = np.array(inverse_rest_floats((1.0, 2.0, 3.0), rotation)[:16]).reshape(4, 4)
        rest = np.eye(4)
        rest[:3, :3] = np.array(rotation).reshape(3, 3)
        rest[3, :3] = (1.0, 2.0, 3.0)
        np.testing.assert_allclose(rest @ inverse, np.eye(4), atol=1e-12)


def one_row(values):
    return np.array([values], dtype=np.float64)


class TestCleanWeights(unittest.TestCase):
    def check_exact(self, new):
        """Every row is empty or sums to exactly 254 units, all on the grid."""
        units = grid_units(new)
        np.testing.assert_allclose(new * GRID, units, atol=1e-9)
        for row in units:
            self.assertIn(int(row.sum()), (0, GRID))

    def test_design_test_1_6_cases(self):
        rows = [
            [0.3, 0.25, 0.2, 0.15, 0.1, 0.0, 0.0],        # 5 bones, sum 1
            [0.2, 0.2, 0.2, 0.15, 0.1, 0.1, 0.05],        # 7 bones
            [0.4, 0.2, 0.1, 0.0, 0.0, 0.0, 0.0],        # sum 0.7
            [0.0] * 7,                                   # sum 0
            [0.5, 0.496, 0.004, 0.0, 0.0, 0.0, 0.0],     # a weight of 0.004
            [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
        ]
        old = np.array(rows)
        new, change, stats = clean_weights(old)
        self.check_exact(new)
        self.assertLessEqual(int((new > 0).sum(axis=1).max()), 4)
        np.testing.assert_allclose(change, np.abs(new - old).sum(axis=1))
        self.assertEqual(stats.points, 6)
        self.assertEqual(stats.no_weight, 1)
        self.assertEqual(stats.over_limit, 2)
        self.assertEqual(stats.sum_off, 1)  # only the 0.7 row; the empty row is counted as no weight instead
        self.assertEqual(int(grid_units(new)[3].sum()), 0)  # the empty point stays empty
        self.assertEqual(change[3], 0.0)
        self.assertEqual(change[5], 0.0)  # 127 + 127 on the grid already
        self.assertEqual(new[5, 0], 127 / GRID)

    def test_the_tiny_weight_goes(self):
        new, _, stats = clean_weights(one_row([0.5, 0.496, 0.004]))
        self.assertEqual(stats.dropped_small, 1)
        self.assertEqual(new[0, 2], 0.0)
        self.check_exact(new)

    def test_sum_two_becomes_one(self):
        """The sample's LOD with 29 points that are 100 % one bone and 100 % another."""
        new, change, stats = clean_weights(one_row([1.0, 1.0, 0.0]))
        np.testing.assert_array_equal(grid_units(new), [[127, 127, 0]])
        self.assertAlmostEqual(change[0], 1.0)
        self.assertEqual(stats.sum_off, 1)
        self.assertAlmostEqual(stats.sum_off_max, 1.0)

    def test_a_point_below_the_threshold_keeps_its_heaviest(self):
        new, _, stats = clean_weights(one_row([0.004, 0.002]))
        np.testing.assert_array_equal(grid_units(new), [[254, 0]])
        self.assertEqual(stats.dropped_small, 1)

    def test_equal_weights_keep_the_lower_bone(self):
        new, _, _ = clean_weights(one_row([0.2] * 5), max_bones=4, threshold=0.0)
        self.assertEqual(int((new > 0).sum()), 4)
        self.assertEqual(new[0, 4], 0.0)
        self.check_exact(new)

    def test_discrete_keeps_the_heaviest_bone_only(self):
        old = np.array([[0.2, 0.5, 0.3], [0.5, 0.5, 0.0], [0.0, 0.0, 0.0]])
        new, change, stats = clean_weights(old, discrete=True)
        np.testing.assert_array_equal(new, [[0, 1, 0], [1, 0, 0], [0, 0, 0]])  # a tie: the lower bone
        np.testing.assert_allclose(change, [1.0, 1.0, 0.0])
        self.assertTrue(stats.discrete)

    def test_a_clean_set_does_not_change(self):
        rng = np.random.default_rng(3)
        units = np.zeros((200, 8), dtype=np.int64)
        for row in units:
            bones = rng.choice(8, size=int(rng.integers(1, 5)), replace=False)
            cuts = np.sort(rng.choice(np.arange(1, GRID), size=len(bones) - 1, replace=False))
            row[bones] = np.diff(np.concatenate([[0], cuts, [GRID]]))
        old = (units / GRID).astype(np.float32).astype(np.float64)  # the file's float32 weights
        old[old < 0.01] = 0.0
        old = old[old.sum(axis=1) > 0.999]
        new, change, stats = clean_weights(old, threshold=0.0)
        self.assertEqual(stats.changed, 0)
        self.assertLess(float(change.max()), 1e-6)

    def test_random_sets_always_end_on_the_grid_with_sum_one(self):
        rng = np.random.default_rng(7)
        old = rng.random((500, 12)) * (rng.random((500, 12)) < 0.5)
        new, change, stats = clean_weights(old, max_bones=4, threshold=0.01)
        self.check_exact(new)
        self.assertLessEqual(int((new > 0).sum(axis=1).max()), 4)
        self.assertEqual(stats.no_weight, int((old.sum(axis=1) == 0).sum()))


if __name__ == "__main__":
    unittest.main()

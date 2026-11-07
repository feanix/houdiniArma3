"""Feet and loose accessories of the wrap and Unpose (task #82): plain numpy on made-up bodies; runs under CPython and hython.

What these catch: a heel or toe found at the wrong end, a sole pitch with the wrong sign or size, a wrapped foot that stays
tilted after the sole step, a sparse sole that reads another pitch than a dense one, a loose piece whose edges change when
the body only moves, a piece that loses its distance from the surface.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.digest import Source, feet_page, wrap_distance_page
from a3ob_core.rig import find_feet, foot_landmarks, lay_on_sole, surface_follow
from a3ob_core.rig.foot import FOOT_LANDMARKS

HEEL, TOE = -0.11, 0.17  # the ends of a foot along Z, metres


def foot_points(side, tilt=0.0, lift=0.0, step=0.01, thick=0.07):
    """The points of one made-up foot: a block of ``step`` spacing from the heel to the toe, 10 cm wide, ``thick`` high. Its
    sole is a plane that rises towards the toes by ``tilt`` degrees, and the whole foot is ``lift`` higher."""
    xs = side * np.arange(0.10, 0.2001, step)
    zs = np.arange(HEEL, TOE + 1e-9, step)
    out = []
    for x in xs:
        for z in zs:
            base = lift + np.tan(np.radians(tilt)) * (z - HEEL)
            for y in np.arange(0.0, thick + 1e-9, step):
                out.append((x, base + y, z))
    return np.array(out)


def legs(side, lift=0.0):
    """A thin column above the foot, so the body is as tall as a person (1.8 m)."""
    return np.array([(side * 0.15, y, 0.0) for y in np.arange(lift + 0.12, lift + 1.801, 0.04)])


def body(tilt=0.0, lift=0.0, step=0.01):
    return np.vstack([foot_points(s, tilt, lift, step) for s in (1, -1)] + [legs(1, lift), legs(-1, lift)])


def sole_plane():
    """The user's sole: a flat sheet at height 0 as two triangles."""
    corners = np.array([(-0.4, 0.0, -0.3), (0.4, 0.0, -0.3), (0.4, 0.0, 0.4), (-0.4, 0.0, 0.4)])
    return corners, np.array([(0, 1, 2), (0, 2, 3)])


class TestFindFeet(unittest.TestCase):
    def test_a_flat_foot_has_no_pitch_and_its_ends_are_at_the_ends(self):
        feet = find_feet(body())
        self.assertEqual(sorted(feet), [-1, 1])
        for side, foot in feet.items():
            self.assertAlmostEqual(foot.pitch, 0.0, places=3)
            self.assertAlmostEqual(foot.back_end[2], HEEL, places=6)
            self.assertAlmostEqual(foot.front_end[2], TOE, places=6)
            self.assertLess(foot.heel[2], foot.toe[2] - 0.2)  # the heel is behind the toe tip
            self.assertGreater(foot.axis[2], 0.99)
            self.assertGreater(side * foot.heel[0], 0.0)  # the left foot (+X) is on the left

    def test_the_pitch_is_the_angle_of_the_sole_and_toes_up_is_positive(self):
        for tilt in (-6.0, 3.0, 8.0):
            pitch = find_feet(body(tilt=tilt))[1].pitch
            self.assertAlmostEqual(pitch, tilt, delta=0.6)

    def test_a_sparse_sole_reads_the_pitch_of_a_dense_one(self):
        dense, sparse = find_feet(body(tilt=4.0, step=0.01))[1], find_feet(body(tilt=4.0, step=0.02))[1]
        self.assertAlmostEqual(dense.pitch, sparse.pitch, delta=1.0)

    def test_a_body_that_floats_is_measured_from_its_own_sole(self):
        low, high = find_feet(body(tilt=2.0))[1], find_feet(body(tilt=2.0, lift=0.5))[1]
        self.assertAlmostEqual(low.pitch, high.pitch, places=6)
        self.assertAlmostEqual(high.low - low.low, 0.5, places=6)

    def test_too_few_points_find_no_foot(self):
        self.assertEqual(find_feet(np.zeros((10, 3))), {})


class TestFootLandmarks(unittest.TestCase):
    def test_the_four_points_of_a_foot_run_from_the_heel_to_the_toe(self):
        for side, foot in find_feet(body()).items():
            ends = [foot.heel[2], foot.sole_heel[2], foot.sole_toe[2], foot.toe[2]]
            for back, front in zip(ends, ends[1:]):
                self.assertLessEqual(back, front + 1e-9, "foot %d: %s" % (side, ends))
            self.assertAlmostEqual(foot.sole_heel[1], 0.0, delta=0.005)  # the ends of the flat sole rest on the floor
            self.assertAlmostEqual(foot.sole_toe[1], 0.0, delta=0.005)

    def test_every_pair_is_named_once_for_each_foot(self):
        pts = body()
        tri = np.array([(i, i + 1, i + 2) for i in range(0, len(pts) - 2, 3)])  # a rough mesh is enough for the names
        found = foot_landmarks(pts, tri)
        self.assertEqual(sorted(found), sorted(FOOT_LANDMARKS))
        corners, sheet = sole_plane()
        self.assertEqual(foot_landmarks(corners, sheet), {})  # a sheet has no feet: nothing is found, nothing breaks


class TestLayOnSole(unittest.TestCase):
    def test_a_wrapped_foot_that_slid_up_at_the_toes_is_laid_flat(self):
        P, tris = sole_plane()
        slid = body(tilt=3.5)  # toes 1.7 cm above the sole, the heel on it: the wrap of task #82 before the fix
        before = find_feet(slid)[1]
        self.assertAlmostEqual(before.pitch, 3.5, delta=0.6)
        flat = lay_on_sole(slid, P, tris)
        for side, foot in find_feet(flat).items():
            self.assertLess(abs(foot.pitch), 0.6, "foot %d" % side)
            self.assertAlmostEqual(foot.low, 0.0, places=6)
        # the lowest points of the toe end now rest on the sole
        toes = flat[(flat[:, 0] > 0) & (flat[:, 2] > TOE - 0.02) & (flat[:, 1] < 0.1)]
        self.assertLess(toes[:, 1].min(), 0.001)

    def test_the_sole_of_a_flat_foot_stays_and_the_leg_never_moves(self):
        P, tris = sole_plane()  # a sheet is no closed body: only the sole layer and the leg are claimed here
        start = body()
        flat = lay_on_sole(start, P, tris)
        sole = start[:, 1] == 0.0
        np.testing.assert_allclose(flat[sole], start[sole], atol=1e-9)
        slid = body(tilt=3.5)
        moved = lay_on_sole(slid, P, tris)
        high = slid[:, 1] > 0.3
        np.testing.assert_array_equal(moved[high], slid[high])


def cylinder(radius=0.15, rows=12, around=24, height=1.2):
    """A tube as polygons (rings of point numbers) and its points: the made-up body surface."""
    pts = [(radius * np.cos(a), y, radius * np.sin(a)) for y in np.linspace(0.0, height, rows)
           for a in np.linspace(0.0, 2 * np.pi, around, endpoint=False)]
    polys = [[r * around + a, r * around + (a + 1) % around, (r + 1) * around + (a + 1) % around, (r + 1) * around + a]
             for r in range(rows - 1) for a in range(around)]
    return np.array(pts), polys


def loose_piece(distance=0.03, radius=0.15, rows=8, around=16):
    """A loose sleeve at ``distance`` from the body surface: a grid of points on a wider tube."""
    return np.array([((radius + distance) * np.cos(a), y, (radius + distance) * np.sin(a))
                     for y in np.linspace(0.2, 1.0, rows) for a in np.linspace(0.0, 1.5 * np.pi, around)])


def edge_lengths(points):
    """The lengths of the grid edges of ``loose_piece`` (neighbours along the rows and along the columns)."""
    grid = points.reshape(8, 16, 3)
    return np.concatenate([np.linalg.norm(np.diff(grid, axis=0), axis=2).ravel(), np.linalg.norm(np.diff(grid, axis=1), axis=2).ravel()])


def turn_y(points, degrees, move=(0.0, 0.0, 0.0)):
    a = np.radians(degrees)
    r = np.array([[np.cos(a), 0.0, np.sin(a)], [0.0, 1.0, 0.0], [-np.sin(a), 0.0, np.cos(a)]])
    return points @ r.T + np.asarray(move)


class TestSurfaceFollow(unittest.TestCase):
    def test_a_loose_piece_keeps_its_edges_when_the_body_moves(self):
        rest, polys = cylinder()
        moved = turn_y(rest, 25.0, (0.3, 0.1, -0.2))  # the body is turned and carried: no bend
        piece = loose_piece()
        out = surface_follow(piece, rest, moved, polys)
        ratio = edge_lengths(out) / edge_lengths(piece)
        self.assertLess(np.abs(ratio - 1.0).max(), 0.01)

    def test_a_loose_piece_keeps_its_distance_from_the_surface(self):
        rest, polys = cylinder()
        moved = turn_y(rest, 25.0, (0.3, 0.1, -0.2))
        piece = loose_piece(distance=0.05)
        out = surface_follow(piece, rest, moved, polys)
        axis = moved[:, [0, 2]].mean(axis=0)
        distance = np.linalg.norm(out[:, [0, 2]] - axis, axis=1) - 0.15  # the tube turned about its own axis: radius 0.15
        np.testing.assert_allclose(distance, 0.05, atol=2e-3)

    def test_the_piece_follows_a_bending_body_and_is_not_bent_by_it(self):
        rest, polys = cylinder()
        bent = rest.copy()
        upper = bent[:, 1] > 0.6
        bent[upper] = turn_y(bent[upper] - (0.0, 0.6, 0.0), 40.0) + (0.0, 0.6, 0.0)  # the top half is twisted
        piece = loose_piece(distance=0.03)
        out = surface_follow(piece, rest, bent, polys, radius=0.0)  # the move of the surface only, not smoothed
        # the lower part of the piece (below the twist) stays where it is, the upper part is turned with the body
        low, high = piece[:, 1] < 0.45, piece[:, 1] > 0.75
        np.testing.assert_allclose(out[low], piece[low], atol=5e-3)
        self.assertGreater(np.linalg.norm(out[high] - piece[high], axis=1).mean(), 0.03)


class TestPages(unittest.TestCase):
    def test_the_feet_page_prints_the_pitch_of_each_foot_beside_the_reference(self):
        page = feet_page([("tilted", body(tilt=4.0)), ("flat", body()), ("nothing", None)], [Source("s.hiplc", "/obj/a")])
        text = page.text()
        self.assertIn("tilted", text)
        rows = [line for line in page.lines() if line.startswith(("tilted", "flat"))]
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0].split()[2].startswith("4."), rows[0])  # toes up positive, about 4 degrees
        self.assertTrue(rows[2].split()[2] in ("0.0", "-0.0"), rows[2])
        self.assertIn("reference: flat sole = 0.0", text)
        self.assertIn("feet of nothing", text)  # a body that was not recorded says so
        self.assertEqual(page.size_notice(), "", page.text())  # at most 25 lines of 80 columns

    def test_the_wrap_distance_page_has_zero_for_a_wrap_that_fits(self):
        P, tris = sole_plane()
        wrapped = np.array([(0.0, 0.0, 0.0), (0.1, 0.0, 0.1), (-0.1, 0.0, 0.2)])
        page = wrap_distance_page(wrapped, np.array([(0, 1, 2)]), None, P, tris, Source("s.hiplc", "/obj/a", "fitted_body"),
                                  Source("s.hiplc", "/obj/a input 1"))
        self.assertIn("median 0.0,", page.lines()[0])
        lifted = wrap_distance_page(wrapped + (0.0, 0.02, 0.0), np.array([(0, 1, 2)]), None, P, tris,
                                    Source("s.hiplc", "/obj/a", "fitted_body"), Source("s.hiplc", "/obj/a input 1"))
        self.assertIn("median 2.0,", lifted.lines()[0])
        for shown in (page, lifted):
            self.assertEqual(shown.size_notice(), "", shown.text())


if __name__ == "__main__":
    unittest.main()

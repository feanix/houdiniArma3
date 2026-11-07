"""Fingers and head (task #83) without Houdini: the two new digests (known answers and page layout), the finger fit on a
made-up hand and the rule that holds a head's tilt.

What these catch: a wrong bend angle or Kabsch angle (a sign, an axis), a page that outgrows 25 lines of 80 columns, a finger fit
that leaves a curled finger straight or bends a straight one, frames that stop being pure rotations, a head that is turned.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.digest import MAX_COLUMNS, MAX_LINES, NR, Source
from a3ob_core.digest.pose import (angle_between, bone_turn, dominant_bone_names, finger_bends, fingers_page, head_page, lean,
                                   turn_angles)
from a3ob_core.rig import bend_fingers, bone_frames, fit_skeleton, hold_orientation, skin_points
from a3ob_core.rig.hand import FINGERS

SRC = Source("scene.hiplc", "/obj/geo/a")


def rot(axis, degrees):
    a = np.radians(degrees)
    k = np.asarray(axis, dtype=np.float64)
    k = k / np.linalg.norm(k)
    cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(a) * cross + (1.0 - np.cos(a)) * (cross @ cross)


class TestFingerBend(unittest.TestCase):
    def finger_points(self, bend):
        """Three segments of 5 points each: segment 1 along +X, segment 2 turned by ``bend`` degrees about Z, segment 3 turned by
        as much again. Returns points and the lower-case bone name of each."""
        rng = np.random.default_rng(1)
        points, names = [], []
        origin, direction = np.zeros(3), np.array([1.0, 0.0, 0.0])
        for k in (1, 2, 3):
            for t in np.linspace(0.0, 0.03, 5):
                points.append(origin + direction * t + rng.normal(0.0, 1e-5, 3))
                names.append("lefthandindex%d" % k)
            origin = origin + direction * 0.03
            direction = rot([0, 0, 1], bend) @ direction
        palm = [np.array([-0.05, 0.0, 0.0]) + rng.normal(0.0, 1e-5, 3) for _ in range(5)]
        return np.array(points + palm), np.array(names + ["lefthand"] * 5, dtype=object)

    def test_the_bend_at_the_middle_knuckle_is_the_angle_between_the_two_directions(self):
        for bend in (0.0, 20.0, 45.0, 90.0):
            points, names = self.finger_points(bend)
            base, middle = finger_bends(points, names, "left", "index")
            self.assertAlmostEqual(middle, bend, delta=1.5, msg=str(bend))  # the segments' own centres are a little off the joint
            self.assertAlmostEqual(base, bend / 2.0 if bend else 0.0, delta=bend * 0.3 + 1.0)

    def test_a_segment_with_too_few_points_is_not_recorded(self):
        points, names = self.finger_points(30.0)
        keep = ~((names == "lefthandindex3") & (np.arange(len(names)) % 5 > 0))  # one point left of the last segment
        base, middle = finger_bends(points[keep], names[keep], "left", "index")
        self.assertIsNone(middle)
        self.assertIsNotNone(base)

    def test_the_page_lists_the_bends_beside_the_reference_and_keeps_its_limits(self):
        points, names = self.finger_points(30.0)
        page = fingers_page([("owner", points, names), ("Arma sample", points[:0], None)], [SRC])
        text = page.text()
        self.assertIn("30", text)
        self.assertIn(NR, text)
        self.assertIn("Arma sample", text)
        self.assertLessEqual(len(page.lines()), MAX_LINES)
        self.assertLessEqual(max(len(line) for line in page.lines()), MAX_COLUMNS)

    def test_angle_between_and_dominant_names(self):
        self.assertAlmostEqual(angle_between(np.array([1.0, 0, 0]), np.array([0.0, 1, 0])), 90.0)
        self.assertTrue(np.isnan(angle_between(np.zeros(3), np.array([1.0, 0, 0]))))
        names = dominant_bone_names(np.array([[0.2, 0.8], [0.0, 0.0]]), ["Head", "Neck"])
        self.assertEqual(list(names), ["neck", ""])


class TestBoneTurn(unittest.TestCase):
    def cloud(self):
        rng = np.random.default_rng(3)
        return rng.normal(0.0, 0.1, (200, 3)) + np.array([0.0, 1.6, 0.0])

    def test_known_pitch_yaw_and_roll_come_back(self):
        a = self.cloud()
        centre = a.mean(axis=0)
        # pitch: nose (+Z) goes down (-Y) for a positive turn about +X
        for axis, index, sign in (([1, 0, 0], 0, 1.0), ([0, 1, 0], 1, 1.0), ([0, 0, 1], 2, 1.0)):
            b = (a - centre) @ rot(axis, 12.0).T + centre + np.array([0.3, 0.0, -0.1])
            rotation, rms = bone_turn(a, b)
            angles = turn_angles(rotation)
            self.assertAlmostEqual(angles[index], 12.0, places=4)
            self.assertAlmostEqual(sum(abs(v) for k, v in enumerate(angles) if k != index), 0.0, places=4)
            self.assertLess(rms, 1e-9)
        nose = rot([1, 0, 0], 10.0) @ np.array([0.0, 0.0, 1.0])
        self.assertLess(nose[1], 0.0)  # positive pitch is nose down

    def test_a_pure_shift_is_no_turn_and_the_rms_shows_what_is_left(self):
        a = self.cloud()
        rotation, rms = bone_turn(a, a + np.array([1.0, 2.0, 3.0]))
        np.testing.assert_allclose(turn_angles(rotation), (0.0, 0.0, 0.0), atol=1e-6)
        self.assertLess(rms, 1e-9)
        bent = a.copy()
        bent[:, 1] *= 1.2  # not a rigid move
        self.assertGreater(bone_turn(a, bent)[1], 0.005)

    def test_lean_is_from_vertical_and_forward_is_positive(self):
        self.assertAlmostEqual(lean(np.array([0.0, 1.0, 1.0]), np.zeros(3)), 45.0)
        self.assertAlmostEqual(lean(np.array([0.0, 1.0, 0.0]), np.zeros(3)), 0.0)
        self.assertAlmostEqual(lean(np.array([0.0, 1.0, -1.0]), np.zeros(3)), -45.0)

    def test_the_head_page_gives_the_turn_and_the_lean_beside_the_arma_rest_and_keeps_its_limits(self):
        a = self.cloud()
        b = (a - a.mean(axis=0)) @ rot([1, 0, 0], 15.0).T + a.mean(axis=0)
        fitted = {"neck": np.zeros(3), "neck1": np.array([0.0, 1.0, 0.3]), "head": np.array([0.0, 2.0, 0.6])}
        arma = {"neck": np.zeros(3), "neck1": np.array([0.0, 1.0, 0.2]), "head": np.array([0.0, 2.0, 0.25])}
        page = head_page([("head", a, b)], ["head"], SRC, SRC, fitted, arma)
        text = page.text()
        self.assertIn("pitch 15.0", text)
        self.assertIn("neck1 -> head", text)
        self.assertIn("reference: 0", text)
        self.assertLessEqual(len(page.lines()), MAX_LINES)
        self.assertLessEqual(max(len(line) for line in page.lines()), MAX_COLUMNS)
        self.assertIn("lean lines", " ".join(head_page([("head", a, b)], ["head"], SRC, SRC, fitted, None).missing))


class TestHoldOrientation(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(5)
        self.points = rng.normal(0.0, 0.1, (60, 3))
        self.members = np.zeros(60, dtype=bool)
        self.members[:30] = True
        self.seam = np.zeros(60, dtype=bool)
        self.seam[:5] = True
        turn = rot([1, 0, 0], 15.0)
        self.caged = self.points @ turn.T + np.array([0.0, 0.1, 0.0])

    def test_the_members_move_by_a_shift_only_and_the_others_stay_where_the_cage_put_them(self):
        out = hold_orientation(self.points, self.caged, self.members, self.seam)
        np.testing.assert_allclose(out[~self.members], self.caged[~self.members])
        moved = out[self.members] - self.points[self.members]
        np.testing.assert_allclose(moved, moved[0] + np.zeros_like(moved), atol=1e-12)  # every member the same shift
        np.testing.assert_allclose(moved[0], (self.caged[:5] - self.points[:5]).mean(axis=0), atol=1e-12)  # the seam's mean move
        rotation, rms = bone_turn(self.points[self.members], out[self.members])
        np.testing.assert_allclose(turn_angles(rotation), (0.0, 0.0, 0.0), atol=1e-6)

    def test_without_a_seam_all_members_count_and_without_members_nothing_changes(self):
        out = hold_orientation(self.points, self.caged, self.members, np.zeros(60, dtype=bool))
        shift = (self.caged[self.members] - self.points[self.members]).mean(axis=0)
        np.testing.assert_allclose(out[self.members] - self.points[self.members], np.tile(shift, (30, 1)), atol=1e-12)
        same = hold_orientation(self.points, self.caged, np.zeros(60, dtype=bool), self.seam)
        np.testing.assert_allclose(same, self.caged)


def hand_skeleton(side="left"):
    """A made-up arm with a hand and the five fingers: ``(names, parents, rest)``. The arm runs along +X (left) with the fingers
    straight on along it, the palm normal along +Y, the thumb at +Z... (a hand held flat, palm down)."""
    sign = 1.0 if side == "left" else -1.0
    names, parents, rest = ["spine"], [-1], [np.zeros(3)]

    def add(name, parent, position):
        names.append(name)
        parents.append(names.index(parent))
        rest.append(np.asarray(position, dtype=np.float64))

    prefix = side
    add(prefix + "forearm", "spine", (sign * 0.3, 0.0, 0.0))
    add(prefix + "hand", prefix + "forearm", (sign * 0.55, 0.0, 0.0))
    lateral = {"index": 0.03, "middle": 0.01, "ring": -0.01, "pinky": -0.03}
    for finger, z in lateral.items():
        add("%shand%s1" % (prefix, finger), prefix + "hand", (sign * 0.64, 0.0, z))
        add("%shand%s2" % (prefix, finger), "%shand%s1" % (prefix, finger), (sign * 0.68, 0.0, z))
        add("%shand%s3" % (prefix, finger), "%shand%s2" % (prefix, finger), (sign * 0.705, 0.0, z))
    add(prefix + "handthumb1", prefix + "hand", (sign * 0.59, -0.01, 0.045))
    add(prefix + "handthumb2", prefix + "handthumb1", (sign * 0.62, -0.01, 0.06))
    add(prefix + "handthumb3", prefix + "handthumb2", (sign * 0.64, -0.01, 0.07))
    return names, parents, np.array(rest)


def tube_points(a, b, radius=0.009, rings=7, around=8):
    """Points on a short tube from ``a`` to ``b`` (a finger segment's skin) and the end cap."""
    axis = (b - a) / np.linalg.norm(b - a)
    side = np.cross(axis, [0.0, 1.0, 0.0])
    if np.linalg.norm(side) < 1e-6:
        side = np.cross(axis, [0.0, 0.0, 1.0])
    side /= np.linalg.norm(side)
    up = np.cross(axis, side)
    points = []
    for t in np.linspace(0.0, 1.0, rings):
        for k in range(around):
            ang = 2.0 * np.pi * k / around
            points.append(a + (b - a) * t + radius * (np.cos(ang) * side + np.sin(ang) * up))
    return np.array(points)


class TestFingerFit(unittest.TestCase):
    """A made-up hand whose fingers are curled by known angles: the finger joints come out where the curl put them."""

    def setUp(self):
        self.names, self.parents, self.rest = hand_skeleton("left")
        self.index = {n: i for i, n in enumerate(self.names)}
        count = len(self.names)
        self.W = np.zeros((0, count))
        self.P = np.zeros((0, 3))
        rows, pts = [], []
        for finger in FINGERS:
            for k in (1, 2, 3):
                joint = self.index["lefthand%s%d" % (finger, k)]
                if k == 3:  # the fingertip goes on past the last joint
                    end = self.rest[joint] + (self.rest[joint] - self.rest[self.parents[joint]]) * 0.7
                else:
                    end = self.rest[self.index["lefthand%s%d" % (finger, k + 1)]]
                for p in tube_points(self.rest[joint], end):
                    row = np.zeros(count)
                    row[joint] = 1.0
                    rows.append(row)
                    pts.append(p)
        palm = []
        for z in np.linspace(-0.04, 0.05, 6):
            for x in np.linspace(0.5, 0.64, 6):
                palm.append((x, 0.0, z))
        for p in palm:
            row = np.zeros(count)
            row[self.index["lefthand"]] = 1.0
            rows.append(row)
            pts.append(np.array(p))
        self.W, self.P = np.array(rows), np.array(pts)

    def curled(self, base, middle):
        """The made-up hand with every finger bent ``base`` degrees at the knuckle and ``middle`` degrees more at the middle
        joint (about the finger's Z axis, towards -Y), the hand where it is. Returns ``(points, true joint positions)``."""
        pos = self.rest.copy()
        mats = np.tile(np.eye(4), (len(self.names), 1, 1))
        world = {}
        for i, name in enumerate(self.names):
            parent = self.parents[i]
            local = np.eye(3)
            if name.startswith("lefthand") and name[-1] == "1" and "thumb" not in name:
                local = rot([0, 0, 1], -base)
            if name.startswith("lefthand") and name[-1] == "2" and "thumb" not in name:
                local = rot([0, 0, 1], -middle)
            if parent < 0:
                world[i] = (np.eye(3), self.rest[i].copy())
            else:
                r_parent, p_parent = world[parent]
                r = r_parent @ local
                world[i] = (r, p_parent + r_parent @ (self.rest[i] - self.rest[parent]))
            mats[i, :3, :3] = world[i][0]
            mats[i, :3, 3] = world[i][1] - world[i][0] @ self.rest[i]
            pos[i] = world[i][1]
        w = self.W / np.maximum(self.W.sum(axis=1, keepdims=True), 1e-9)
        blend = np.einsum("nb,bij->nij", w, mats)
        return np.einsum("nij,nj->ni", blend[:, :3, :3], self.P) + blend[:, :3, 3], pos

    def targets(self, truth):
        """The hand landmarks a user would set: the wrist, the end joints of the middle finger and the thumb."""
        return {self.index["lefthand"]: truth[self.index["lefthand"]],
                self.index["lefthandmiddle3"]: truth[self.index["lefthandmiddle3"]],
                self.index["lefthandthumb3"]: truth[self.index["lefthandthumb3"]],
                self.index["leftforearm"]: truth[self.index["leftforearm"]]}

    def fit_both(self, base, middle):
        points, truth = self.curled(base, middle)
        targets = self.targets(truth)
        first = fit_skeleton(self.parents, self.rest, targets, "bone")
        turns, bent, info = bend_fingers(self.parents, self.rest, first, self.index, self.P, self.W, points)
        return points, truth, targets, first, fit_skeleton(self.parents, self.rest, targets, "bone", 1.0, bent, turns), info

    def test_curled_fingers_get_joints_that_follow_the_curl_and_the_frames_stay_pure_rotations(self):
        points, truth, targets, first, second, info = self.fit_both(40.0, 60.0)
        joints = [self.index["lefthand%s%d" % (f, k)] for f in FINGERS[:4] for k in (2, 3)]
        before = np.linalg.norm(first.positions[joints] - truth[joints], axis=1)
        after = np.linalg.norm(second.positions[joints] - truth[joints], axis=1)
        print("   task 83 curled hand (40 and 60 degrees): finger joints %.2f cm off straight, %.2f cm after (worst %.2f cm)" % (
            100 * before.mean(), 100 * after.mean(), 100 * after.max()))
        self.assertGreater(float(before.mean()), 0.02)  # the straight fit really is off
        self.assertLess(float(after.mean()), 0.5 * float(before.mean()))
        self.assertLess(float(after.mean()), 0.012)
        self.assertLess(float(after.max()), 0.025)
        self.assertEqual(len(info), 5)
        for finger in FINGERS[:4]:
            flex1, flex2, _ = info["left " + finger]
            self.assertAlmostEqual(abs(flex2), 60.0, delta=20.0, msg=finger)
        frames = bone_frames(self.parents, second.positions)
        for number in range(len(self.names)):
            np.testing.assert_allclose(frames[number] @ frames[number].T, np.eye(3), atol=1e-9)
            self.assertAlmostEqual(float(np.linalg.det(frames[number])), 1.0, places=9)
        # and the skinned sample is bent like the mesh: the fitted fingers are closer to the mesh than the straight ones
        skinned = skin_points(self.P, self.W, self.rest, second)
        straight = skin_points(self.P, self.W, self.rest, first)
        dist = lambda a: np.linalg.norm(a - points, axis=1).mean()
        self.assertLess(dist(skinned), 0.5 * dist(straight))

    def test_a_straight_hand_stays_straight(self):
        points, truth, targets, first, second, info = self.fit_both(0.0, 0.0)
        joints = [self.index["lefthand%s%d" % (f, k)] for f in FINGERS[:4] for k in (2, 3)]
        error = np.linalg.norm(second.positions[joints] - truth[joints], axis=1)
        print("   task 83 straight hand: finger joints %.2f cm at most off the truth" % (100 * error.max()))
        self.assertLess(float(error.max()), 0.012)

    def test_a_finger_with_too_few_points_is_left_straight(self):
        points, truth = self.curled(30.0, 50.0)
        targets = self.targets(truth)
        first = fit_skeleton(self.parents, self.rest, targets, "bone")
        turns, bent, info = bend_fingers(self.parents, self.rest, first, self.index, self.P, self.W, points[:3])
        self.assertEqual(bent, {})  # three points of the mesh: nothing to fit to


if __name__ == "__main__":
    unittest.main()

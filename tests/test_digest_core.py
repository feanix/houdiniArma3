"""The rig digests without Houdini (task #81): known answers on tiny made-up skeletons and meshes, the rules that must always
hold, and the page layout at the size of the owner scene.

What these catch: a wrong formula (an angle, a ratio, a count), a difference column that is not the two values, a page that
outgrows 25 lines of 80 columns, a zero printed where a figure was not recorded.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import re
import unittest

import numpy as np

from a3ob_core.digest import (MAX_COLUMNS, MAX_LINES, NR, SampleSide, Skel, Source, compare_page, deform_page, edge_ratios,
                              landmarks_page, measure_values, skeleton_page, weight_figures, weights_page)
from a3ob_core.digest import regions
from a3ob_core.rig import check
from a3ob_core.rig.landmarks import Measure, Template
from a3ob_houdini.p3d_export.rig_bones import OFP2_MANSKELETON_BONES

SRC = Source("scene.hiplc", "/obj/geo/a")
SRC_B = Source("scene.hiplc", "/obj/geo/b", "fitted_skeleton")


def body_skeleton(arm_drop=0.3, scale=1.0, shift=(0.0, 0.0, 0.0)) -> Skel:
    """The joints the body measures need, in metres. The elbow is bent 90 degrees when ``arm_drop`` is 0.3."""
    joints = {
        "Pelvis": (0.0, 1.0, 0.0), "Spine": (0.0, 1.0, 0.0), "head": (0.0, 1.6, 0.0),
        "LeftUpLeg": (0.1, 0.9, 0.0), "RightUpLeg": (-0.1, 0.9, 0.0),
        "LeftLeg": (0.1, 0.5, 0.0), "RightLeg": (-0.1, 0.5, 0.0), "LeftFoot": (0.1, 0.1, 0.0), "RightFoot": (-0.1, 0.1, 0.0),
        "LeftArm": (0.2, 1.5, 0.0), "RightArm": (-0.2, 1.5, 0.0),
        "LeftForeArm": (0.5, 1.5, 0.0), "RightForeArm": (-0.5, 1.5, 0.0),
        "LeftHand": (0.5, 1.5 - arm_drop, 0.0), "RightHand": (-0.5, 1.5 - arm_drop, 0.0),
    }
    parents = {"Pelvis": None, "Spine": "Pelvis", "head": "Spine", "LeftUpLeg": "Pelvis", "RightUpLeg": "Pelvis",
               "LeftLeg": "LeftUpLeg", "RightLeg": "RightUpLeg", "LeftFoot": "LeftLeg", "RightFoot": "RightLeg",
               "LeftArm": "Spine", "RightArm": "Spine", "LeftForeArm": "LeftArm", "RightForeArm": "RightArm",
               "LeftHand": "LeftForeArm", "RightHand": "RightForeArm"}
    names = list(joints)
    pos = np.array([joints[n] for n in names]) * scale + np.array(shift)
    return Skel(names, [-1 if parents[n] is None else names.index(parents[n]) for n in names], pos)


def table(page, label):
    """The numbers of the table row whose first cell starts with ``label``."""
    for line in page.lines():
        if line.startswith(label):
            return [float(x) for x in re.findall(r"-?\d+\.\d+", line[len(label):])]
    raise AssertionError("no row %r in\n%s" % (label, page))


class TestSkeleton(unittest.TestCase):
    def test_measures_on_a_made_up_body(self):
        m = measure_values(body_skeleton())
        self.assertAlmostEqual(m["pelvis_hips"], 0.1)  # the Pelvis to the middle of the hips (0, 0.9, 0)
        self.assertAlmostEqual(m["pelvis_head"], 0.6)
        self.assertAlmostEqual(m["hip_width"], 0.2)
        self.assertAlmostEqual(m["shoulder_width"], 0.4)
        self.assertAlmostEqual(m["elbow_l"], 90.0)  # upper arm along -X from the elbow, forearm down
        self.assertAlmostEqual(m["knee_l"], 180.0)  # hip, knee and ankle in one line

    def test_a_missing_joint_is_none_not_zero(self):
        skel = body_skeleton()
        skel = Skel(skel.names[1:], [p - 1 for p in skel.parents[1:]], skel.positions[1:])  # no Pelvis
        m = measure_values(skel)
        self.assertIsNone(m["pelvis_hips"])
        self.assertIsNone(m["pelvis_head"])
        self.assertIsNotNone(m["hip_width"])

    def test_skeleton_page_bone_lengths_beside_the_rest(self):
        page = skeleton_page(body_skeleton(scale=2.0), body_skeleton(), SRC, "/obj/geo/rest")
        text = page.text()
        self.assertIn("15 joints, 1 root", page.lines()[0])
        self.assertIn("0.0/0.0", text)  # the zero-length Pelvis to Spine bone, in both
        self.assertIn("root", text)
        self.assertIn("Arma rest: /obj/geo/rest", text)
        # head: 0.6 m from the Spine at rest; twice that in the scaled copy
        self.assertIn("120.0/60.0", text.replace("0.0/0.0", ""))

    def test_no_rest_is_not_recorded(self):
        page = skeleton_page(body_skeleton(), None, SRC)
        self.assertIn("Arma rest: " + NR, page.text())
        self.assertTrue(any(line.startswith("not recorded:") for line in page.lines()))

    def test_compare_differences_equal_the_values_asked_one_at_a_time(self):
        a, b = body_skeleton(), body_skeleton(arm_drop=0.1, scale=1.1, shift=(0.5, 0.0, 0.0))
        both = compare_page(a, b, SRC, SRC_B)
        only_a = compare_page(a, a, SRC, SRC)
        only_b = compare_page(b, b, SRC_B, SRC_B)
        from a3ob_core.digest.skeleton import MEASURES
        for _, label, unit in MEASURES:
            name = "%s %s" % (label, unit)
            va, vb, diff = table(both, name)
            self.assertEqual(va, table(only_a, name)[0], name)
            self.assertEqual(vb, table(only_b, name)[1], name)
            self.assertAlmostEqual(diff, vb - va, delta=0.11, msg=name)  # three printed figures of one decimal each
        self.assertEqual(table(both, "pelvis-to-hips cm")[:2], [10.0, 11.0])

    def test_comparing_a_skeleton_with_itself_changes_nothing(self):
        page = compare_page(body_skeleton(), body_skeleton(), SRC, SRC)
        for label in ("position offset cm", "bone length diff cm"):
            self.assertEqual(table(page, label)[:3], [0.0, 0.0, 0.0], label)
        for _, label, unit in __import__("a3ob_core.digest.skeleton", fromlist=["MEASURES"]).MEASURES:
            self.assertEqual(table(page, "%s %s" % (label, unit))[2], 0.0)

    def test_a_shift_shows_in_the_offset_and_not_after_the_mean_move(self):
        page = compare_page(body_skeleton(), body_skeleton(shift=(0.5, 0.0, 0.0)), SRC, SRC_B)
        self.assertEqual(table(page, "position offset cm")[0], 50.0)
        self.assertEqual(table(page, "  less mean shift cm")[0], 0.0)

    def test_unmatched_joints_are_said(self):
        a = body_skeleton()
        b = Skel(a.names[:-2], a.parents[:-2], a.positions[:-2])
        page = compare_page(a, b, SRC, SRC_B)
        self.assertIn("2 joints of A", page.text())


def strip(n=6):
    """A strip of triangles along X: points 0..n-1 on the line y = 0 and n..2n-1 on y = 1."""
    pts = np.array([(float(i), 0.0, 0.0) for i in range(n)] + [(float(i), 1.0, 0.0) for i in range(n)])
    tris = []
    for i in range(n - 1):
        tris += [(i, i + 1, n + i), (i + 1, n + i + 1, n + i)]
    return pts, np.array(tris)


class TestDeform(unittest.TestCase):
    def test_a_mesh_against_itself_is_one_everywhere(self):
        pts, tris = strip()
        edges = check.edges_of(tris)
        self.assertTrue(np.all(edge_ratios(pts, pts, edges) == 1.0))
        page = deform_page(pts, pts, tris, None, [], SRC, SRC_B)
        self.assertIn("median 1.000, p1 1.000, p99 1.000", page.lines()[0])

    def test_known_ratios_by_region_and_group(self):
        pts, tris = strip(6)
        after = pts.copy()
        after[[3, 4, 5, 9, 10, 11], 0] = [3.0, 5.0, 7.0, 3.0, 5.0, 7.0]  # the right half is stretched along X by 2
        weights = np.zeros((12, 2))
        weights[[0, 1, 2, 6, 7, 8], 0] = 1.0  # head
        weights[[3, 4, 5, 9, 10, 11], 1] = 1.0  # LeftArm
        group = np.zeros(12, dtype=bool)
        group[[3, 4, 5, 9, 10, 11]] = True
        page = deform_page(pts, after, tris, weights, ["head", "LeftArm"], SRC, SRC_B, "region", None, "body", group, "helmet")
        text = page.text()
        self.assertEqual(table(page, "head"), [1.0, 1.0, 1.0])  # median, p1, p99 of unchanged edges
        self.assertEqual(table(page, "arm_l")[2], 2.0)  # p99: the edges along X are twice as long
        self.assertEqual(table(page, "arm_l")[1], 1.0)  # p1: a vertical edge, which is not stretched
        self.assertIn("helmet size cm: A 200.0 x 100.0 x 0.0; B 400.0 x 100.0 x 0.0", text)
        self.assertIn("body rows (no group body", text)  # no body group: not recorded, not a row of zeros

    def test_by_bone_names_the_bones(self):
        pts, tris = strip(4)
        weights = np.zeros((8, 2))
        weights[:4, 0] = 1.0
        weights[4:, 1] = 1.0
        page = deform_page(pts, pts, tris, weights, ["head", "LeftArm"], SRC, SRC_B, "bone")
        self.assertTrue(any(line.startswith("head ") for line in page.lines()))
        self.assertTrue(any(line.startswith("LeftArm ") for line in page.lines()))

    def test_regions_of_bones(self):
        self.assertEqual(regions.region_of("LeftForeArmRoll"), "arm_l")
        self.assertEqual(regions.region_of("RightHandIndex2"), "hand_r")
        self.assertEqual(regions.region_of("LeftUpLegRoll"), "leg_l")
        self.assertEqual(regions.region_of("RightToeBase"), "foot_r")
        self.assertEqual(regions.region_of("Spine2"), "torso")
        self.assertEqual(regions.region_of("neck1"), "head")
        self.assertEqual(regions.region_of("weapon"), "")


class TestWeights(unittest.TestCase):
    BONES = ["a", "b", "c", "d", "e", "f"]

    def matrix(self):
        w = np.zeros((6, 6))
        w[0, 0] = 1.0                       # one bone
        w[1, [0, 1]] = [0.5, 0.5]           # two
        w[2, [0, 1, 2, 3, 4]] = [0.3, 0.2, 0.2, 0.2, 0.1]  # five: over the limit
        w[3] = 0.0                          # none
        w[4, [1, 2]] = [0.5, 0.4]           # sum 0.9
        w[5, 5] = 1.0
        return w

    def test_counts(self):
        f = weight_figures(self.matrix())
        self.assertEqual(f["unweighted"], 1)
        self.assertEqual(list(f["influences"]), [1, 2, 5, 0, 2, 1])
        self.assertEqual(list(f["per_bone"]), [3, 3, 2, 1, 1, 1])
        page = weights_page(self.matrix(), self.BONES, SRC, "bone selections")
        self.assertIn("6 points; 1 unweighted; 1 over 4 bones; most influences 5", page.lines()[0])
        self.assertIn("0:1 1:2 2:2 3:0 4:0 5+:1", page.text())
        self.assertIn("(point 4); over 0.01: 1", page.text())  # the sum off 1 is 0.1 at point 4

    def test_a_weight_of_zero_is_no_influence(self):
        w = np.zeros((2, 2))
        w[0] = [1.0, 0.0]
        self.assertEqual(list(weight_figures(w)["influences"]), [1, 0])

    def test_no_weight_at_all_says_so(self):
        page = weights_page(np.zeros((3, 2)), ["a", "b"], SRC, "bone selections")
        self.assertIn("no point has a weight", page.text())
        self.assertNotIn("median 0.0000", page.text())


class TestLandmarks(unittest.TestCase):
    def test_not_recorded_without_a_mesh_or_sample(self):
        page = landmarks_page({"lm_groin": np.array([0.0, 0.8, 0.0])}, None, SampleSide(), SRC, "none")
        text = page.text()
        self.assertIn("1 of 18 set", page.lines()[0])
        self.assertIn(NR, text)
        self.assertIn("not recorded:", text)
        self.assertNotIn("0.0 cm from", text)  # no mesh crotch: no distance, not a zero distance

    def test_heights_above_the_sole_and_the_distance_to_the_found_crotch(self):
        pts = []
        for x in np.arange(0.04, 0.16, 0.02):  # two legs apart, 0..0.9
            for y in np.arange(0.0, 0.9, 0.01):
                pts += [(x, y, 0.0), (-x, y, 0.0)]
        for x in np.arange(-0.2, 0.2, 0.02):  # the trunk and the head, 0.9..1.8
            for y in np.arange(0.9, 1.8, 0.01):
                pts.append((x, y, 0.0))
        mesh = np.array(pts)
        landmarks = {"lm_groin": np.array([0.0, 0.9, 0.0])}
        page = landmarks_page(landmarks, mesh, SampleSide(), SRC, "mesh, group body")
        row = table(page, "crotch height")
        self.assertAlmostEqual(row[0], 90.0, places=1)  # the landmark, above the lowest point (0)
        self.assertAlmostEqual(row[1], 90.0, delta=3.0)  # found on the mesh, within a slice of the true gap top


def _arma_parent(names, name):
    """The parent number of a joint in the Arma man skeleton's shape (the chains of the page, the fingers under the hand)."""
    from a3ob_core.digest.skeleton import CHAINS
    for _, chain in CHAINS:
        if name in chain and chain.index(name) > 0:
            return names.index(chain[chain.index(name) - 1])
    if name == "Pelvis":
        return -1
    if name.startswith(("LeftHand", "RightHand")):
        return names.index(name[:name.index("Hand") + 4])
    if name in ("LeftShoulder", "RightShoulder"):
        return names.index("Spine3")
    return names.index("Pelvis")


def sized_case(points=12888):
    """The size of the owner scene: the 64-joint Arma skeleton names, a 12888-point mesh with weights on about 57 bones."""
    rng = np.random.default_rng(1)
    names = [n for n in OFP2_MANSKELETON_BONES if not n.startswith(("Face_", "Eye")) and n not in ("Camera", "weapon", "launcher")]
    parents = [_arma_parent(names, n) for n in names]
    positions = rng.normal(size=(len(names), 3)) * 0.1
    return names, parents, positions


class TestLayout(unittest.TestCase):
    def check(self, page):
        lines = page.lines()
        self.assertLessEqual(len(lines), MAX_LINES, "\n" + page.text())
        self.assertLessEqual(max(len(line) for line in lines), MAX_COLUMNS, "\n" + page.text())
        self.assertTrue(lines[-1].startswith("next:"), page.text())
        self.assertEqual(page.size_notice(), "")

    def test_every_page_keeps_25_lines_of_80_columns_at_the_owner_scene_size(self):
        names, parents, positions = sized_case()
        self.assertEqual(len(names), 64)
        a = Skel(names, parents, positions)
        b = Skel(names, parents, positions * 1.05 + 0.3)
        long_source = Source("Character_68_rigcheck_long_name_copy_for_the_layout.hiplc",
                             "/obj/rig_fit_66/a_rather_long_node_name_in_a_deep_network/weight_cleanup_final", "fitted_skeleton")
        self.check(skeleton_page(a, b, long_source, "/obj/rig_fit_66/skeleton (input 2)"))
        self.check(skeleton_page(a, None, SRC))
        self.check(compare_page(a, b, long_source, SRC_B))
        rng = np.random.default_rng(2)
        n = 12888
        pts = rng.normal(size=(n, 3))
        tris = rng.integers(0, n, size=(25876, 3))
        tris = tris[(tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])]
        posed = pts * 1.25
        bones = list(OFP2_MANSKELETON_BONES)
        w = np.zeros((n, len(bones)))
        pick = rng.integers(0, 57, size=n)
        w[np.arange(n), pick] = 1.0
        some = np.arange(0, n, 7)
        w[some, (pick[some] + 1) % 57] = 0.4
        w[::50] = 0.0
        body = rng.random(n) < 0.7
        self.check(deform_page(pts, posed, tris, w, bones, long_source, SRC_B, "region", body, "body", ~body, "helmet"))
        self.check(deform_page(pts, posed, tris, None, bones, SRC, SRC_B, "region", None, "body", None, "helmet"))
        self.check(weights_page(w, bones, long_source, "bone selections", zero_members=3))
        truth = {n_: np.array([0.1, 1.0, 0.0]) for n_ in ("lm_groin", "lm_hip_l", "lm_hip_r", "lm_shoulder_l", "lm_shoulder_r")}
        template = Template(truth, Measure(0.0, 1.8, 0.0, 0.0, 0.9, np.array([0.0, 0.9, 0.0]), True, 1.5), {})
        landmarks = {n_: rng.normal(size=3) for n_ in __import__("a3ob_core.rig", fromlist=["LANDMARK_NAMES"]).LANDMARK_NAMES}
        body_pts = rng.normal(size=(9000, 3))
        self.check(landmarks_page(landmarks, body_pts, SampleSide(template, a), long_source,
                                  "/obj/rig_fit_66/group_body, group body", b))


if __name__ == "__main__":
    unittest.main()

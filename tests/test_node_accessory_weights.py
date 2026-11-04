"""The Accessory Weights node in hython (design test 2.2 through the node, chains, the skeleton input, task #73, D41).
Needs ``hou`` with KineFX: do not run it with HOUDINI_PACKAGE_SKIP=1 (that removes every kinefx:: type).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_houdini import entry, rig_weights
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import cook, make_source
from tests.test_node_rig_skeleton import write_pivots  # noqa: F401  (used by the base class)
from tests.test_node_weight_transfer import BONES, TransferTestCase, add_group, skinned
from tests.test_node_weights import selection_weights
from tests.test_rig_transfer import tube, two_bone_weights, with_cuff


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class AccessoryTestCase(TransferTestCase):
    def scene(self, **parms):
        """A body tube weighted to Spine and ArmRoll with a cuff on it (unweighted), and a far-away unweighted tube
        ``extra``. Point groups: ``body``, ``cuff``, ``extra``. Returns ``(node, cuff mask, body mask)``."""
        P, tris = tube(24, 16)
        W = np.zeros((len(P), 5))
        two = two_bone_weights(P)
        W[:, 1], W[:, 3] = two[:, 0], two[:, 1]  # Spine below, ArmRoll above
        all_P, all_tris, cuff = with_cuff(P, tris)
        extra_P, extra_tris = tube(4, 8, 0.1, 0.3, 0.0, first_point=len(all_P))
        extra_P = extra_P + np.array([4.0, 0.0, 0.0])
        every_P, every_tris = np.concatenate([all_P, extra_P]), np.concatenate([all_tris, extra_tris])
        weights = np.zeros((len(every_P), 5))
        weights[:len(P)] = W
        geo = skinned(every_P, every_tris, weights, BONES)
        body = np.zeros(len(every_P), bool)
        body[:len(P)] = True
        full_cuff = np.zeros(len(every_P), bool)
        full_cuff[:len(cuff)] = cuff
        extra = np.zeros(len(every_P), bool)
        extra[len(all_P):] = True
        add_group(geo, "body", body)
        add_group(geo, "cuff", full_cuff)
        add_group(geo, "extra", extra)
        self.extra_count = int(extra.sum())
        node = self.network.createNode("a3ob_accessory_weights")
        node.setInput(0, make_source(self.network, geo))
        node.setInput(1, self.skeleton())
        node.parm("target").set("cuff")
        node.parm("source").set("body")
        for name, value in parms.items():
            node.parm(name).set(value)
        return node, full_cuff, body

    def weights(self, node):
        return selection_weights(node.geometry(), BONES)


class TestRigid(AccessoryTestCase):
    def test_auto_follows_the_dominant_bone_with_the_twist_rule(self):
        node, cuff, body = self.scene()
        before = selection_weights(node.input(0).geometry(), BONES)
        result = cook(node)
        self.assertEqual(result.errors, ())
        text = "\n".join(result.messages)
        self.assertIn("Rigid: 1 piece.", text)
        self.assertIn("Rigid piece 0: %d points, bone ForeArm." % cuff.sum(), text)  # ArmRoll counts for ForeArm
        weights = self.weights(node)
        self.assertTrue((weights[cuff] == [0.0, 0.0, 0.0, 0.0, 1.0]).all())
        np.testing.assert_array_equal(weights[body], before[body])  # the source keeps its weights
        geo = node.geometry()
        origin = np.array(geo.pointIntAttribValues("a3ob_rig_origin"))
        piece = np.array(geo.pointIntAttribValues("a3ob_rig_piece"))
        self.assertTrue((origin[cuff] == 3).all() and (origin[~cuff] == 0).all())
        self.assertTrue((piece[cuff] == 0).all() and (piece[~cuff] == -1).all())

    def test_merge_twist_off_blend_and_a_picked_bone(self):
        node, cuff, _ = self.scene(merge_twist=0)
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(self.weights(node)[cuff][0].tolist(), [0.0, 0.0, 0.0, 1.0, 0.0])
        node.parm("use").set("blend")
        self.assertEqual(cook(node).errors, ())
        rows = self.weights(node)[cuff]
        self.assertTrue((np.abs(rows - rows[0]) < 1e-6).all())
        self.assertGreater(float(rows[0][1]), 0.0)
        self.assertIn("one fixed blend", "\n".join(node.messages()))
        node.parm("use").set("bone")
        node.parm("bone").set("arm")  # any case
        node.parm("source").set("")  # a picked bone needs no source
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertTrue((self.weights(node)[cuff] == [0.0, 0.0, 1.0, 0.0, 0.0]).all())
        self.assertIn("bone Arm (picked)", "\n".join(result.messages))

    def test_errors_name_the_field(self):
        node, _, _ = self.scene()
        node.parm("target").set("")
        self.assertIn("Target Group", " ".join(cook(node).errors))
        node.parm("target").set("nothing")
        self.assertIn("Target Group", " ".join(cook(node).errors))
        node.parm("target").set("cuff")
        node.parm("source").set("")
        self.assertIn("Source Group", " ".join(cook(node).errors))
        node.parm("source").set("nothing")
        self.assertIn("Source Group", " ".join(cook(node).errors))
        node.parm("source").set("body")
        node.parm("use").set("bone")
        node.parm("bone").set("NoSuchBone")
        self.assertIn("Bone", " ".join(cook(node).errors))
        node.parm("use").set("auto")
        node.setInput(1, node.input(0))
        self.assertIn("not a skeleton", " ".join(cook(node).errors))

    def test_unweighted_source_points_and_far_targets_warn_with_counts(self):
        node, cuff, _ = self.scene(source="body extra")
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("%d points of the Source Group have no bone weight" % self.extra_count, " ".join(result.warnings))
        # a target with no source in reach takes the nearest weighted source point
        node.parm("source").set("body")
        node.parm("distance").set(0.0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("%d target points have no source point within the distance limit" % cuff.sum(), " ".join(result.warnings))
        self.assertTrue((self.weights(node)[cuff].sum(axis=1) > 0.99).all())

    def test_small_pieces_are_ignored(self):
        node, cuff, _ = self.scene(minpiece=1000)
        self.assertEqual(cook(node).errors, ())
        self.assertTrue((self.weights(node)[cuff] == 0).all())
        self.assertIn("ignored", "\n".join(node.messages()))


class TestNonRigid(AccessoryTestCase):
    def test_varies_along_the_piece_and_smoothing_helps(self):
        node, cuff, _ = self.scene(mode="nonrigid", smooth=0, distance=0.5)
        self.assertEqual(cook(node).errors, ())
        rough = self.weights(node)
        node.parm("smooth").set(20)
        self.assertEqual(cook(node).errors, ())
        smooth = self.weights(node)
        self.assertGreater(float(np.ptp(rough[cuff][:, 3])), 0.3)
        self.assertLess(float(np.ptp(smooth[cuff][:, 3])), float(np.ptp(rough[cuff][:, 3])))
        origin = np.array(node.geometry().pointIntAttribValues("a3ob_rig_origin"))
        self.assertTrue((origin[cuff] == 4).all())
        self.assertIn("Non-rigid: 1 piece", "\n".join(node.messages()))


class TestChains(AccessoryTestCase):
    def test_an_accessory_takes_its_weights_from_another_accessory(self):
        # body -> cuff (rigid, from the body) -> pouch (non-rigid, from the cuff), the pouch group made by the test.
        node, cuff, body = self.scene()
        geo = node.input(0).geometry()
        pouch_P, pouch_tris = tube(3, 8, 0.1 + 0.03 + 0.005, 0.1, 1.0)
        joined = hou.Geometry()
        joined.merge(geo)
        count = len(joined.points())
        points = joined.createPoints([tuple(float(v) for v in p) for p in pouch_P])
        for tri in pouch_tris:
            prim = joined.createPolygon()
            for index in tri:
                prim.addVertex(points[int(index)])
        pouch = np.zeros(count + len(pouch_P), bool)
        pouch[count:] = True
        add_group(joined, "pouch", pouch)
        node.setInput(0, make_source(self.network, joined))
        second = self.network.createNode("a3ob_accessory_weights")
        second.setInput(0, node)
        second.setInput(1, node.input(1))
        second.parm("target").set("pouch")
        second.parm("source").set("cuff")
        cleanup = self.network.createNode("a3ob_weight_cleanup")
        cleanup.setInput(0, second)
        cleanup.setInput(1, node.input(1))
        for step in (node, second, cleanup):
            self.assertEqual(cook(step).errors, (), step.path())
        weights = selection_weights(second.geometry(), BONES)
        self.assertTrue((weights[pouch] == weights[cuff.tolist() + [False] * len(pouch_P)][0]).all())
        piece = np.array(second.geometry().pointIntAttribValues("a3ob_rig_piece"))
        self.assertEqual(sorted(set(piece[piece >= 0].tolist())), [0, 1])  # pieces are counted on along the chain
        self.assertIn("Rigid piece 1:", "\n".join(second.messages()))


class TestSkeletonInput(AccessoryTestCase):
    def fit_like(self, skeleton):
        """A geometry shaped like a Rig Fit output: the skeleton as the packed part ``fitted_skeleton``."""
        geo = hou.Geometry()
        rig_weights.write_parts(geo, {rig_weights.PART_SKELETON: skeleton.geometry()})
        return make_source(self.network, geo)

    def test_a_rig_fit_output_gives_its_fitted_skeleton_and_the_bone_menu_lists_the_bones(self):
        node, cuff, _ = self.scene()
        plain = entry.menu("accessory_weights", "bones", {"node": node})
        self.assertEqual(plain[::2], BONES)
        node.setInput(1, self.fit_like(node.input(1)))
        self.assertEqual(entry.menu("accessory_weights", "bones", {"node": node})[::2], BONES)
        self.assertEqual(cook(node).errors, ())
        self.assertTrue((self.weights(node)[cuff] == [0.0, 0.0, 0.0, 0.0, 1.0]).all())

    def test_the_menu_without_a_skeleton_is_empty(self):
        node, _, _ = self.scene()
        node.setInput(1, None)
        self.assertEqual(entry.menu("accessory_weights", "bones", {"node": node}), [])


if __name__ == "__main__":
    unittest.main()

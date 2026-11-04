"""The Weight Transfer node in hython (design tests 2.1 and 2.2 through the node, and the chain to Joint Deform, task #65).
Needs ``hou`` with KineFX: do not run it with HOUDINI_PACKAGE_SKIP=1 (that removes every kinefx:: type).

The test on the real Samples files skips when the Samples are not there.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

import numpy as np

from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_node_rig_skeleton import write_pivots
from tests.test_node_weights import blend, selection_weights
from tests.test_rig_transfer import tube, two_bone_weights, with_cuff

BONES = ["Pelvis", "Spine", "Arm", "ArmRoll", "ForeArm"]
CFG = 'class CfgSkeletons { class Man { skeletonBones[] = {"Pelvis", "", "Spine", "Pelvis", "Arm", "Spine", ' \
      '"ArmRoll", "Arm", "ForeArm", "ArmRoll"}; }; };'
PIVOTS = {"Pelvis": (0.0, 0.0, 0.0), "Spine": (0.0, 0.5, 0.0), "Arm": (0.0, 1.0, 0.0), "ArmRoll": (0.0, 1.1, 0.0),
          "ForeArm": (0.0, 1.5, 0.0)}


def skinned(P, tris, W, names, mixed_case=True):
    """Polygons with a point group per bone (``names`` spelled in lower case, as the importer might) and ``a3ob_w_*``."""
    geo = hou.Geometry()
    points = geo.createPoints([tuple(float(v) for v in p) for p in P])
    for tri in tris:
        prim = geo.createPolygon()
        for index in tri:
            prim.addVertex(points[int(index)])
    for column, name in enumerate(names):
        members = np.nonzero(W[:, column] > 0)[0]
        if not len(members):
            continue
        spelled = name.lower() if mixed_case else name
        group = geo.createPointGroup(hou.text.encodeAttrib(spelled))
        group.add([points[int(i)] for i in members])
        attrib = "a3ob_w_" + hou.text.encodeAttrib(spelled)
        geo.addAttrib(hou.attribType.Point, attrib, 1.0)
        values = np.ones(len(P))
        values[members] = W[members, column]
        geo.setPointFloatAttribValues(attrib, [float(v) for v in values])
    return geo


def add_group(geo, name, mask):
    group = geo.createPointGroup(name)
    group.add([geo.point(int(i)) for i in np.nonzero(mask)[0]])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TransferTestCase(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="a3ob_wtr_")
        self.pivots = os.path.join(self.folder, "pivots.p3d").replace("\\", "/")
        write_pivots(self.pivots, PIVOTS)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def skeleton(self):
        cfg = os.path.join(self.folder, "model.cfg").replace("\\", "/")
        with open(cfg, "w", encoding="utf-8") as file:
            file.write(CFG)
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set(cfg)
        node.parm("pivots").set(self.pivots)
        node.parm("skeleton").set("Man")
        self.assertEqual(cook(node).errors, ())
        return node

    def transfer(self, mesh, body, skeleton, **parms):
        node = self.network.createNode("a3ob_weight_transfer")
        node.setInput(0, mesh)
        node.setInput(1, body)
        node.setInput(2, skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def scene(self, **parms):
        """A source tube weighted to Spine and ArmRoll (lower case selections), a target tube with a cuff on it (point
        group ``cuff``), and the Weight Transfer node on them. Returns ``(node, target geometry, cuff mask)``."""
        P, tris = tube(24, 16)
        W = np.zeros((len(P), 5))
        two = two_bone_weights(P)
        W[:, 1], W[:, 3] = two[:, 0], two[:, 1]  # Spine below, ArmRoll above
        body = make_source(self.network, skinned(P, tris, W, BONES))
        all_P, all_tris, cuff = with_cuff(P, tris)
        target = hou.Geometry()
        target.merge(skinned(all_P, all_tris, np.zeros((len(all_P), 5)), BONES))
        add_group(target, "cuff", cuff)
        mesh = make_source(self.network, target)
        node = self.transfer(mesh, body, self.skeleton(), **parms)
        return node, all_P, cuff


class TestWeightTransferNode(TransferTestCase):
    def test_layers_and_outputs(self):
        node, all_P, cuff = self.scene(rigid="cuff")
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo = node.geometry()
        text = "\n".join(result.messages)
        self.assertIn("First pass:", text)
        self.assertIn("Rigid parts: 1 piece.", text)
        self.assertIn("Loose parts: 0 pieces.", text)
        names = sorted(hou.text.decodeAttrib(g.name()) for g in geo.pointGroups())
        self.assertIn("Spine", names)  # the model.cfg spelling, not "spine"
        self.assertNotIn("spine", names)
        weights = selection_weights(geo, BONES)
        origin = np.array(geo.pointIntAttribValues("a3ob_rig_origin"))
        piece = np.array(geo.pointIntAttribValues("a3ob_rig_piece"))
        self.assertTrue((origin[cuff] == 3).all())
        self.assertTrue((origin[~cuff] > 0).all() and (origin[~cuff] < 3).all())
        self.assertTrue((piece[cuff] == 0).all() and (piece[~cuff] == -1).all())
        np.testing.assert_allclose(weights.sum(axis=1), 1.0, atol=1e-5)
        # the cuff sits on the ArmRoll blend and follows the forearm (twist merged), every point the same
        self.assertTrue((weights[cuff] == weights[cuff][0]).all())
        self.assertEqual(weights[cuff][0].tolist(), [0.0, 0.0, 0.0, 0.0, 1.0])
        self.assertIn("bone ForeArm", text)

    def test_merge_twist_off_keeps_the_roll_bone_and_blend_mode_shares_a_row(self):
        node, _, cuff = self.scene(rigid="cuff", merge_twist=0)
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(selection_weights(node.geometry(), BONES)[cuff][0].tolist(), [0.0, 0.0, 0.0, 1.0, 0.0])
        node.parm("rigid_mode").set("blend")
        self.assertEqual(cook(node).errors, ())
        rows = selection_weights(node.geometry(), BONES)[cuff]
        self.assertTrue((np.abs(rows - rows[0]) < 1e-6).all())
        self.assertGreater(float(rows[0][1]), 0.0)
        self.assertIn("one fixed blend", "\n".join(node.messages()))

    def test_bone_override_attribute_wins(self):
        node, all_P, cuff = self.scene(rigid="cuff")
        geo = hou.Geometry()
        geo.merge(node.input(0).geometry())
        geo.addAttrib(hou.attribType.Point, "a3ob_rig_bone", "")
        index = int(np.nonzero(cuff)[0][3])
        geo.point(index).setAttribValue("a3ob_rig_bone", "arm")
        node.setInput(0, make_source(self.network, geo))
        node.parm("rigid").set("cuff")
        self.assertEqual(cook(node).errors, ())
        weights = selection_weights(node.geometry(), BONES)
        self.assertTrue((weights[cuff] == [0.0, 0.0, 1.0, 0.0, 0.0]).all())
        self.assertIn("a3ob_rig_bone", "\n".join(node.messages()))

    def test_loose_group_varies_and_smoothing_helps(self):
        node, _, cuff = self.scene(nonrigid="cuff", smooth=0)
        self.assertEqual(cook(node).errors, ())
        rough = selection_weights(node.geometry(), BONES)
        node.parm("smooth").set(20)
        self.assertEqual(cook(node).errors, ())
        smooth = selection_weights(node.geometry(), BONES)
        self.assertGreater(float(np.ptp(rough[cuff][:, 3])), 0.3)
        self.assertLess(float(np.ptp(smooth[cuff][:, 3])), float(np.ptp(rough[cuff][:, 3])))
        origin = np.array(node.geometry().pointIntAttribValues("a3ob_rig_origin"))
        self.assertTrue((origin[cuff] == 4).all())
        self.assertIn("Loose parts: 1 piece.", "\n".join(node.messages()))

    def test_other_selections_stay_and_untouched_points_keep_their_weights(self):
        node, _, cuff = self.scene(firstpass="!cuff")  # first pass is everything but the cuff, which is in no layer
        self.assertEqual(cook(node).errors, ())
        geo = node.geometry()
        self.assertIsNotNone(geo.findPointGroup("cuff"))
        self.assertTrue((selection_weights(geo, BONES)[cuff] == 0).all())
        origin = np.array(geo.pointIntAttribValues("a3ob_rig_origin"))
        self.assertTrue((origin[cuff] == 0).all())

    def test_errors(self):
        node, _, _ = self.scene(rigid="cuff")
        node.setInput(1, node.input(0))  # a body with no weights
        self.assertIn("no selection of the source body", " ".join(cook(node).errors))
        node2, _, _ = self.scene(rigid="nothing")
        self.assertTrue(cook(node2).errors)  # Houdini cannot read the group, or no point is in a group
        node3, _, _ = self.scene()
        node3.setInput(2, node3.input(0))
        self.assertIn("not a skeleton", " ".join(cook(node3).errors))


class TestChain(TransferTestCase):
    """Design criterion 4: the output goes through Weight Cleanup, Weights Convert and Joint Deform with no extra step."""

    def test_transfer_cleanup_convert_and_joint_deform(self):
        node, all_P, cuff = self.scene(rigid="cuff")
        skeleton = node.input(2)
        cleanup = self.network.createNode("a3ob_weight_cleanup")
        cleanup.setInput(0, node)
        cleanup.setInput(1, skeleton)
        convert = self.network.createNode("a3ob_weights_convert")
        convert.setInput(0, cleanup)
        convert.setInput(1, skeleton)
        convert.parm("mode").set("tocapture")
        for step in (node, cleanup, convert):
            self.assertEqual(cook(step).errors, (), step.path())
        deform = self.network.createNode("kinefx::jointdeform")
        deform.setInput(0, convert)
        deform.setInput(1, skeleton)
        deform.setInput(2, skeleton)
        self.assertEqual(cook(deform).errors, ())
        before = np.array(convert.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        np.testing.assert_allclose(np.array(deform.geometry().pointFloatAttribValues("P")).reshape(-1, 3), before, atol=1e-5)
        # a turned joint moves the points by the cleaned weights
        posed = hou.Geometry()
        posed.copy(skeleton.geometry())
        c, s = np.cos(0.6), np.sin(0.6)
        matrix = np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])
        posed.points()[3].setAttribValue("transform", tuple(matrix.reshape(9)))
        deform.setInput(2, make_source(self.network, posed))
        self.assertEqual(cook(deform).errors, ())
        rest = np.array([tuple(p.position()) for p in skeleton.geometry().points()])
        rotations = [matrix if i == 3 else np.eye(3) for i in range(5)]
        expected = blend(before, rest, rotations, selection_weights(cleanup.geometry(), BONES).astype(np.float32).astype(np.float64))
        np.testing.assert_allclose(np.array(deform.geometry().pointFloatAttribValues("P")).reshape(-1, 3), expected, atol=1e-5)
        self.assertGreater(float(np.abs(expected - before).max()), 0.01)
        new = selection_weights(cleanup.geometry(), BONES)
        self.assertEqual(np.rint(new.sum(axis=1) * 254).tolist(), [254] * len(new))


SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestOnSamples(NodeTestCase):
    """Resolution 1 weights onto the sample's own Resolution 2 (aligned, another topology), through the node."""

    def lod(self, name):
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(samples_support.path(SAMPLE).replace("\\", "/"))
        extract = self.network.createNode("a3ob_extract_lod")
        extract.setInput(0, importer)
        extract.parm("lod").set(name)
        self.assertEqual(cook(extract).errors, ())
        return extract

    def test_resolution_1_onto_resolution_2_and_the_chain(self):
        skeleton = self.network.createNode("a3ob_rig_skeleton")
        skeleton.parm("cfg").set(samples_support.path(SAMPLE_CFG).replace("\\", "/"))
        skeleton.parm("pivots").set(samples_support.path(SAMPLE_PIVOTS).replace("\\", "/"))
        self.assertEqual(cook(skeleton).errors, ())
        bones = [p.attribValue("name") for p in skeleton.geometry().points()]
        source, target = self.lod("Resolution 1"), self.lod("Resolution 2")
        node = self.network.createNode("a3ob_weight_transfer")
        node.setInput(0, target)
        node.setInput(1, source)
        node.setInput(2, skeleton)
        result = cook(node)
        self.assertEqual(result.errors, ())
        truth = selection_weights(target.geometry(), bones)
        got = selection_weights(node.geometry(), bones)
        proxy = {p.vertex(0).point().number() for p in target.geometry().prims() if isinstance(p, hou.PackedPrim)}
        ok = np.array([i not in proxy for i in range(len(truth))]) & (truth.sum(axis=1) > 0.5)
        l1 = np.abs(got - truth).sum(axis=1)[ok]
        self.assertLess(float(l1.mean()), 0.15)
        print("   Resolution 1 -> 2: L1 mean %.3f; %s" % (l1.mean(), result.messages[0] if result.messages else ""))
        cleanup = self.network.createNode("a3ob_weight_cleanup")
        cleanup.setInput(0, node)
        cleanup.setInput(1, skeleton)
        self.assertEqual(cook(cleanup).errors, ())


if __name__ == "__main__":
    unittest.main()

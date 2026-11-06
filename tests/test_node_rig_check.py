"""The Rig Check node in hython (design tests 2.6 to 2.8 and the volume test, task #68). It needs ``hou`` with KineFX (do not
run it with HOUDINI_PACKAGE_SKIP=1). The made-up cylinder tests need nothing else; the Samples tests skip without the Arma 3
Samples.

Purpose: the node wires the measures of ``a3ob_core.rig.check`` to real geometry: attributes, messages, the Output menu, a
Rig Fit-shaped skeleton input, and the export of its output equal to the export of its input.

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

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"

if HAS_HOU:
    from a3ob_houdini import messages, nodes, rig_weights, settings
    from tests.test_rig_check import cylinder


def attrib(node, name):
    return np.array(node.geometry().pointFloatAttribValues(name))


def bone_geo(points, tris, weights, names, rest):
    """A cylinder mesh with bone selections A and B and the two-joint skeleton, as two ``hou.Geometry``."""
    geo = hou.Geometry()
    for p in points:
        geo.createPoint().setPosition(hou.Vector3(*p))
    for tri in tris:
        poly = geo.createPolygon()
        for number in tri:
            poly.addVertex(geo.point(int(number)))
    for bone, name in enumerate(names):
        group = geo.createPointGroup(hou.text.encodeAttrib(name))
        members = [i for i in range(len(points)) if weights[i, bone] > 0]
        group.add([geo.point(i) for i in members])
        geo.addAttrib(hou.attribType.Point, "a3ob_w_" + group.name(), 1.0)
        geo.setPointFloatAttribValues("a3ob_w_" + group.name(), [float(v) for v in weights[:, bone]])
    skeleton = hou.Geometry()
    skeleton.addAttrib(hou.attribType.Point, "name", "")
    for name, position in zip(names, rest):
        point = skeleton.createPoint()
        point.setPosition(hou.Vector3(*position))
        point.setAttribValue("name", name)
    line = skeleton.createPolygon()  # a bone is a 2-point line from parent to child
    line.addVertex(skeleton.point(0))
    line.addVertex(skeleton.point(1))
    return geo, skeleton


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestRigCheckOnCylinder(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.points, self.tris, self.ring = cylinder()
        wb = np.clip((self.points[:, 0] - 0.8) / 0.4, 0.0, 1.0)
        self.weights = np.stack([1.0 - wb, wb], axis=1)
        self.rest = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
        mesh, skeleton = bone_geo(self.points, self.tris, self.weights, ["A", "B"], self.rest)
        self.mesh, self.skeleton = make_source(self.network, mesh), make_source(self.network, skeleton)

    def check(self, **parms):
        node = self.network.createNode("a3ob_rig_check")
        node.setInput(0, self.mesh)
        node.setInput(1, self.skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def test_it_cooks_and_writes_the_attributes_with_the_geometry_unchanged(self):
        node = self.check()
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo = node.geometry()
        for name in ("a3ob_rig_jump", "a3ob_rig_infl", "a3ob_rig_sumerr", "a3ob_rig_straddle", "a3ob_rig_stress",
                     "a3ob_rig_dominant", "a3ob_rig_volume_change"):
            self.assertIsNotNone(geo.findPointAttrib(name), name)
        np.testing.assert_allclose(np.array(geo.pointFloatAttribValues("P")).reshape(-1, 3), self.points, atol=1e-6)
        self.assertEqual(len(geo.prims()), len(self.tris))
        print("   messages:", " | ".join(result.messages + result.warnings))

    def test_a_pose_the_skeleton_cannot_bend_is_skipped_with_a_warning(self):
        node = self.check(pose="elbows")  # the cylinder has no arm bones
        result = cook(node)
        self.assertIn("skipped", " ".join(result.warnings))
        self.assertEqual(float(np.abs(attrib(node, "a3ob_rig_stress")).max()), 0.0)

    def test_the_output_menu_gives_the_posed_mesh_and_the_skeleton(self):
        node = self.check(view="skeleton")
        self.assertEqual(cook(node).errors, ())
        geo = node.geometry()
        self.assertEqual(len(geo.points()), 2)
        depth = geo.pointFloatAttribValues("a3ob_rig_joint_depth")
        self.assertGreaterEqual(depth[0], 0.0)  # joint A sits on the centre of the cap: on the surface, depth 0
        self.assertGreater(depth[1], 0.05)  # joint B is on the axis in the middle: 0.1 from the wall
        self.assertIsNotNone(geo.findPointAttrib("Cd"))

    def test_visualize_adds_the_button_hint_message_and_off_does_not(self):
        # #78: the colours need the viewport Visualization button; the message says so only while Visualize is on
        from a3ob_houdini import node_visualizer
        self.assertIn(node_visualizer.HINT, cook(self.check(visualize=1)).messages)
        self.assertNotIn(node_visualizer.HINT, cook(self.check(visualize=0)).messages)
        template = self.network.createNode("a3ob_rig_check").parm("visualize").parmTemplate()
        self.assertIn(node_visualizer.HINT, template.help())

    def test_an_input_that_is_not_connected_is_an_error(self):
        node = self.network.createNode("a3ob_rig_check")
        node.setInput(0, self.mesh)
        self.assertTrue(cook(node).errors)  # Houdini itself says that the node needs both inputs

    def test_a_skeleton_that_is_a_rig_fit_output_is_read_through_its_part(self):
        skeleton = hou.Geometry()
        skeleton.copy(self.skeleton.geometry())
        wrapped = hou.Geometry()
        rig_weights.write_parts(wrapped, {rig_weights.PART_SKELETON: skeleton})
        node = self.check()
        node.setInput(1, make_source(self.network, wrapped))
        plain = self.check()
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(cook(plain).errors, ())
        np.testing.assert_allclose(attrib(node, "a3ob_rig_dominant"), attrib(plain, "a3ob_rig_dominant"))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestRigCheckOnSamples(NodeTestCase):
    def setUp(self):
        super().setUp()
        path = samples_support.path
        self.importer = self.network.createNode("a3ob_p3d_import")
        self.importer.parm("file").set(path(SAMPLE).replace("\\", "/"))
        self.body = self.network.createNode("a3ob_extract_lod")
        self.body.setInput(0, self.importer)
        self.body.parm("lod").set("Resolution 1")
        self.skeleton = self.network.createNode("a3ob_rig_skeleton")
        self.skeleton.parm("cfg").set(path(SAMPLE_CFG).replace("\\", "/"))
        self.skeleton.parm("pivots").set(path(SAMPLE_PIVOTS).replace("\\", "/"))
        for node in (self.body, self.skeleton):
            self.assertEqual(cook(node).errors, ())
        self.folder = tempfile.mkdtemp(prefix="a3ob_rigcheck_")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def check(self, source=None, skeleton=None, **parms):
        node = self.network.createNode("a3ob_rig_check")
        node.setInput(0, source or self.body)
        node.setInput(1, skeleton or self.skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def test_it_checks_the_sample_in_every_pose_and_names_the_worst_joints(self):
        for pose in ("worst", "arms_up", "arms_forward", "elbows", "hips", "knees", "spine", "neck", "wrists"):
            with self.subTest(pose=pose):
                node = self.check(pose=pose)
                result = cook(node)
                self.assertEqual(result.errors, ())
                stress, volume = attrib(node, "a3ob_rig_stress"), attrib(node, "a3ob_rig_volume_change")
                self.assertGreater(float(np.abs(stress).max()), 0.0)
                self.assertGreater(float(volume.min()), -1.0000001)
                print("   %s: stress %.3f..%.3f, volume %.3f..%.3f; %s" % (
                    pose, stress.min(), stress.max(), volume.min(), volume.max(),
                    [m for m in result.messages + result.warnings if "Volume" in m or "lose" in m][:1]))
        # worst of all keeps the largest change per point
        worst = attrib(self.check(pose="worst"), "a3ob_rig_stress")
        single = attrib(self.check(pose="knees"), "a3ob_rig_stress")
        self.assertTrue(np.all(np.abs(worst) >= np.abs(single) - 1e-9))

    def test_the_problem_attributes_and_the_joint_depth_make_sense_on_the_sample(self):
        node = self.check(view="skeleton")
        result = cook(node)
        self.assertEqual(result.errors, ())
        depth = np.array(node.geometry().pointFloatAttribValues("a3ob_rig_joint_depth"))
        print("   joints inside the sample mesh: %d of %d" % (int((depth > 0).sum()), len(depth)))
        print("   messages:", " | ".join(result.messages + result.warnings))
        self.assertGreater(int((depth > 0).sum()), len(depth) // 2)
        mesh = self.check()
        self.assertEqual(cook(mesh).errors, ())
        infl = np.array(mesh.geometry().pointIntAttribValues("a3ob_rig_infl"))
        self.assertGreater(int(infl.max()), 0)

    def test_a_rig_fit_shaped_skeleton_gives_the_same_result_as_the_arma_skeleton(self):
        wrapped = hou.Geometry()
        skeleton = hou.Geometry()
        skeleton.copy(self.skeleton.geometry())
        rig_weights.write_parts(wrapped, {rig_weights.PART_SKELETON: skeleton})
        fitted = make_source(self.network, wrapped)
        plain, fit = self.check(pose="elbows"), self.check(skeleton=fitted, pose="elbows")
        self.assertEqual(cook(plain).errors, ())
        self.assertEqual(cook(fit).errors, ())
        for name in ("a3ob_rig_stress", "a3ob_rig_volume_change"):
            np.testing.assert_allclose(attrib(plain, name), attrib(fit, name), atol=1e-12)

    def export(self, source, name):
        out = os.path.join(self.folder, name).replace("\\", "/")
        node = self.network.createNode("a3ob_p3d_export")
        node.setInput(0, source)
        node.parm("file").set(out)
        nodes.load("p3d_export").apply_preset(node, "Lossless", settings.Settings())
        node.parm("file").set(out)
        node.parm("maxmessages").set(100000)
        report = nodes.load("p3d_export").run_save(node)
        self.assertFalse(report.has_errors, report.texts(messages.ERROR))
        return out

    def test_design_2_8_the_export_of_the_output_equals_the_export_of_the_input(self):
        plain = self.network.createNode("a3ob_assemble_lods")
        plain.setInput(0, self.importer)
        plain.setInput(1, self.body)
        checked = self.network.createNode("a3ob_assemble_lods")
        checked.setInput(0, self.importer)
        checked.setInput(1, self.check())
        for node in (plain, checked):
            self.assertEqual(cook(node).errors, ())
        first, second = self.export(plain, "plain.p3d"), self.export(checked, "checked.p3d")
        with open(first, "rb") as one, open(second, "rb") as two:
            self.assertEqual(one.read(), two.read())


if __name__ == "__main__":
    unittest.main()

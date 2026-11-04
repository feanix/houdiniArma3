"""The Rig Skeleton node in hython (design test 1.1, task #61): the KineFX skeleton it writes, its messages and errors,
the Skeleton menu, the Samples defaults, and that Rig Pose, Joint Capture and Joint Deform accept the result. Needs ``hou``
with KineFX: do not run it with HOUDINI_PACKAGE_SKIP=1 (that removes every kinefx:: type).

The test on the real Samples files skips when the Samples are not there.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from a3ob_core import p3d
from a3ob_core.rig import read_pivots_file
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook

CFG = '''
class CfgSkeletons {
    class Man {
        isDiscrete = 0; skeletonInherit = "";
        skeletonBones[] = {"Pelvis", "", "Spine", "Pelvis", "head", "Spine", "Face_Jaw", "head", "Camera", "head",
                           "Arm", "Spine"};
        pivotsModel = "a3\\anims_f\\pivots.p3d";
    };
    class Other { skeletonBones[] = {"Root", ""}; };
};
'''
PIVOTS = {"pelvis": (0.0, 1.0, 0.5), "Spine": (0.0, 1.4, 0.5), "head": (0.0, 1.7, 0.25), "Arm": (0.3, 1.5, 0.5),
          "Hips": (0.0, 0.0, 0.0), "Face_Jaw": (0.0, 1.6, 0.2)}


def write_pivots(path, pivots):
    lod = p3d.LOD()
    lod.set_lod_type(p3d.LodType.MEMORY)
    for name, position in pivots.items():
        lod.add_selection(name, {lod.add_point(position): 1.0})
    p3d.write_p3d_file(p3d.P3DModel([lod]), path)


def joints(geo):
    """``{name: (position, parent name or None)}`` of a skeleton geometry."""
    parent = {}
    for prim in geo.prims():
        a, b = [v.point().number() for v in prim.vertices()]
        parent[b] = a
    names = [p.attribValue("name") for p in geo.points()]
    return {names[p.number()]: (tuple(p.position()), names[parent[p.number()]] if p.number() in parent else None)
            for p in geo.points()}


def rounded(position):
    """Houdini positions are float32; round to compare with the decimals typed in a test."""
    return tuple(round(value, 5) for value in position)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestRigSkeletonNode(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="a3ob_rig_")
        self.cfg = os.path.join(self.folder, "model.cfg").replace("\\", "/")
        self.pivots = os.path.join(self.folder, "pivots.p3d").replace("\\", "/")
        with open(self.cfg, "w", encoding="utf-8") as file:
            file.write(CFG)
        write_pivots(self.pivots, PIVOTS)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def node(self, cfg=None, pivots=None, skeleton=""):
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set(self.cfg if cfg is None else cfg)
        node.parm("pivots").set(self.pivots if pivots is None else pivots)
        node.parm("skeleton").set(skeleton)
        return node

    def test_joints_parents_positions_and_the_kinefx_attributes(self):
        node = self.node()
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo = node.geometry()
        found = joints(geo)
        # Houdini axes: (x, y, -z). Names use the model.cfg spelling ("head", not the pivots file's case).
        self.assertEqual(list(found), ["Pelvis", "Spine", "head", "Arm"])
        self.assertEqual((rounded(found["Pelvis"][0]), found["Pelvis"][1]), ((0.0, 1.0, -0.5), None))
        self.assertEqual((rounded(found["head"][0]), found["head"][1]), ((0.0, 1.7, -0.25), "Spine"))
        self.assertEqual((rounded(found["Arm"][0]), found["Arm"][1]), ((0.3, 1.5, -0.5), "Spine"))
        self.assertEqual(geo.attribValue("a3ob_rig_discrete"), 0)  # the model.cfg says isDiscrete = 0 (task #62)
        self.assertEqual(geo.findPointAttrib("transform").size(), 9)
        for point in geo.points():
            self.assertEqual(tuple(point.attribValue("transform")), (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0))
        for prim in geo.prims():  # parent first, child second
            self.assertEqual(len(prim.vertices()), 2)
            self.assertLess(prim.vertices()[0].point().number(), prim.vertices()[1].point().number())
        self.assertFalse(prim.isClosed())

    def test_the_info_lines_report_the_counts_and_what_was_left_out(self):
        result = cook(self.node())
        text = "\n".join(result.messages)
        self.assertIn("4 joints from the 6 bones of Man", text)
        self.assertIn("1 face and eye bones", text)
        self.assertIn("No pivot for bones Camera", text)
        self.assertIn("Hips", text)
        self.assertIn("a3\\anims_f\\pivots.p3d", text)  # pivotsModel is named, not read

    def test_the_skeleton_menu_lists_the_classes_and_the_choice_is_used(self):
        node = self.node()
        items = node.parm("skeleton").menuItems()
        self.assertEqual(items, ("Man", "Other"))
        self.assertIn("Man (6 bones)", node.parm("skeleton").menuLabels())
        node.parm("skeleton").set("Other")
        result = cook(node)
        self.assertIn("no bone of Other has a pivot", " ".join(result.errors))

    def test_empty_fields_say_what_to_set(self):
        for kwargs, expected in (({"cfg": ""}, "Model Config"), ({"pivots": ""}, "Pivots File")):
            node = self.node(**kwargs)
            node.parm("cfg").set(kwargs.get("cfg", self.cfg))
            node.parm("pivots").set(kwargs.get("pivots", self.pivots))
            error = " ".join(cook(node).errors)
            self.assertIn("no ", error)
            self.assertIn(expected, error)
            self.assertIn("Arma 3 Samples folder", error)

    def test_a_missing_file_a_bad_cfg_and_a_bad_skeleton(self):
        self.assertIn("does not exist", " ".join(cook(self.node(cfg=self.folder + "/none.cfg")).errors))
        bad = os.path.join(self.folder, "bad.cfg").replace("\\", "/")
        with open(bad, "w", encoding="utf-8") as file:
            file.write('class CfgSkeletons { class S { skeletonBones[] = {"a", "", "A", "a"}; }; };')
        self.assertIn("listed twice", " ".join(cook(self.node(cfg=bad)).errors))
        with open(bad, "w", encoding="utf-8") as file:
            file.write("class CfgSkeletons {")
        self.assertIn("cannot be read", " ".join(cook(self.node(cfg=bad)).errors))
        with open(bad, "w", encoding="utf-8") as file:
            file.write("class CfgModels {};")
        self.assertIn("no skeleton class", " ".join(cook(self.node(cfg=bad)).errors))

    def test_an_include_line_is_a_warning(self):
        path = os.path.join(self.folder, "inc.cfg").replace("\\", "/")
        with open(path, "w", encoding="utf-8") as file:
            file.write('#include "more.hpp"\n' + CFG)
        self.assertIn("not followed", " ".join(cook(self.node(cfg=path)).warnings))

    def test_a_new_node_starts_from_the_samples_files(self):
        from a3ob_houdini import nodes, settings
        root = os.path.join(self.folder, "Samples")
        for parts in (settings.SAMPLES_CFG, settings.SAMPLES_PIVOTS):
            os.makedirs(os.path.join(root, *parts[:-1]), exist_ok=True)
            open(os.path.join(root, *parts), "w").close()
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set("")
        node.parm("pivots").set("")
        with mock.patch.dict(os.environ, {settings.ENV_SAMPLES: ""}):
            nodes.load("rig_skeleton").created(node, settings.Settings(samples_dir=root))
        self.assertTrue(node.parm("cfg").eval().endswith("Test_Character_01/model.cfg"))
        self.assertTrue(node.parm("pivots").eval().endswith("TemplateRTM/ManSkeleton_Pivots.p3d"))
        other = self.network.createNode("a3ob_rig_skeleton")  # its OnCreated event already ran with the real settings
        other.parm("cfg").set("")
        with mock.patch.dict(os.environ, {settings.ENV_SAMPLES: ""}):  # the variable would win over the setting
            nodes.load("rig_skeleton").created(other, settings.Settings(samples_dir=os.path.join(self.folder, "nowhere")))
        self.assertEqual(other.parm("cfg").eval(), "")

    def test_rig_pose_joint_capture_and_joint_deform_accept_the_skeleton(self):
        skeleton = self.node()
        pose = self.network.createNode("kinefx::rigpose")
        pose.setInput(0, skeleton)
        self.assertEqual(cook(pose).errors, ())
        self.assertEqual(len(pose.geometry().points()), 4)
        # A tube along the body, captured to the skeleton and deformed with the rest pose: nothing may move.
        box = self.network.createNode("box")
        box.parmTuple("size").set((0.4, 1.0, 0.4))
        box.parmTuple("t").set((0.0, 1.3, -0.4))
        divide = self.network.createNode("subdivide")
        divide.setInput(0, box)
        capture = self.network.createNode("kinefx::jointcaptureproximity")
        capture.setInput(0, divide)
        capture.setInput(1, skeleton)
        self.assertEqual(cook(capture).errors, ())
        deform = self.network.createNode("kinefx::jointdeform")
        deform.setInput(0, capture)
        deform.setInput(1, skeleton)
        deform.setInput(2, pose)
        self.assertEqual(cook(deform).errors, ())
        before = capture.geometry().pointFloatAttribValues("P")
        after = deform.geometry().pointFloatAttribValues("P")
        self.assertEqual(len(before), len(after))
        self.assertLess(max(abs(a - b) for a, b in zip(before, after)), 1e-4)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestRigSkeletonOnSamples(NodeTestCase):
    """Design test 1.1: the Samples character skeleton, joint by joint against a direct read of the pivots file."""

    def test_sample_skeleton(self):
        cfg = samples_support.path("Addons/Test_Character_01/model.cfg").replace("\\", "/")
        pivots = samples_support.path("Addons/TemplateRTM/ManSkeleton_Pivots.p3d").replace("\\", "/")
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set(cfg)
        node.parm("pivots").set(pivots)
        result = cook(node)
        self.assertEqual(result.errors, ())
        found = joints(node.geometry())
        self.assertEqual(len(found), 64)
        self.assertFalse([name for name in found if name.startswith(("Face_", "Eye"))])
        for name in ("Camera", "weapon", "launcher", "Hips"):
            self.assertNotIn(name, found)
        direct = {key.lower(): value for key, value in read_pivots_file(pivots).items()}
        for name, (position, _) in found.items():
            x, y, z = direct[name.lower()]
            self.assertEqual(position, (x, y, -z), name)
        # parents before children, and every parent is a joint
        order = list(found)
        for name, (_, parent) in found.items():
            if parent is not None:
                self.assertLess(order.index(parent), order.index(name))
        self.assertIn("Pelvis", found)
        self.assertEqual([n for n, (_, p) in found.items() if p is None], ["Pelvis"])
        text = "\n".join(result.messages)
        self.assertIn("64 joints from the 103 bones of OFP2_ManSkeleton", text)
        self.assertIn("Camera", text)
        self.assertIn("Hips", text)
        self.assertEqual(result.warnings, ())
        # the picker lists the classes of this file
        self.assertIn("OFP2_ManSkeleton", node.parm("skeleton").menuItems())


if __name__ == "__main__":
    unittest.main()

"""The Weights Convert and Weight Cleanup nodes in hython (design tests 1.4 to 1.6, task #62). Needs ``hou`` with KineFX: do
not run it with HOUDINI_PACKAGE_SKIP=1 (that removes every kinefx:: type).

The tests on the real Samples files skip when the Samples are not there.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

import numpy as np

from a3ob_houdini import geo_edit
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_node_rig_skeleton import CFG, PIVOTS, write_pivots

BONES = ["Pelvis", "Spine", "head", "Arm"]
DISCRETE_CFG = CFG.replace("isDiscrete = 0", "isDiscrete = 1")


def group_name(name):
    return hou.text.encodeAttrib(name)


def selection_weights(geo, bones):
    """``(points x bones)`` weights of the bone selections, read straight from the geometry (names ignoring case)."""
    out = np.zeros((len(geo.points()), len(bones)))
    lower = [b.lower() for b in bones]
    for group in geo.pointGroups():
        name = hou.text.decodeAttrib(group.name())
        if name.lower() not in lower:
            continue
        attrib = geo.findPointAttrib("a3ob_w_" + group.name())
        values = geo.pointFloatAttribValues(attrib.name()) if attrib is not None else None
        for point in group.points():
            out[point.number(), lower.index(name.lower())] = 1.0 if values is None else values[point.number()]
    return out


def make_mesh(weights, names, extra_groups=("Component01",), polygons=False):
    """A point cloud (no skin needed) with a selection per name; ``weights`` is points x len(names)."""
    geo = hou.Geometry()
    count = len(weights)
    rng = np.random.default_rng(5)
    positions = np.column_stack([rng.uniform(-0.2, 0.2, count), rng.uniform(0.9, 1.9, count),
                                 rng.uniform(-0.7, -0.3, count)])
    points = geo.createPoints([tuple(float(v) for v in p) for p in positions])
    for column, name in enumerate(names):
        encoded = group_name(name)
        group = geo.createPointGroup(encoded)
        members = [i for i in range(count) if weights[i][column] > 0]
        group.add([points[i] for i in members])
        if any(weights[i][column] != 1.0 for i in members):
            attrib = "a3ob_w_" + encoded
            geo.addAttrib(hou.attribType.Point, attrib, 1.0)
            geo.setPointFloatAttribValues(attrib, [float(max(weights[i][column], 0.0)) if i in members else 1.0
                                                   for i in range(count)])
    for name in extra_groups:
        geo.createPointGroup(name).add(points[:3])
    if polygons:
        prim = geo.createPolygon()
        for point in points[:3]:
            prim.addVertex(point)
        geo.createPrimGroup(group_name(names[0])).add(prim)
        geo.addAttrib(hou.attribType.Prim, "a3ob_fw_" + group_name(names[0]), 1.0)
        geo.setPrimFloatAttribValues("a3ob_fw_" + group_name(names[0]), [0.5])
    return geo


def random_weights(count, width, seed=1):
    rng = np.random.default_rng(seed)
    w = np.zeros((count, width))
    for row in w:
        bones = rng.choice(width, size=int(rng.integers(1, min(4, width) + 1)), replace=False)
        units = rng.integers(1, 100, size=len(bones)).astype(float)
        row[bones] = (np.rint(units / units.sum() * 254) / 254)
    return w


def blend(positions, rest, rotations, weights):
    """Numpy linear blend skinning in Houdini's row-vector form: ``x' = sum_j w_j ((x - P_j) M_j + P_j)``."""
    total = weights.sum(axis=1, keepdims=True)
    weights = np.divide(weights, total, out=np.zeros_like(weights), where=total > 0)  # Joint Deform normalises
    out = np.zeros_like(positions)
    for j in range(weights.shape[1]):
        out += weights[:, j:j + 1] * ((positions - rest[j]) @ rotations[j] + rest[j])
    return np.where(total > 0, out, positions)  # a point with no weight stays


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class WeightsTestCase(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="a3ob_wgt_")
        self.pivots = os.path.join(self.folder, "pivots.p3d").replace("\\", "/")
        write_pivots(self.pivots, PIVOTS)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def skeleton(self, text=CFG):
        cfg = os.path.join(self.folder, "model%d.cfg" % len(os.listdir(self.folder))).replace("\\", "/")
        with open(cfg, "w", encoding="utf-8") as file:
            file.write(text)
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set(cfg)
        node.parm("pivots").set(self.pivots)
        node.parm("skeleton").set("Man")
        self.assertEqual(cook(node).errors, ())
        return node

    def convert(self, source, skeleton, mode):
        node = self.network.createNode("a3ob_weights_convert")
        node.setInput(0, source)
        node.setInput(1, skeleton)
        node.parm("mode").set(mode)
        return node

    def cleanup(self, source, skeleton, **parms):
        node = self.network.createNode("a3ob_weight_cleanup")
        node.setInput(0, source)
        node.setInput(1, skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node


class TestWeightsConvert(WeightsTestCase):
    def test_round_trip_with_mixed_case_keeps_weights_and_other_selections(self):
        w = random_weights(60, 4)
        w[0] = [0.0, 0.0, 0.0, 0.0]
        names = ["pelvis", "SPINE", "head", "Arm"]
        geo = make_mesh(w, names, polygons=True)
        geo_edit.set_detail_array(geo, "a3ob_tagg_order", "string", ["SPINE", "Component01"])
        source = make_source(self.network, geo)
        skeleton = self.skeleton()
        forward = self.convert(source, skeleton, "tocapture")
        result = cook(forward)
        self.assertEqual(result.errors, ())
        self.assertIn("Capture weights for 4 joints from 4 bone selections", "\n".join(result.messages))
        self.assertIn("Component01", "\n".join(result.messages))
        capture = forward.geometry()
        self.assertIsNotNone(capture.findPointAttrib("boneCapture"))
        unpacked = hou.Geometry()
        hou.sopNodeTypeCategory().nodeVerb("captureattribunpack").execute(unpacked, [capture])
        self.assertEqual(tuple(unpacked.attribValue("boneCapture_pCaptPath")), tuple(BONES))
        back = self.convert(forward, skeleton, "toselections")
        result = cook(back)
        self.assertEqual(result.errors, ())
        out = back.geometry()
        names_out = sorted(hou.text.decodeAttrib(g.name()) for g in out.pointGroups())
        self.assertEqual(names_out, sorted(["Pelvis", "Spine", "head", "Arm", "Component01"]))  # model.cfg spelling
        self.assertEqual([p.number() for p in out.findPointGroup("Component01").points()], [0, 1, 2])
        np.testing.assert_allclose(selection_weights(out, BONES), w, atol=1 / 254 / 2 + 1e-6)
        self.assertEqual(len(out.findPrimGroup("Pelvis").prims()), 1)  # the prim group moved with its name
        self.assertIsNone(out.findPrimGroup("pelvis"))
        self.assertEqual(out.primFloatAttribValues("a3ob_fw_Pelvis"), (0.5,))
        self.assertIn("renamed", "\n".join(result.messages))
        self.assertEqual(tuple(out.attribValue("a3ob_tagg_order")), ("Spine", "Component01"))  # the export order hint

    def test_keep_spelling_option(self):
        w = random_weights(20, 4)
        source = make_source(self.network, make_mesh(w, ["pelvis", "SPINE", "head", "Arm"]))
        skeleton = self.skeleton()
        forward = self.convert(source, skeleton, "tocapture")
        back = self.convert(forward, skeleton, "toselections")
        back.parm("case").set("keep")
        self.assertEqual(cook(back).errors, ())
        names_out = sorted(hou.text.decodeAttrib(g.name()) for g in back.geometry().pointGroups())
        self.assertEqual(names_out, ["Arm", "Component01", "SPINE", "head", "pelvis"])

    def test_zero_weight_members_are_ignored_and_reported(self):
        w = np.array([[0.5, 0.5, 0, 0], [1.0, 0, 0, 0], [0, 0, 0, 0]], dtype=float)
        geo = make_mesh(w, BONES)
        # point 2 is a member of "head" with weight 0 (byte 255 in the file)
        geo.findPointGroup("head").add(geo.point(2))
        geo.addAttrib(hou.attribType.Point, "a3ob_w_head", 1.0)
        geo.setPointFloatAttribValues("a3ob_w_head", [1.0, 1.0, 0.0])
        node = self.convert(make_source(self.network, geo), self.skeleton(), "tocapture")
        result = cook(node)
        self.assertIn("1 members with weight 0", "\n".join(result.messages))
        pack = hou.Geometry()
        hou.sopNodeTypeCategory().nodeVerb("captureattribunpack").execute(pack, [node.geometry()])
        self.assertEqual(pack.point(2).attribValue("boneCapture_index"), ())

    def test_errors(self):
        w = random_weights(5, 4)
        source = make_source(self.network, make_mesh(w, BONES))
        node = self.network.createNode("a3ob_weights_convert")
        node.setInput(0, source)
        self.assertTrue(cook(node).errors)  # Houdini itself asks for both inputs
        node.setInput(1, source)
        self.assertIn("not a skeleton", " ".join(cook(node).errors))
        node.setInput(1, self.skeleton())
        node.parm("mode").set("toselections")
        self.assertIn("no capture weights", " ".join(cook(node).errors))
        clash = make_source(self.network, make_mesh(w, ["head", "Head"] + BONES[:1]))
        both = self.convert(clash, self.skeleton(), "tocapture")
        self.assertIn("differ only in case", " ".join(cook(both).errors))


class TestJointDeform(WeightsTestCase):
    """Design test 1.5: Joint Deform with our capture."""

    def deformed(self, capture, skeleton, posed):
        deform = self.network.createNode("kinefx::jointdeform")
        deform.setInput(0, capture)
        deform.setInput(1, skeleton)
        deform.setInput(2, posed)
        self.assertEqual(cook(deform).errors, ())
        return np.array(deform.geometry().pointFloatAttribValues("P")).reshape(-1, 3)

    def posed(self, skeleton, joint, rotation):
        geo = hou.Geometry()
        geo.copy(skeleton.geometry())
        geo.points()[joint].setAttribValue("transform", tuple(rotation))
        return make_source(self.network, geo)

    def test_rest_pose_returns_every_point_and_one_rotated_joint_matches_blend_skinning(self):
        w = random_weights(300, 4, seed=9)
        names = ["Pelvis", "Spine", "head", "Arm"]
        source = make_source(self.network, make_mesh(w, names))
        skeleton = self.skeleton()
        capture = self.convert(source, skeleton, "tocapture")
        self.assertEqual(cook(capture).errors, ())
        rest = np.array([tuple(p.position()) for p in skeleton.geometry().points()])
        before = np.array(source.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        # rest pose
        np.testing.assert_allclose(self.deformed(capture, skeleton, skeleton), before, atol=1e-5)
        # the Spine joint (number 1) turned 40 degrees about Z, the others at rest
        c, s = np.cos(np.radians(40)), np.sin(np.radians(40))
        matrix = np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])
        posed = self.posed(skeleton, 1, matrix.reshape(9))
        rotations = [np.eye(3), matrix, np.eye(3), np.eye(3)]
        result = self.deformed(capture, skeleton, posed)
        np.testing.assert_allclose(result, blend(before, rest, rotations, w.astype(np.float32).astype(np.float64)), atol=1e-5)
        self.assertGreater(float(np.abs(result - before).max()), 0.05)  # the pose did move points

    def test_a_skeleton_with_rotated_joints_at_rest_still_gives_no_movement(self):
        """The inverse rest matrix has to include the joint's own rotation (a Rig Fit skeleton will have one)."""
        w = random_weights(100, 4, seed=2)
        source = make_source(self.network, make_mesh(w, BONES))
        base = self.skeleton()
        c, s = np.cos(0.7), np.sin(0.7)
        rotated = self.posed(base, 2, (c, s, 0.0, -s, c, 0.0, 0.0, 0.0, 1.0))
        capture = self.convert(source, rotated, "tocapture")
        self.assertEqual(cook(capture).errors, ())
        before = np.array(source.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        np.testing.assert_allclose(self.deformed(capture, rotated, rotated), before, atol=1e-5)


class TestWeightCleanup(WeightsTestCase):
    def test_design_test_1_6(self):
        rows = [
            [0.3, 0.25, 0.2, 0.15, 0.1, 0.0],       # 5 bones
            [0.2, 0.2, 0.2, 0.15, 0.1, 0.15],       # 6 bones
            [0.4, 0.2, 0.1, 0.0, 0.0, 0.0],         # sum 0.7
            [0.0] * 6,                              # no weight
            [0.5, 0.496, 0.004, 0.0, 0.0, 0.0],     # a weight of 0.004
            [0.5, 0.5, 0.0, 0.0, 0.0, 0.0],
        ]
        names = ["a%d" % i for i in range(6)]
        cfg = 'class CfgSkeletons { class Man { skeletonBones[] = {%s}; }; };' % ", ".join(
            '"%s", "%s"' % (n, "" if i == 0 else names[0]) for i, n in enumerate(names))
        pivots = dict((n, (0.1 * i, 1.0, 0.5)) for i, n in enumerate(names))
        write_pivots(self.pivots, pivots)
        w = np.array(rows)
        source = make_source(self.network, make_mesh(w, names))
        skeleton = self.skeleton(cfg)
        node = self.cleanup(source, skeleton)
        result = cook(node)
        self.assertEqual(result.errors, ())
        out = node.geometry()
        new = selection_weights(out, names)
        units = np.rint(new * 254)
        np.testing.assert_allclose(new * 254, units, atol=1e-3)
        self.assertLessEqual(int((new > 0).sum(axis=1).max()), 4)
        self.assertEqual(units.sum(axis=1).tolist(), [254, 254, 254, 0, 254, 254])
        old = w
        changed = np.array(out.pointFloatAttribValues("a3ob_rig_changed"))
        np.testing.assert_allclose(changed, np.abs(new - old).sum(axis=1), atol=1e-5)
        text = "\n".join(result.messages)
        self.assertIn("6 points, 6 bone selections. 2 points had more than 4 bones. 1 weights below 0.01 were dropped.", text)
        self.assertIn("1 points had a weight sum other than 1", text)
        self.assertIn("1 of 6 points have no bone weight", " ".join(result.warnings))
        self.assertEqual(changed[3], 0.0)
        self.assertEqual(changed[5], 0.0)

    def test_discrete_skeleton_gives_one_bone_per_point(self):
        w = random_weights(50, 4, seed=4)
        source = make_source(self.network, make_mesh(w, BONES))
        node = self.cleanup(source, self.skeleton(DISCRETE_CFG))
        result = cook(node)
        self.assertEqual(result.errors, ())
        new = selection_weights(node.geometry(), BONES)
        self.assertTrue(((new > 0).sum(axis=1) == 1).all())
        self.assertTrue(((new.max(axis=1)) == 1.0).all())
        np.testing.assert_array_equal(new.argmax(axis=1), w.argmax(axis=1))
        self.assertIn("isDiscrete = 1", "\n".join(result.messages))

    def test_other_selections_and_names_stay(self):
        w = random_weights(30, 4, seed=6)
        source = make_source(self.network, make_mesh(w, ["pelvis", "SPINE", "head", "Arm"]))
        node = self.cleanup(source, self.skeleton())
        self.assertEqual(cook(node).errors, ())
        out = node.geometry()
        self.assertEqual(sorted(g.name() for g in out.pointGroups()), ["Arm", "Component01", "SPINE", "head", "pelvis"])
        self.assertEqual([p.number() for p in out.findPointGroup("Component01").points()], [0, 1, 2])


SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestOnSamples(NodeTestCase):
    """Design tests 1.4, 1.5 and 1.6 on the Samples character (read from the owner's disk, never stored)."""

    def lod(self, name):
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(samples_support.path(SAMPLE).replace("\\", "/"))
        extract = self.network.createNode("a3ob_extract_lod")
        extract.setInput(0, importer)
        extract.parm("lod").set(name)
        self.assertEqual(cook(extract).errors, ())
        return extract

    def skeleton(self):
        node = self.network.createNode("a3ob_rig_skeleton")
        node.parm("cfg").set(samples_support.path(SAMPLE_CFG).replace("\\", "/"))
        node.parm("pivots").set(samples_support.path(SAMPLE_PIVOTS).replace("\\", "/"))
        self.assertEqual(cook(node).errors, ())
        return node

    def wire(self, kind, mesh, skeleton, **parms):
        node = self.network.createNode(kind)
        node.setInput(0, mesh)
        node.setInput(1, skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def test_round_trip_of_resolution_1(self):
        mesh = self.lod("Resolution 1")
        skeleton = self.skeleton()
        bones = [p.attribValue("name") for p in skeleton.geometry().points()]
        before = selection_weights(mesh.geometry(), bones)
        forward = self.wire("a3ob_weights_convert", mesh, skeleton, mode="tocapture")
        result = cook(forward)
        self.assertEqual(result.errors, ())
        self.assertIn("members with weight 0", "\n".join(result.messages))
        back = self.wire("a3ob_weights_convert", forward, skeleton, mode="toselections")
        self.assertEqual(cook(back).errors, ())
        out = back.geometry()
        after = selection_weights(out, bones)
        zero_member = before <= 0
        self.assertLessEqual(float(np.abs(after - before).max()), 1 / 254)
        self.assertTrue((after[zero_member] == 0).all())
        # the bone selections now have the model.cfg spelling; every other selection is as it was
        in_names = {hou.text.decodeAttrib(g.name()) for g in mesh.geometry().pointGroups()}
        out_names = {hou.text.decodeAttrib(g.name()) for g in out.pointGroups()}
        lower = {b.lower(): b for b in bones}
        self.assertEqual(out_names, {lower.get(n.lower(), n) for n in in_names})
        for name in in_names:
            if name.lower() not in lower:
                self.assertEqual([p.number() for p in mesh.geometry().findPointGroup(hou.text.encodeAttrib(name)).points()],
                                 [p.number() for p in out.findPointGroup(hou.text.encodeAttrib(name)).points()], name)

    def test_joint_deform_at_rest_and_with_one_joint_turned(self):
        mesh = self.lod("Resolution 1")
        skeleton = self.skeleton()
        names = [p.attribValue("name") for p in skeleton.geometry().points()]
        rest = np.array([tuple(p.position()) for p in skeleton.geometry().points()])
        capture = self.wire("a3ob_weights_convert", mesh, skeleton, mode="tocapture")
        self.assertEqual(cook(capture).errors, ())
        before = np.array(mesh.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        weights = selection_weights(mesh.geometry(), names)
        weights[weights < 0] = 0
        posed_geo = hou.Geometry()
        posed_geo.copy(skeleton.geometry())
        joint = names.index("LeftForeArm")
        c, s = np.cos(np.radians(50)), np.sin(np.radians(50))
        matrix = np.array([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]])
        posed_geo.points()[joint].setAttribValue("transform", tuple(matrix.reshape(9)))
        posed = make_source(self.network, posed_geo)
        for pose, rotations, label in ((skeleton, [np.eye(3)] * len(names), "rest"),
                                       (posed, [matrix if i == joint else np.eye(3) for i in range(len(names))], "bent")):
            deform = self.network.createNode("kinefx::jointdeform")
            deform.setInput(0, capture)
            deform.setInput(1, skeleton)
            deform.setInput(2, pose)
            self.assertEqual(cook(deform).errors, (), label)
            result = np.array(deform.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
            expected = blend(before, rest, rotations, weights)
            self.assertLess(float(np.abs(result - expected).max()), 1e-5, label)
            if label == "rest":
                self.assertLess(float(np.abs(result - before).max()), 1e-5)
            else:
                self.assertGreater(float(np.abs(result - before).max()), 0.01)

    def test_cleanup_on_resolution_1_changes_only_the_off_sum_points(self):
        mesh = self.lod("Resolution 1")
        skeleton = self.skeleton()
        bones = [p.attribValue("name") for p in skeleton.geometry().points()]
        before = selection_weights(mesh.geometry(), bones)
        node = self.wire("a3ob_weight_cleanup", mesh, skeleton)
        result = cook(node)
        self.assertEqual(result.errors, ())
        after = selection_weights(node.geometry(), bones)
        changed = np.array(node.geometry().pointFloatAttribValues("a3ob_rig_changed"))
        sums = before.sum(axis=1)
        moved = changed > 1e-6
        self.assertTrue((moved <= (np.abs(sums - 1.0) > 1e-5) | (before.max(axis=1) < 0.01 + 1e-6)).all(),
                        "points that changed although their sum was 1")
        self.assertTrue((np.rint(after.sum(axis=1) * 254)[sums > 0] == 254).all())
        self.assertEqual(int((after > 0).sum(axis=1).max()), 4)
        text = "\n".join(result.messages)
        self.assertIn("points had a weight sum other than 1", text)
        self.assertIn("members with weight 0", text)

    def test_cleanup_on_the_lod_with_sum_two(self):
        mesh = self.lod("Resolution 4")
        skeleton = self.skeleton()
        bones = [p.attribValue("name") for p in skeleton.geometry().points()]
        before = selection_weights(mesh.geometry(), bones)
        node = self.wire("a3ob_weight_cleanup", mesh, skeleton)
        result = cook(node)
        self.assertEqual(result.errors, ())
        after = selection_weights(node.geometry(), bones)
        two = np.abs(before.sum(axis=1) - 2.0) < 1e-5
        # 29 points are in both Spine3 and Head; 21 of them are 100 % in each (sum 2.0). The other 8 have 0.476 or 0.5
        # in Spine3 and sum to 1.476 or 1.5 (task #63 checked this in the file, not a missing joint).
        self.assertEqual(int(two.sum()), 21)
        np.testing.assert_array_equal(np.rint(after[two] * 254).sum(axis=1), np.full(21, 254))
        self.assertLessEqual(float(np.abs(np.array(node.geometry().pointFloatAttribValues("a3ob_rig_changed"))[two] - 1.0).max()), 1e-5)


if __name__ == "__main__":
    unittest.main()

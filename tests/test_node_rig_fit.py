"""The Rig Fit node in hython (design tests 2.3, fit part, and 2.4, task #66). It needs ``hou`` with KineFX (do not run it
with HOUDINI_PACKAGE_SKIP=1) and the Arma 3 Samples (it skips without them).

The user's mesh is the sample character itself, posed and sized as in design test 2.3 (the spike's scenario C: arms 20
degrees down, legs 8 degrees apart, arms 10 % and legs 8 % longer, the whole body 5 % bigger), so the true joints are known.
The landmarks are point groups on that mesh (one extra point each), as the node asks for them.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import BONE_OF, LANDMARK_NAMES
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"

if HAS_HOU:
    from a3ob_core.rig import CHIN_FROM_HEAD, drop_small_pieces, make_template
    from a3ob_houdini import rig_weights
    from a3ob_houdini.nodes.weight_transfer import _mesh


def rotation_z(degrees):
    a = np.radians(degrees)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestRigFitOnSamples(NodeTestCase):
    def setUp(self):
        super().setUp()
        path = samples_support.path
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(path(SAMPLE).replace("\\", "/"))
        self.body = self.network.createNode("a3ob_extract_lod")
        self.body.setInput(0, importer)
        self.body.parm("lod").set("Resolution 1")
        self.skeleton = self.network.createNode("a3ob_rig_skeleton")
        self.skeleton.parm("cfg").set(path(SAMPLE_CFG).replace("\\", "/"))
        self.skeleton.parm("pivots").set(path(SAMPLE_PIVOTS).replace("\\", "/"))
        for node in (self.body, self.skeleton):
            self.assertEqual(cook(node).errors, ())
        info = rig_weights.read_skeleton("test", self.skeleton.geometry())
        self.names, self.parents = info.names, info.parents
        self.rest = np.array(info.positions)
        self.index = {n.lower(): i for i, n in enumerate(self.names)}
        positions, tris = _mesh(self.body.geometry())
        tris = drop_small_pieces(len(positions), tris, 10)
        self.used = np.unique(tris)
        remap = -np.ones(len(positions), dtype=np.int64)
        remap[self.used] = np.arange(len(self.used))
        self.P = positions[self.used]
        self.tris = remap[tris]
        self.W = rig_weights.read_selections("test", self.body.geometry(), self.names).weights[self.used]
        # the sample's own landmarks that are not joints (see the node: the chin from the head, the crotch measured)
        self.chin_rest = self.rest[self.index["head"]] + np.array(CHIN_FROM_HEAD)
        joints = {n.lower(): self.rest[i] for i, n in enumerate(self.names)}
        self.groin_rest = make_template(joints, self.P).truth["lm_groin"]

    # ---- the posed copy -------------------------------------------------------------------------------------------

    def pose(self, arms=-20.0, legs=8.0, arm_scale=1.10, leg_scale=1.08, size=1.05):
        """``(4x4 matrices, world joint positions)`` of the spike's scenario C (or a part of it)."""
        local = {"LeftArm": rotation_z(arms), "RightArm": rotation_z(-arms), "LeftUpLeg": rotation_z(legs),
                 "RightUpLeg": rotation_z(-legs)}
        length = {n: arm_scale for n in ("LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand")}
        length.update({n: leg_scale for n in ("LeftLeg", "LeftFoot", "RightLeg", "RightFoot")})
        count = len(self.names)
        world_rot, world_pos, mats = [None] * count, np.zeros((count, 3)), np.zeros((count, 4, 4))
        for i, name in enumerate(self.names):
            parent = self.parents[i]
            rot_local = local.get(name, np.eye(3))
            if parent < 0:
                world_rot[i], world_pos[i] = size * rot_local, self.rest[i] * size
            else:
                offset = (self.rest[i] - self.rest[parent]) * length.get(name, 1.0)
                world_rot[i] = world_rot[parent] @ rot_local
                world_pos[i] = world_pos[parent] + world_rot[parent] @ offset
            mats[i, :3, :3] = world_rot[i]
            mats[i, :3, 3] = world_pos[i] - world_rot[i] @ self.rest[i]
            mats[i, 3, 3] = 1.0
        return mats, world_pos

    def posed_points(self, mats):
        blend = np.einsum("nb,bij->nij", self.W / np.maximum(self.W.sum(1, keepdims=True), 1e-9), mats)
        return np.einsum("nij,nj->ni", blend[:, :3, :3], self.P) + blend[:, :3, 3]

    def true_landmarks(self, mats, world_pos):
        out = {}
        for name in LANDMARK_NAMES:
            bone = BONE_OF[name]
            if bone:
                out[name] = world_pos[self.index[bone.lower()]]
        head = mats[self.index["head"]]
        out["lm_chin"] = head[:3, :3] @ self.chin_rest + head[:3, 3]
        pelvis = mats[self.index["pelvis"]]  # the crotch is part of the pelvis, whichever way the legs are turned
        out["lm_groin"] = pelvis[:3, :3] @ self.groin_rest + pelvis[:3, 3]
        return out

    def user_mesh(self, points, landmarks=None):
        """Polygons at ``points`` and, for each landmark, one extra point in the point group of its name."""
        geo = hou.Geometry()
        created = geo.createPoints([tuple(float(v) for v in p) for p in points])
        for tri in self.tris:
            prim = geo.createPolygon()
            for index in tri:
                prim.addVertex(created[int(index)])
        for name, position in (landmarks or {}).items():
            point = geo.createPoint()
            point.setPosition(tuple(float(v) for v in position))
            geo.createPointGroup(name).add(point)
        return make_source(self.network, geo)

    def fit(self, mesh, output="skeleton", **parms):
        node = self.network.createNode("a3ob_rig_fit")
        node.setInput(0, mesh)
        node.setInput(1, self.skeleton)
        node.setInput(2, self.body)
        node.parm("output").set(output)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def scenario(self):
        mats, world_pos = self.pose()
        truth = self.true_landmarks(mats, world_pos)
        posed = self.posed_points(mats)
        return mats, world_pos, truth, posed, self.user_mesh(posed, truth)

    # ---- tests ----------------------------------------------------------------------------------------------------

    def test_design_2_3_the_joints_land_within_1_5_cm_of_the_true_joints(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "skeleton", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo = node.geometry()
        self.assertEqual(geo.pointStringAttribValues("name"), tuple(self.names))
        fitted = np.array(geo.pointFloatAttribValues("P")).reshape(-1, 3)
        error = np.linalg.norm(fitted - world_pos, axis=1)
        worst = int(error.argmax())
        print("   design test 2.3 fit: joints %.2f cm mean, %.2f cm at most (%s)" % (
            100 * error.mean(), 100 * error.max(), self.names[worst]))
        self.assertLess(float(error.max()), 0.015, self.names[worst])
        # the polylines of the skeleton stay, and the Arma skeleton itself is not touched
        self.assertEqual(len(geo.prims()), len(self.skeleton.geometry().prims()))
        np.testing.assert_allclose(np.array(self.skeleton.geometry().pointFloatAttribValues("P")).reshape(-1, 3), self.rest,
                                   atol=1e-6)

    def test_the_fitted_transform_is_rotation_times_size_in_row_form(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "skeleton", snap=0)
        self.assertEqual(cook(node).errors, ())
        matrices = np.array(node.geometry().pointFloatAttribValues("transform")).reshape(-1, 3, 3)
        for number in (0, self.index["leftarm"], self.index["rightforearm"], self.index["leftleg"]):
            linear = matrices[number].T  # the node writes the row-vector form
            size = float(np.cbrt(np.linalg.det(linear)))
            self.assertGreater(size, 0.8)
            np.testing.assert_allclose(linear @ linear.T, size * size * np.eye(3), atol=1e-4)
        # the left arm is turned down by about the 20 degrees of the pose (a rotation about Z that takes +X towards -Y)
        left = matrices[self.index["leftarm"]].T
        self.assertLess(float((left @ np.array([1.0, 0.0, 0.0]))[1]), -0.2)

    def test_the_sample_body_is_posed_and_keeps_its_weights(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "body", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo, source = node.geometry(), self.body.geometry()
        self.assertEqual(sorted(g.name() for g in geo.pointGroups()), sorted(g.name() for g in source.pointGroups()))
        self.assertEqual(sorted(a.name() for a in geo.pointAttribs() if a.name().startswith("a3ob_w_")),
                         sorted(a.name() for a in source.pointAttribs() if a.name().startswith("a3ob_w_")))
        got = np.array(geo.pointFloatAttribValues("P")).reshape(-1, 3)[self.used]
        error = np.linalg.norm(got - posed, axis=1)
        print("   design test 2.3 body: %.2f cm mean, %.2f cm at most" % (100 * error.mean(), 100 * error.max()))
        self.assertLess(float(error.mean()), 0.01)
        self.assertLess(float(error.max()), 0.025)
        # the weights are the sample's own
        names = self.names
        before = rig_weights.read_selections("test", source, names).weights
        after = rig_weights.read_selections("test", geo, names).weights
        np.testing.assert_allclose(after, before, atol=1e-6)

    def test_design_2_4_the_fit_quality_is_a_message_and_a_warning_above_1_cm(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        good = self.fit(mesh, "skeleton", snap=0)
        result = cook(good)
        text = "\n".join(result.messages)
        self.assertIn("Fit quality:", text)
        self.assertEqual(result.warnings, ())
        # one size for the whole body cannot follow longer arms and legs: it is shown as a warning
        body = self.fit(mesh, "skeleton", snap=0, scale="body")
        result = cook(body)
        self.assertEqual(result.errors, ())
        self.assertIn("more than 1 cm", " ".join(result.warnings))
        self.assertIn("Fit quality:", "\n".join(result.messages))

    def test_the_landmark_output_has_a_named_point_for_each_landmark(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "landmarks", snap=0)
        self.assertEqual(cook(node).errors, ())
        geo = node.geometry()
        names = list(geo.pointStringAttribValues("name"))
        self.assertEqual(names, list(LANDMARK_NAMES))
        for name, point in zip(names, geo.points()):
            self.assertLess(float(np.linalg.norm(np.array(point.position()) - truth[name])), 0.002, name)

    def test_landmark_modes(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        some = {name: truth[name] for name in ("lm_knee_l", "lm_knee_r")}
        partial = self.user_mesh(posed, some)
        # From groups needs all of them
        node = self.fit(partial, "landmarks", landmarks="groups")
        self.assertIn("lm_chin", " ".join(cook(node).errors))
        # Both: groups win, the others are guessed
        node = self.fit(partial, "landmarks", landmarks="both", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("2 from groups, 12 guessed", "\n".join(result.messages))
        point = {n: np.array(p.position()) for n, p in zip(node.geometry().pointStringAttribValues("name"), node.geometry().points())}
        self.assertLess(float(np.linalg.norm(point["lm_knee_l"] - truth["lm_knee_l"])), 0.002)
        # Auto-guess ignores the groups
        node = self.fit(partial, "landmarks", landmarks="guess", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("ignored", "\n".join(result.messages))
        point = {n: np.array(p.position()) for n, p in zip(node.geometry().pointStringAttribValues("name"), node.geometry().points())}
        self.assertGreater(float(np.linalg.norm(point["lm_knee_l"] - truth["lm_knee_l"])), 0.01)

    def test_the_auto_guess_finds_the_joints_of_the_sample_itself_and_of_a_bigger_copy(self):
        for size, limit in ((1.0, 0.02), (1.10, 0.05)):
            mats, world_pos = self.pose(arms=0.0, legs=0.0, arm_scale=1.0, leg_scale=1.0, size=size)
            posed = self.posed_points(mats)
            node = self.fit(self.user_mesh(posed), "landmarks", snap=0)
            self.assertEqual(cook(node).errors, ())
            geo = node.geometry()
            found = {n: np.array(p.position()) for n, p in zip(geo.pointStringAttribValues("name"), geo.points())}
            truth = self.true_landmarks(mats, world_pos)
            worst = max((float(np.linalg.norm(found[n] - truth[n])), n) for n in LANDMARK_NAMES
                        if n not in ("lm_chin", "lm_groin"))
            print("   auto-guess, size %.2f: worst joint %s %.2f cm" % (size, worst[1], 100 * worst[0]))
            self.assertLess(worst[0], limit, worst[1])

    def test_a_mesh_that_faces_the_other_way_is_warned_about(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        turned = posed * np.array([-1.0, 1.0, -1.0])
        node = self.fit(self.user_mesh(turned), "skeleton", landmarks="guess")
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("the other way", " ".join(result.warnings))

    def test_refine_is_not_available_yet_and_says_so(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "skeleton", snap=0, refine=1)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("Refine is not available", " ".join(result.warnings))

    def test_the_fitted_body_goes_straight_into_weight_transfer(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        body = self.fit(mesh, "body", snap=0)
        transfer = self.network.createNode("a3ob_weight_transfer")
        transfer.setInput(0, mesh)
        transfer.setInput(1, body)
        transfer.setInput(2, self.skeleton)
        result = cook(transfer)
        self.assertEqual(result.errors, ())
        got = rig_weights.read_selections("test", transfer.geometry(), self.names).weights
        # the points of the user's mesh come first (the landmark points have no polygons and are not in the groups)
        weights = got[:len(self.P)]
        l1 = np.abs(weights - self.W).sum(axis=1)
        print("   fitted sample body -> Weight Transfer: L1 mean %.3f against the sample's own weights" % l1.mean())
        self.assertLess(float(l1.mean()), 0.25)  # the unfitted-by-up-to-1.7-cm body costs more than the exact copy (0.064)


if __name__ == "__main__":
    unittest.main()

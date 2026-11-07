"""The Unpose node in hython (design tests 2.3, unpose part, and 2.5, task #67). It needs ``hou`` with KineFX (do not run it
with HOUDINI_PACKAGE_SKIP=1) and the Arma 3 Samples (it skips without them).

The user's mesh is the sample character posed as in design test 2.3 (the spike's scenario C: arms 20 degrees down, legs 8
degrees apart, arms 10 % and legs 8 % longer; since task #80 the body is not bigger, because Unpose no longer undoes an overall size).
The longer arms and legs are longer all the way:
every bone below the shoulder or the hip (twist bones and fingers too) is longer, and the mesh around a bone is longer
along the bone only, as for a person with long arms. The true rest is the sample.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from tests import samples_support
from tests import test_node_rig_fit as rig_fit_tests
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

if HAS_HOU:
    from a3ob_core.rig import CHIN_FROM_HEAD, aim_targets, bone_frames, ORIGIN_RIGID, drop_small_pieces, make_template
    from a3ob_houdini import rig_weights
    from a3ob_houdini.rig_weights import read_mesh as _mesh


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestUnposeOnSamples(NodeTestCase):
    def setUp(self):
        super().setUp()
        path = samples_support.path
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(path(rig_fit_tests.SAMPLE).replace("\\", "/"))
        self.body = self.network.createNode("a3ob_extract_lod")
        self.body.setInput(0, importer)
        self.body.parm("lod").set("Resolution 1")
        self.skeleton = self.network.createNode("a3ob_rig_skeleton")
        self.skeleton.parm("cfg").set(path(rig_fit_tests.SAMPLE_CFG).replace("\\", "/"))
        self.skeleton.parm("pivots").set(path(rig_fit_tests.SAMPLE_PIVOTS).replace("\\", "/"))
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
        # the sample's landmarks that are not joints, as in the Rig Fit tests
        self.chin_rest = self.rest[self.index["head"]] + np.array(CHIN_FROM_HEAD)
        joints = {n.lower(): self.rest[i] for i, n in enumerate(self.names)}
        self.groin_rest = make_template(joints, self.P).truth["lm_groin"]
        geo = self.body.geometry()
        self.full_P, self.full_tris = _mesh(geo)
        self.full_W = rig_weights.read_selections("test", geo, self.names).weights
        self.weighted = self.full_W.sum(axis=1) > 0
        self.meshed = np.zeros(len(self.full_P), dtype=bool)  # the points of polygons (not those of proxy markers)
        self.meshed[np.unique(self.full_tris)] = True

    # helpers of the Rig Fit tests (plain functions, they use only the attributes that setUp makes)
    posed_points = rig_fit_tests.TestRigFitOnSamples.posed_points
    true_landmarks = rig_fit_tests.TestRigFitOnSamples.true_landmarks
    user_mesh = rig_fit_tests.TestRigFitOnSamples.user_mesh
    fit = rig_fit_tests.TestRigFitOnSamples.fit
    part = rig_fit_tests.TestRigFitOnSamples.part

    def stretched_pose(self, arms=-20.0, legs=8.0, arm_scale=1.10, leg_scale=1.08, size=1.0, bend=0.0, hands=0.0):
        """``(4x4 matrices, world joint positions)``: design test 2.3 with limbs that are longer all the way down. A bone
        that is longer makes the mesh around it longer along the bone only (a longer arm is not a thicker one); the whole
        body is ``size`` bigger. A chain end (a hand, a foot) has no bone and takes the body size."""
        rotation_z = rig_fit_tests.rotation_z
        local = {"LeftArm": rotation_z(arms), "RightArm": rotation_z(-arms), "LeftUpLeg": rotation_z(legs),
                 "RightUpLeg": rotation_z(-legs), "LeftForeArm": rotation_z(bend), "LeftHand": rotation_z(hands),
                 "RightHand": rotation_z(-hands)}
        count = len(self.names)
        grow = np.ones(count)
        for i in range(count):
            joint = i
            while joint >= 0:
                name = self.names[joint].lower()
                if name in ("leftarm", "rightarm"):
                    grow[i] = arm_scale
                    break
                if name in ("leftupleg", "rightupleg"):
                    grow[i] = leg_scale
                    break
                joint = self.parents[joint]
        axes = bone_frames(self.parents, self.rest)[:, :, 0]
        aimed = np.array(aim_targets(self.parents, self.rest)) >= 0
        world_rot, stretch = [None] * count, [None] * count
        world_pos, mats = np.zeros((count, 3)), np.zeros((count, 4, 4))
        for i in range(count):
            parent = self.parents[i]
            along = size * (grow[i] if aimed[i] else 1.0)
            stretch[i] = size * np.eye(3) + (along - size) * np.outer(axes[i], axes[i])
            rot_local = local.get(self.names[i], np.eye(3))
            if parent < 0:
                world_rot[i], world_pos[i] = rot_local, self.rest[i] * size
            else:
                world_rot[i] = world_rot[parent] @ rot_local
                world_pos[i] = world_pos[parent] + world_rot[parent] @ stretch[parent] @ (self.rest[i] - self.rest[parent])
            linear = world_rot[i] @ stretch[i]
            mats[i, :3, :3] = linear
            mats[i, :3, 3] = world_pos[i] - linear @ self.rest[i]
            mats[i, 3, 3] = 1.0
        self.rotations = world_rot
        return mats, world_pos

    def skin_all(self, mats):
        """Every point of the sample body (also those outside ``self.used``) posed with its own weights."""
        w = self.full_W / np.maximum(self.full_W.sum(axis=1, keepdims=True), 1e-9)
        blend = np.einsum("nb,bij->nij", w, mats)
        return np.einsum("nij,nj->ni", blend[:, :3, :3], self.full_P) + blend[:, :3, 3]

    def sample_with_points(self, points, attribs=None):
        """The sample body (weights, groups) with its points moved to ``points``; optional point int attributes."""
        geo = hou.Geometry()
        geo.copy(self.body.geometry())
        geo.setPointFloatAttribValues("P", [float(v) for v in np.asarray(points).reshape(-1)])
        for name, values in (attribs or {}).items():
            geo.addAttrib(hou.attribType.Point, name, 0)
            geo.setPointIntAttribValues(name, [int(v) for v in values])
        return make_source(self.network, geo)

    def skeleton_source(self, positions):
        """A Rig Fit-shaped geometry (the fitted skeleton as a named part) with the joints at ``positions``."""
        skeleton = hou.Geometry()
        skeleton.copy(self.skeleton.geometry())
        skeleton.setPointFloatAttribValues("P", [float(v) for v in np.asarray(positions).reshape(-1)])
        out = hou.Geometry()
        rig_weights.write_parts(out, {rig_weights.PART_SKELETON: skeleton})
        return make_source(self.network, out)

    def unpose(self, mesh, fitted, method="bones", **parms):
        """The Bones method unless a test asks for the cage (task #80); the older tests are about the bone matrices."""
        node = self.network.createNode("a3ob_unpose")
        node.parm("method").set(method)
        node.setInput(0, mesh)
        node.setInput(1, fitted)
        node.setInput(2, self.skeleton)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def points_of(self, node):
        return np.array(node.geometry().pointFloatAttribValues("P")).reshape(-1, 3)

    # ---- tests ----------------------------------------------------------------------------------------------------

    def test_design_2_5_true_weights_and_a_perfect_skeleton_are_taken_back_below_0_01_cm(self):
        mats, world_pos = self.stretched_pose()
        mesh = self.sample_with_points(self.skin_all(mats))
        node = self.unpose(mesh, self.skeleton_source(world_pos))
        result = cook(node)
        self.assertEqual(result.errors, ())
        error = np.linalg.norm(self.points_of(node) - self.full_P, axis=1)[self.weighted]
        print("   design test 2.5: error %.5f cm mean, %.5f cm at most; %s" % (100 * error.mean(), 100 * error.max(),
                                                                         result.messages[0]))
        self.assertLess(float(error.max()), 1e-4)  # 0.01 cm

    def test_design_2_5_rigid_pieces_keep_their_edge_lengths_to_0_1_percent(self):
        mats, world_pos = self.stretched_pose(bend=45.0)
        near = np.linalg.norm(self.full_P - self.rest[self.index["leftforearm"]], axis=1) < 0.15
        cuff = np.nonzero(near & self.weighted)[0]
        self.assertGreater(len(cuff), 50)
        posed = self.skin_all(mats)
        forearm = self.index["leftforearm"]  # the cuff is rigid on the forearm in the truth: turned and moved, not stretched
        turn = self.rotations[forearm]
        posed[cuff] = (self.full_P[cuff] - self.rest[forearm]) @ turn.T + world_pos[forearm]
        flag = np.zeros(len(posed), dtype=np.int64)
        flag[cuff] = 1
        mesh = self.sample_with_points(posed, {"a3ob_rig_piece": np.where(flag == 1, 7, -1),
                                               "a3ob_rig_origin": np.where(flag == 1, ORIGIN_RIGID, 1)})
        ends = np.concatenate([self.full_tris[:, [0, 1]], self.full_tris[:, [1, 2]], self.full_tris[:, [2, 0]]])
        ends = ends[flag[ends[:, 0]].astype(bool) & flag[ends[:, 1]].astype(bool)]
        self.assertGreater(len(ends), 100)

        def ratios(node):
            self.assertEqual(cook(node).errors, ())
            out = self.points_of(node)
            return np.linalg.norm(out[ends[:, 0]] - out[ends[:, 1]], axis=1) / np.linalg.norm(
                self.full_P[ends[:, 0]] - self.full_P[ends[:, 1]], axis=1)

        fitted = self.skeleton_source(world_pos)
        kept = ratios(self.unpose(mesh, fitted, rigid_pieces=1))
        loose = ratios(self.unpose(mesh, fitted, rigid_pieces=0))
        print("   rigid cuff: edge length ratio %.5f to %.5f with rigid pieces, %.3f to %.3f without" % (
            kept.min(), kept.max(), loose.min(), loose.max()))
        self.assertLess(float(np.abs(kept - 1).max()), 1e-3)
        self.assertGreater(float(np.abs(loose - 1).max()), 1e-2)

    def through_rig_fit_and_unpose(self, mats, world_pos):
        """Pose, Rig Fit, Weight Transfer, Weight Cleanup, Unpose; returns (cook result, error per point, posed points)."""
        truth = self.true_landmarks(mats, world_pos)
        posed = self.posed_points(mats)
        mesh = self.user_mesh(posed, truth)
        fit = self.fit(mesh, "all", snap=0)
        transfer = self.network.createNode("a3ob_weight_transfer")
        transfer.setInput(0, mesh)
        transfer.setInput(1, fit)
        transfer.setInput(2, self.skeleton)
        cleanup = self.network.createNode("a3ob_weight_cleanup")
        cleanup.setInput(0, transfer)
        cleanup.setInput(1, self.skeleton)
        node = self.unpose(cleanup, fit)
        result = cook(node)
        error = np.linalg.norm(self.points_of(node)[:len(self.P)] - self.P, axis=1)
        return result, error, posed

    def test_design_2_3_d43_consistent_scenario_through_rig_fit_and_unpose_ends_within_1_cm(self):
        """D43 pass/fail test: scenario C's pose, bones below the shoulder and hip lengthened, mesh stretched along them."""
        mats, world_pos = self.stretched_pose()
        result, error, posed = self.through_rig_fit_and_unpose(mats, world_pos)
        self.assertEqual(result.errors, ())
        print("   design test 2.3 unpose (D43 consistent scenario): %.2f cm mean, %.2f cm at most (the pose moved the points %.1f cm on average)" % (
            100 * error.mean(), 100 * error.max(), 100 * np.linalg.norm(posed - self.P, axis=1).mean()))
        self.assertLess(float(error.max()), 0.01)
        self.assertLess(float(error.mean()), 0.003)

    def test_task_70_the_finger_points_of_hands_turned_30_degrees_come_back_within_1_cm_of_the_arma_rest(self):
        """Task #70, criterion 3: the hands are fitted (finger landmarks), so Unpose puts the finger points back."""
        mats, world_pos = self.stretched_pose(hands=30.0)
        result, error, posed = self.through_rig_fit_and_unpose(mats, world_pos)
        self.assertEqual(result.errors, ())
        owner = self.full_W[self.used].argmax(axis=1)
        fingers = np.array([self.names[b].startswith(("LeftHand", "RightHand")) for b in owner])
        self.assertGreater(int(fingers.sum()), 100)
        print("   task 70 unpose, hands turned 30 degrees: finger points %.2f cm mean, %.2f cm at most (all points %.2f cm at most)" % (
            100 * error[fingers].mean(), 100 * error[fingers].max(), 100 * error.max()))
        self.assertLess(float(error[fingers].max()), 0.01)

    def test_design_2_3_d43_literal_scenario_c_known_limit_regression_guard(self):
        """Known limit (D43): literal scenario C, bones lengthened but the mesh NOT stretched.

        The unpose cannot tell a longer bone from a longer body, so the result is worse than the consistent
        scenario (measured 0.65 cm mean, 1.53 cm max). This only guards against getting clearly worse.
        """
        mats, world_pos = rig_fit_tests.TestRigFitOnSamples.pose(self)
        result, error, posed = self.through_rig_fit_and_unpose(mats, world_pos)
        self.assertEqual(result.errors, ())
        print("   design test 2.3 unpose (D43 literal scenario C, known limit): %.2f cm mean, %.2f cm at most (the pose moved the points %.1f cm on average)" % (
            100 * error.mean(), 100 * error.max(), 100 * np.linalg.norm(posed - self.P, axis=1).mean()))
        self.assertLess(float(error.max()), 0.02)
        self.assertLess(float(error.mean()), 0.008)

    def test_a_perfect_fit_moves_nothing_and_the_weights_stay(self):
        node = self.unpose(self.body, self.skeleton)  # A' = A: the Arma skeleton itself
        result = cook(node)
        self.assertEqual(result.errors, ())
        np.testing.assert_allclose(self.points_of(node), self.full_P, atol=1e-6)
        self.assertIn("0.00 cm", result.messages[0])
        before = rig_weights.read_selections("test", self.body.geometry(), self.names).weights
        after = rig_weights.read_selections("test", node.geometry(), self.names).weights
        np.testing.assert_array_equal(before, after)

    def test_the_message_gives_the_mean_the_largest_move_and_the_worst_bones(self):
        mats, world_pos = self.stretched_pose()
        node = self.unpose(self.sample_with_points(self.skin_all(mats)), self.skeleton_source(world_pos))
        result = cook(node)
        text = "\n".join(result.messages)
        self.assertRegex(text, r"moved back to the Arma rest pose: \d+\.\d\d cm on average, \d+\.\d\d cm at most")
        self.assertIn("Bones whose points moved most", text)

    # ---- the cage method (task #80, D44) ----------------------------------------------------------------------------

    def cage_source(self, fitted_points):
        """A Rig Fit-shaped geometry: the Arma skeleton, the fitted body with its points at ``fitted_points`` and the rest
        body (the sample as it is). Both bodies keep the proxy markers, as Rig Fit's do."""
        skeleton = hou.Geometry()
        skeleton.copy(self.skeleton.geometry())
        fitted = hou.Geometry()
        fitted.copy(self.body.geometry())
        fitted.setPointFloatAttribValues("P", [float(v) for v in np.asarray(fitted_points).reshape(-1)])
        rest = hou.Geometry()
        rest.copy(self.body.geometry())
        out = hou.Geometry()
        rig_weights.write_parts(out, {rig_weights.PART_SKELETON: skeleton, rig_weights.PART_BODY: fitted,
                                      rig_weights.PART_REST_BODY: rest})
        return make_source(self.network, out)

    def edge_ratios(self, node, mask, before):
        """Length ratio (after over ``before``, the points that went in) of the edges of the sample whose two points are
        in ``mask``."""
        ends = np.concatenate([self.full_tris[:, [0, 1]], self.full_tris[:, [1, 2]], self.full_tris[:, [2, 0]]])
        ends = ends[mask[ends[:, 0]] & mask[ends[:, 1]]]
        out = self.points_of(node)
        d0 = np.linalg.norm(before[ends[:, 0]] - before[ends[:, 1]], axis=1)
        keep = d0 > 1e-9
        return (np.linalg.norm(out[ends[:, 0]] - out[ends[:, 1]], axis=1)[keep] / d0[keep]), ends

    def test_task_80_the_cage_takes_a_body_of_other_proportions_onto_the_sample_with_no_size_change(self):
        """Made-up case with a known rest: the user's body is the sample 10 % bigger (the wrapped body is that too), so the
        cage must give the sample back, and a body of the same size, turned, must keep every edge length."""
        n = len(self.full_P)
        bigger = self.full_P * 1.1
        node = self.unpose(self.sample_with_points(bigger), self.cage_source(bigger), method="cage", bodygroup="")
        result = cook(node)
        self.assertEqual(result.errors, ())
        error = np.linalg.norm(self.points_of(node) - self.full_P, axis=1)[self.meshed]
        print("   task 80 cage, a body 10 %% bigger: %.3f cm mean, %.3f cm at most; %s" % (
            100 * error.mean(), 100 * error.max(), result.messages[0]))
        self.assertLess(float(np.percentile(error, 99)), 0.01)
        turn = rig_fit_tests.rotation_z(25.0)
        turned = self.full_P @ turn.T + np.array([0.4, 0.0, -0.2])
        same = self.unpose(self.sample_with_points(turned), self.cage_source(turned), method="cage", bodygroup="")
        self.assertEqual(cook(same).errors, ())
        np.testing.assert_allclose(self.points_of(same)[self.meshed], self.full_P[self.meshed], atol=1e-5)  # a pure move: back exactly
        ratios, _ = self.edge_ratios(same, np.ones(n, dtype=bool), turned)
        self.assertLess(float(np.abs(ratios - 1).max()), 1e-4)  # (points are float32)

    def test_task_80_a_rigid_piece_keeps_its_edges_while_the_rest_follows_the_cage(self):
        """The cage stretches the body 20 % along Y; the cuff (a rigid piece) moves with it but keeps every edge length."""
        near = np.linalg.norm(self.full_P - self.rest[self.index["leftforearm"]], axis=1) < 0.15
        cuff = near & self.weighted
        self.assertGreater(int(cuff.sum()), 50)
        stretched = self.full_P * np.array([1.0, 1.2, 1.0])
        mesh = self.sample_with_points(stretched, {"a3ob_rig_piece": np.where(cuff, 7, -1),
                                                   "a3ob_rig_origin": np.where(cuff, ORIGIN_RIGID, 1)})
        node = self.unpose(mesh, self.cage_source(stretched), method="cage", bodygroup="")
        result = cook(node)
        self.assertEqual(result.errors, ())
        ratios, _ = self.edge_ratios(node, cuff, stretched)
        print("   task 80 rigid cuff under the cage: edge ratio %.8f to %.8f; %s" % (ratios.min(), ratios.max(), result.messages[-1]))
        self.assertGreater(len(ratios), 100)
        self.assertLess(float(np.abs(ratios - 1).max()), 1e-4)  # (points are float32)
        self.assertIn("1 rigid piece moved with one rigid transform", " ".join(result.messages))
        loose = self.unpose(mesh, self.cage_source(stretched), method="cage", bodygroup="", rigid_pieces=0)
        self.assertEqual(cook(loose).errors, ())
        ratios, _ = self.edge_ratios(loose, cuff, stretched)
        self.assertGreater(float(np.abs(ratios - 1).max()), 1e-4)

    # ---- fingers and head (task #83) ---------------------------------------------------------------------------------

    def run_pipeline(self, mesh, method="cage", **fit_parms):
        """Rig Fit, Weight Transfer, Weight Cleanup and Unpose on ``mesh``; returns ``(unpose node, Rig Fit node)``."""
        fit = self.fit(mesh, "all", snap=0, **fit_parms)
        transfer = self.network.createNode("a3ob_weight_transfer")
        transfer.setInput(0, mesh)
        transfer.setInput(1, fit)
        transfer.setInput(2, self.skeleton)
        cleanup = self.network.createNode("a3ob_weight_cleanup")
        cleanup.setInput(0, transfer)
        cleanup.setInput(1, self.skeleton)
        node = self.unpose(cleanup, fit, method=method)
        self.assertEqual(cook(node).errors, ())
        return node, fit

    def test_task_83_curled_fingers_are_fitted_and_come_back_straight_through_rig_fit_and_unpose(self):
        """Made-up case with a known answer: the fingers of the sample are curled 30, 40 and 20 degrees at their three joints.
        With Bend Fingers the fitted finger joints follow the curl and Unpose gives straight fingers (the sample's own); with
        it off the straight chains cannot follow the curl and the fingers come back worse."""
        mats, world_pos = rig_fit_tests.TestRigFitOnSamples.pose(self, curl=(30.0, 40.0, 20.0))
        truth = self.true_landmarks(mats, world_pos)
        mesh = self.user_mesh(self.posed_points(mats), truth)
        owner = self.full_W[self.used].argmax(axis=1)
        fingers = np.array([self.names[b].startswith(("LeftHand", "RightHand")) and "Thumb" not in self.names[b] and
                            self.names[b] not in ("LeftHand", "RightHand") for b in owner])
        self.assertGreater(int(fingers.sum()), 100)
        hand_joints = [i for i, n in enumerate(self.names) if n.startswith(("LeftHand", "RightHand")) and n[-1] in "23" and
                       "Thumb" not in n]
        worst = {}
        for bend in (0, 1):
            node, fit = self.run_pipeline(mesh, "cage", refine=1, bendfingers=bend)
            error = np.linalg.norm(self.points_of(node)[:len(self.P)] - self.P, axis=1)
            joints = np.array(self.part(fit, "fitted_skeleton").pointFloatAttribValues("P")).reshape(-1, 3)
            joint_error = np.linalg.norm(joints[hand_joints] - world_pos[hand_joints], axis=1)
            worst[bend] = (float(error[fingers].mean()), float(joint_error.mean()))
            print("   task 83 curled fingers, Bend Fingers %d: fitted finger joints %.2f cm off, finger points back %.2f cm off the "
                  "Arma rest on average (%.2f cm at most)" % (bend, 100 * worst[bend][1], 100 * worst[bend][0], 100 * error[fingers].max()))
        self.assertLess(worst[1][1], 0.5 * worst[0][1])  # the fitted joints are far nearer the true ones
        self.assertLess(worst[1][1], 0.015)
        self.assertLess(worst[1][0], 0.7 * worst[0][0])  # and the unposed fingers are nearer the sample's
        self.assertLess(worst[1][0], 0.015)

    def head_case(self, pitch):
        """The sample body with its top slice (the neck stub and above) as the ``head`` bone's points, the part above the chest
        bent forward by ``pitch`` degrees about the X axis, as the wrapped body of a character whose neck leans: the cage then
        turns that slice back. Returns ``(mesh, rest body points, head mask, wrapped points)``."""
        top = self.full_P[self.meshed, 1].max()
        head = self.meshed & (self.full_P[:, 1] > top - 0.07)
        bend = self.meshed & (self.full_P[:, 1] > top - 0.20)
        pivot = np.array([0.0, top - 0.20, float(self.full_P[bend, 2].mean())])
        bent = self.full_P.copy()
        bent[bend] = (self.full_P[bend] - pivot) @ rig_fit_tests.rotation_about([1, 0, 0], pitch).T + pivot
        geo = hou.Geometry()
        geo.copy(self.body.geometry())
        geo.setPointFloatAttribValues("P", [float(v) for v in bent.reshape(-1)])
        for group in geo.pointGroups():  # the head slice leaves its bones and follows the head bone only
            for point in [p for p in geo.points() if head[p.number()] and group.contains(p)]:
                if group.name().lower() in [n.lower() for n in self.names]:
                    group.remove(point)
        group = next((g for g in geo.pointGroups() if g.name().lower() == "head"), None) or geo.createPointGroup("head")
        group.add([p for p in geo.points() if head[p.number()]])
        return make_source(self.network, geo), bent, head

    def test_task_83_the_head_keeps_its_tilt_through_the_cage_unpose(self):
        """Made-up head tilted 12 degrees against the neck: Unpose gives it no turn (Kabsch of its points before and after), the
        way the cage alone does turn it, and the base of the head stays seated where the cage puts the neck."""
        from a3ob_core.digest.pose import bone_turn, turn_angles
        pitch = 12.0
        mesh, bent, head = self.head_case(pitch)
        results = {}
        for keep in (0, 1):
            node = self.unpose(mesh, self.cage_source(bent), method="cage", bodygroup="", keephead=keep)
            result = cook(node)
            self.assertEqual(result.errors, ())
            out = self.points_of(node)
            rotation, rms = bone_turn(bent[head], out[head])
            results[keep] = (turn_angles(rotation), rms)
            print("   task 83 head tilted %.0f degrees, Head Keeps Its Tilt %d: head turned pitch %.2f yaw %.2f roll %.2f degrees, "
                  "rms %.3f cm; %s" % (pitch, keep, *results[keep][0], 100 * rms, result.messages[0]))
            if keep:
                self.assertIn("points keep their tilt", " ".join(result.messages))
        self.assertGreater(abs(results[0][0][0]), 5.0)  # the cage does turn it
        self.assertLess(abs(results[1][0][0]), 0.5)  # and now it is not turned (the criterion asks for 2 degrees)
        self.assertLess(abs(results[1][0][1]) + abs(results[1][0][2]), 0.5)
        self.assertLess(results[1][1], 1e-4)  # exactly rigid: a pure shift

    def test_task_80_a_rig_fit_output_without_the_rest_body_is_a_clear_error(self):
        mesh = self.sample_with_points(self.full_P)
        node = self.unpose(mesh, self.skeleton_source(self.rest), method="cage", bodygroup="")
        self.assertIn("has no part fitted_body", " ".join(cook(node).errors))


    def test_the_group_limits_the_points_and_a_mismatching_skeleton_is_an_error(self):
        mats, world_pos = self.stretched_pose()
        posed = self.skin_all(mats)
        mesh = self.sample_with_points(posed)
        node = self.unpose(mesh, self.skeleton_source(world_pos), group="0-99")
        self.assertEqual(cook(node).errors, ())
        out = self.points_of(node)
        np.testing.assert_allclose(out[100:], posed[100:], atol=1e-5)
        self.assertLess(float(np.abs(out[:100] - self.full_P[:100])[self.weighted[:100]].max()), 1e-4)
        other = hou.Geometry()
        other.copy(self.skeleton.geometry())
        other.deletePoints([other.points()[-1]])
        bad = self.unpose(mesh, make_source(self.network, other))
        self.assertIn("same bones", " ".join(cook(bad).errors))


if __name__ == "__main__":
    unittest.main()

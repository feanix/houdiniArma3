"""The Rig Fit node in hython (design tests 2.3, fit part, and 2.4, task #66). It needs ``hou`` with KineFX (do not run it
with HOUDINI_PACKAGE_SKIP=1) and the Arma 3 Samples (it skips without them).

The user's mesh is the sample character itself, posed and sized as in design test 2.3 (the spike's scenario C: arms 20
degrees down, legs 8 degrees apart, arms 10 % and legs 8 % longer, the whole body 5 % bigger), so the true joints are known.
The landmarks are detail attributes on that mesh (the form the Rig Landmarks node writes, task #74, D42).

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
    from a3ob_houdini import landmark_io, rig_weights
    from a3ob_houdini.rig_weights import read_mesh as _mesh


def rotation_z(degrees):
    a = np.radians(degrees)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


def rotation_about(axis, degrees):
    """The rotation by ``degrees`` about the direction ``axis`` (Rodrigues)."""
    a = np.radians(degrees)
    k = np.asarray(axis, dtype=np.float64) / np.linalg.norm(axis)
    cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(a) * cross + (1.0 - np.cos(a)) * (cross @ cross)


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

    def pose(self, arms=-20.0, legs=8.0, arm_scale=1.10, leg_scale=1.08, size=1.05, hand_swing=0.0, hand_twist=0.0,
             finger_scale=1.0, curl=None):
        """``(4x4 matrices, world joint positions)`` of the spike's scenario C (or a part of it). ``hand_swing`` turns each
        hand about the Z axis (in the plane of the A-pose), ``hand_twist`` about the axis of its forearm (the palm turns),
        both in degrees; ``finger_scale`` lengthens every bone from a hand down (task #70). ``curl`` is ``(knuckle, middle,
        end)`` in degrees: every finger (not the thumb) is bent by these at its three joints, towards the palm (task #83)."""
        local = {"LeftArm": rotation_z(arms), "RightArm": rotation_z(-arms), "LeftUpLeg": rotation_z(legs),
                 "RightUpLeg": rotation_z(-legs)}
        if curl:
            from a3ob_core.rig.hand import FINGERS, _palm_normal
            for side, sign in (("Left", 1.0), ("Right", -1.0)):
                palm = _palm_normal(self.rest, self.index, side.lower(), np.eye(3))
                for finger in FINGERS[:4]:
                    names = ["%sHand%s%d" % (side, finger.capitalize(), k) for k in (1, 2, 3)]
                    direction = self.rest[self.index[names[1].lower()]] - self.rest[self.index[names[0].lower()]]
                    axis = np.cross(direction, palm)
                    for name, degrees in zip(names, curl):
                        local[name] = rotation_about(axis, sign * degrees)
        for side, sign in (("Left", 1.0), ("Right", -1.0)):
            axis = self.rest[self.index[side.lower() + "hand"]] - self.rest[self.index[side.lower() + "forearm"]]
            local[side + "Hand"] = rotation_z(sign * hand_swing) @ rotation_about(axis, sign * hand_twist)  # mirrored, so Symmetry keeps both hands
        length = {n: arm_scale for n in ("LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand")}
        length.update({n: leg_scale for n in ("LeftLeg", "LeftFoot", "RightLeg", "RightFoot")})
        length.update({n: finger_scale for n in self.names if n.startswith(("LeftHand", "RightHand")) and
                       n not in ("LeftHand", "RightHand")})
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

    def user_mesh(self, points, landmarks=None, group="body"):
        """Polygons at ``points`` and, for each landmark, a detail vector ``a3ob_lm_*`` (what Rig Landmarks writes). All the
        points are in the point group ``group`` (Refine wraps onto it; empty: no group)."""
        geo = hou.Geometry()
        created = geo.createPoints([tuple(float(v) for v in p) for p in points])
        if group:
            geo.createPointGroup(group).add(created)
        for tri in self.tris:
            prim = geo.createPolygon()
            for index in tri:
                prim.addVertex(created[int(index)])
        landmark_io.write_landmarks(geo, landmarks or {})
        return make_source(self.network, geo)

    def part(self, node, name):
        """The geometry of one named part of the one output of a Rig Fit node."""
        return rig_weights.read_part("test", node.geometry(), name)

    def fit(self, mesh, show="all", **parms):
        node = self.network.createNode("a3ob_rig_fit")
        node.setInput(0, mesh)
        node.setInput(1, self.skeleton)
        node.setInput(2, self.body)
        node.parm("show").set(show)
        parms.setdefault("refine", 0)  # Refine is on by default; the tests of the plain fit keep it off
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
        geo = self.part(node, "fitted_skeleton")
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

    def test_design_task_70_hands_turned_30_degrees_and_fingers_10_percent_longer_land_within_1_cm(self):
        """Task #70: scenario C plus hands turned 30 degrees (in the plane of the arm, about the forearm, and both) and every
        finger bone 10 % longer. The hand and finger joints come out within 1 cm of the true ones."""
        hands = [i for i, n in enumerate(self.names) if n.startswith(("LeftHand", "RightHand")) and n not in ("LeftHand", "RightHand")]
        self.assertGreaterEqual(len(hands), 30)
        for swing, twist in ((30.0, 0.0), (0.0, 30.0), (30.0, 30.0)):
            mats, world_pos = self.pose(hand_swing=swing, hand_twist=twist, finger_scale=1.10)
            truth = self.true_landmarks(mats, world_pos)
            posed = self.posed_points(mats)
            node = self.fit(self.user_mesh(posed, truth), "skeleton", snap=0)
            self.assertEqual(cook(node).errors, ())
            fitted = np.array(self.part(node, "fitted_skeleton").pointFloatAttribValues("P")).reshape(-1, 3)
            error = np.linalg.norm(fitted - world_pos, axis=1)
            worst = max(hands, key=lambda i: error[i])
            print("   task 70, hands swung %d and turned %d degrees, fingers 10 %% longer: finger joints %.2f cm at most (%s), "
                  "the rest of the body %.2f cm" % (swing, twist, 100 * error[worst], self.names[worst],
                                                     100 * np.delete(error, hands).max()))
            self.assertLess(float(error[hands].max()), 0.01, self.names[worst])
            self.assertLess(float(np.delete(error, hands).max()), 0.015)
            # the frames stay pure rotations (task #76)
            matrices = np.array(self.part(node, "fitted_skeleton").pointFloatAttribValues("transform")).reshape(-1, 3, 3)
            for number in hands:
                np.testing.assert_allclose(matrices[number] @ matrices[number].T, np.eye(3), atol=1e-4)
                self.assertAlmostEqual(float(np.linalg.det(matrices[number])), 1.0, places=4)

    def test_task_70_a_hand_with_no_fingers_set_still_works_and_the_fingers_are_guessed_from_the_hand(self):
        """The finger and thumb landmarks are guessed when they are not set; the wrist that is set starts the guess."""
        mats, world_pos = self.pose(hand_swing=20.0)
        truth = self.true_landmarks(mats, world_pos)
        some = {n: truth[n] for n in LANDMARK_NAMES if not n.startswith(("lm_finger", "lm_thumb"))}
        node = self.fit(self.user_mesh(self.posed_points(mats), some), "landmarks", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("14 from Rig Landmarks, 4 guessed", "\n".join(result.messages))
        geo = self.part(node, "landmarks")
        found = {n: np.array(p.position()) for n, p in zip(geo.pointStringAttribValues("name"), geo.points())}
        for name in ("lm_finger_l", "lm_finger_r"):
            self.assertLess(float(np.linalg.norm(found[name] - truth[name])), 0.03, name)  # the guess of the fingertip
        for name in ("lm_thumb_l", "lm_thumb_r"):
            self.assertLess(float(np.linalg.norm(found[name] - truth[name])), 0.05, name)

    def test_zero_length_rest_bones_stay_zero_length_and_the_spine_does_not_zigzag(self):
        # task #75: Pelvis and Spine are in one place in the Arma rest; they must be after the fit too
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "skeleton", snap=0)
        self.assertEqual(cook(node).errors, ())
        fitted = np.array(self.part(node, "fitted_skeleton").pointFloatAttribValues("P")).reshape(-1, 3)
        zero = [i for i, p in enumerate(self.parents) if p >= 0 and np.linalg.norm(self.rest[i] - self.rest[p]) < 1e-4]
        self.assertIn(self.index["spine"], zero)
        for i in zero:
            self.assertLess(float(np.linalg.norm(fitted[i] - fitted[self.parents[i]])), 1e-3, self.names[i])
        chain = [fitted[self.index[n]][1] for n in ("pelvis", "spine", "spine1")]
        self.assertGreaterEqual(chain[2] - chain[1], 0.0)  # no step down then up
        self.assertGreaterEqual(chain[1] - chain[0], -1e-3)

    def test_the_fitted_transform_is_a_pure_rotation_aimed_down_the_bone(self):
        # task #76: orthonormal, det +1, X toward the main child; the old scale (0.7 to 1.1) is gone
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "skeleton", snap=0)
        self.assertEqual(cook(node).errors, ())
        geo = self.part(node, "fitted_skeleton")
        matrices = np.array(geo.pointFloatAttribValues("transform")).reshape(-1, 3, 3)
        fitted = np.array(geo.pointFloatAttribValues("P")).reshape(-1, 3)
        for number, matrix in enumerate(matrices):
            np.testing.assert_allclose(matrix @ matrix.T, np.eye(3), atol=1e-4, err_msg=self.names[number])
            self.assertAlmostEqual(float(np.linalg.det(matrix)), 1.0, places=4, msg=self.names[number])
        for name in ("leftarm", "leftforearm", "rightupleg", "rightleg", "spine2"):
            a = self.index[name]
            kids = [c for c, p in enumerate(self.parents) if p == a and np.linalg.norm(fitted[c] - fitted[a]) > 1e-3]
            cosines = [float(matrices[a][0] @ (fitted[c] - fitted[a]) / np.linalg.norm(fitted[c] - fitted[a]))
                       for c in kids]  # row 0 = the X axis: it points down one of the bones to a child
            self.assertGreater(max(cosines), 1.0 - 1e-4, name)

    def test_the_sample_body_is_posed_and_keeps_its_weights(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "body", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo, source = self.part(node, "fitted_body"), self.body.geometry()
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
        geo = self.part(node, "landmarks")
        names = list(geo.pointStringAttribValues("name"))
        self.assertEqual(names, list(LANDMARK_NAMES))
        for name, point in zip(names, geo.points()):
            self.assertLess(float(np.linalg.norm(np.array(point.position()) - truth[name])), 0.002, name)

    def test_landmark_modes(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        some = {name: truth[name] for name in ("lm_knee_l", "lm_knee_r")}
        partial = self.user_mesh(posed, some)
        # From Rig Landmarks needs all of them
        node = self.fit(partial, "landmarks", landmarks="landmarks")
        self.assertIn("lm_chin", " ".join(cook(node).errors))
        # Both: the positions that are set win, the others are guessed
        node = self.fit(partial, "landmarks", landmarks="both", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("2 from Rig Landmarks, 16 guessed", "\n".join(result.messages))
        point = {n: np.array(p.position()) for n, p in zip(self.part(node, "landmarks").pointStringAttribValues("name"), self.part(node, "landmarks").points())}
        self.assertLess(float(np.linalg.norm(point["lm_knee_l"] - truth["lm_knee_l"])), 0.002)
        # Auto-guess ignores the positions
        node = self.fit(partial, "landmarks", landmarks="guess", snap=0)
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("ignored", "\n".join(result.messages))
        point = {n: np.array(p.position()) for n, p in zip(self.part(node, "landmarks").pointStringAttribValues("name"), self.part(node, "landmarks").points())}
        self.assertGreater(float(np.linalg.norm(point["lm_knee_l"] - truth["lm_knee_l"])), 0.01)

    def test_the_positions_reach_the_fit_unchanged(self):
        """Criterion 8 (task #74): a position that Rig Landmarks writes is the landmark that Rig Fit uses."""
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(mesh, "landmarks", snap=0, symmetry=0, landmarks="landmarks")
        result = cook(node)
        self.assertEqual(result.errors, ())
        geo = self.part(node, "landmarks")
        used = {n: np.array(p.position()) for n, p in zip(geo.pointStringAttribValues("name"), geo.points())}
        for name in LANDMARK_NAMES:
            np.testing.assert_allclose(used[name], truth[name], atol=1e-6, err_msg=name)  # float32 detail vectors

    def test_the_auto_guess_finds_the_joints_of_the_sample_itself_and_of_a_bigger_copy(self):
        for size, limit in ((1.0, 0.02), (1.10, 0.05)):
            mats, world_pos = self.pose(arms=0.0, legs=0.0, arm_scale=1.0, leg_scale=1.0, size=size)
            posed = self.posed_points(mats)
            node = self.fit(self.user_mesh(posed), "landmarks", snap=0)
            self.assertEqual(cook(node).errors, ())
            geo = self.part(node, "landmarks")
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

    def test_task_79_refine_brings_the_sample_body_closer_to_a_known_target(self):
        """Task #79 (D44): the target is the posed sample with a 10 cm belly in front and a 25 % wider waist (a shape that a bone fit cannot make; the
        landmarks stay), so the target surface is known. Refine wraps the fitted body onto it: its points lie closer to the
        target surface (nearest target point, whatever the point numbers) than those of the plain fit."""
        mats, world_pos, truth, posed, mesh = self.scenario()
        belly = posed.copy()
        belly[:, 2] += 0.10 * np.exp(-(((posed[:, 1] - 0.2) / 0.15) ** 2)) * (posed[:, 2] > -0.02)
        belly[:, 0] *= 1.0 + 0.25 * np.exp(-(((posed[:, 1] - 0.2) / 0.25) ** 2))
        mesh = self.user_mesh(belly, truth)
        distance = {}
        for refine in (0, 1):
            node = self.fit(mesh, "body", snap=0, refine=refine)
            result = cook(node)
            self.assertEqual(result.errors, (), refine)
            got = np.array(self.part(node, "fitted_body").pointFloatAttribValues("P")).reshape(-1, 3)[self.used]
            near = np.array([np.sqrt(((belly - p) ** 2).sum(axis=1).min()) for p in got])
            distance[refine] = near
            print("   task 79, refine %d: %.2f cm mean, %.2f cm at most from the target" % (
                refine, 100 * near.mean(), 100 * near.max()))
            if refine:
                self.assertIn("Refine:", "\n".join(result.messages))
        self.assertLess(float(distance[1].mean()), 0.8 * float(distance[0].mean()))
        # the wrapped body keeps its groups and weights (Weight Transfer reads them)
        wrapped, source = self.part(node, "fitted_body"), self.body.geometry()
        self.assertEqual(sorted(g.name() for g in wrapped.pointGroups()), sorted(g.name() for g in source.pointGroups()))
        count = len(wrapped.points())
        np.testing.assert_allclose(rig_weights.read_selections("test", wrapped, self.names).weights,
                                   rig_weights.read_selections("test", source, self.names).weights[:count], atol=1e-6)

    def test_task_79_refine_wraps_onto_the_body_group_only(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        node = self.fit(self.user_mesh(posed, truth, group="skin"), "body", snap=0, refine=1, bodygroup="skin")
        self.assertEqual(cook(node).errors, ())
        node = self.fit(self.user_mesh(posed, truth, group="skin"), "body", snap=0, refine=1, bodygroup="nothing")
        self.assertIn("nothing", " ".join(cook(node).errors))
        node = self.fit(self.user_mesh(posed, truth, group=""), "body", snap=0, refine=1, bodygroup="")  # the whole mesh
        self.assertEqual(cook(node).errors, ())

    def test_the_output_holds_the_parts_by_name_and_show_only_hides(self):
        # task #80: a fourth part, rest_body (the sample as it came in, for Unpose), is never drawn
        mats, world_pos, truth, posed, mesh = self.scenario()
        outputs = {}
        for show in ("all", "skeleton", "body", "landmarks"):
            node = self.fit(mesh, show, snap=0)
            self.assertEqual(cook(node).errors, ())
            geo = node.geometry()
            prims = geo.prims()
            self.assertEqual([p.attribValue("a3ob_rig_part") for p in prims], ["fitted_skeleton", "fitted_body", "landmarks", "rest_body"])
            self.assertTrue(all(isinstance(p, hou.PackedPrim) for p in prims))
            hidden = [p.attribValue("a3ob_rig_part") for p in prims if p.intrinsicValue("viewportlod") == "hidden"]
            wanted = {"all": [], "skeleton": ["fitted_body", "landmarks"], "body": ["fitted_skeleton", "landmarks"],
                      "landmarks": ["fitted_skeleton", "fitted_body"]}[show] + ["rest_body"]
            wanted = sorted(wanted)
            hidden = sorted(hidden)
            self.assertEqual(hidden, wanted)
            rest = self.part(node, "rest_body")  # the sample as it came in
            np.testing.assert_array_equal(rest.pointFloatAttribValues("P"), self.body.geometry().pointFloatAttribValues("P"))
            outputs[show] = {name: (self.part(node, name).pointFloatAttribValues("P"),
                                    len(self.part(node, name).prims()), len(self.part(node, name).pointGroups()))
                             for name in ("fitted_skeleton", "fitted_body", "landmarks")}
        for show in ("skeleton", "body", "landmarks"):
            self.assertEqual(outputs[show], outputs["all"], show)

    def test_a_plain_geometry_is_its_own_part_and_a_missing_part_is_an_error(self):
        self.assertIs(rig_weights.read_part("test", self.body.geometry(), "fitted_body").__class__, hou.Geometry)
        self.assertEqual(len(rig_weights.read_part("test", self.body.geometry(), "fitted_body").points()),
                         len(self.body.geometry().points()))
        from a3ob_houdini.errors import UserError
        geo = hou.Geometry()
        rig_weights.write_parts(geo, {"landmarks": hou.Geometry()})
        with self.assertRaises(UserError):
            rig_weights.read_part("test", geo, "fitted_body")

    def test_the_fit_output_goes_straight_into_weight_transfer(self):
        mats, world_pos, truth, posed, mesh = self.scenario()
        body = self.fit(mesh, "all", snap=0)
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

"""The frames of the fitted skeleton (task #76): pure rotations aimed down the bones, and Weights Convert + stock Bone
Deform on a skeleton that has them. The core part runs under CPython; the Houdini part needs hython with KineFX (do not
set HOUDINI_PACKAGE_SKIP=1). It uses a small synthetic body, so it does not need the Arma 3 Samples.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

import numpy as np

from a3ob_core.rig import bone_frames, fit_skeleton
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

# A small body: 0 Pelvis, 1 Spine (same place as the Pelvis), 2 Spine1, 3 Spine2, 4 head, 5 LeftArm, 6 LeftHand (+X side),
# 7 RightArm, 8 RightHand, 9 LeftUpLeg, 10 LeftFoot (foot points forward, +Z), 11 RightUpLeg, 12 RightFoot.
NAMES = ["Pelvis", "Spine", "Spine1", "Spine2", "head", "LeftArm", "LeftHand", "RightArm", "RightHand", "LeftUpLeg",
         "LeftFoot", "RightUpLeg", "RightFoot"]
PARENTS = [-1, 0, 1, 2, 3, 3, 5, 3, 7, 0, 9, 0, 11]
REST = np.array([[0, 1.0, 0], [0, 1.0, 0], [0, 1.3, 0], [0, 1.5, 0], [0, 1.8, 0], [0.2, 1.5, 0], [0.7, 1.5, 0],
                 [-0.2, 1.5, 0], [-0.7, 1.5, 0], [0.1, 1.0, 0], [0.1, 0.0, 0.0], [-0.1, 1.0, 0], [-0.1, 0.0, 0.0]])
# The feet are one more joint each in a real body; here the leg is one bone and a foot is a chain end.


def fitted(scale=1.0):
    """The synthetic body moved and stretched unevenly, so the old frames carried scale."""
    stretch = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.2, 0.8, 0.8, 0.8, 0.8])
    targets = {}
    for i in range(len(PARENTS)):
        p = PARENTS[i]
        targets[i] = REST[i] * scale if p < 0 else targets[p] + (REST[i] - REST[p]) * scale * stretch[i]
    return fit_skeleton(PARENTS, REST, targets, "bone")


class TestBoneFrames(unittest.TestCase):
    def setUp(self):
        self.fit = fitted()
        self.frames = self.fit.frames

    def test_every_frame_is_a_pure_rotation(self):
        for i, frame in enumerate(self.frames):
            np.testing.assert_allclose(frame @ frame.T, np.eye(3), atol=1e-9, err_msg=NAMES[i])
            self.assertAlmostEqual(float(np.linalg.det(frame)), 1.0, places=9, msg=NAMES[i])
        self.assertGreater(float(np.abs(self.fit.scales - 1.0).max()), 0.1)  # the fit still scales the bones (skinning)

    def test_x_points_down_the_bone_to_the_main_child(self):
        positions = self.fit.positions
        main = {"Spine1": "Spine2", "Spine2": "head", "LeftArm": "LeftHand", "RightUpLeg": "RightFoot",
                "Pelvis": "Spine1", "Spine": "Spine1"}  # the Pelvis aims up the spine, through the zero-length bone
        for name, child in main.items():
            a, c = NAMES.index(name), NAMES.index(child)
            direction = positions[c] - positions[a]
            direction /= np.linalg.norm(direction)
            np.testing.assert_allclose(self.frames[a][:, 0], direction, atol=1e-9, err_msg=name)

    def test_left_and_right_frames_mirror(self):
        # x mirrors in world X, y (the front, +Z) is shared, so the two limbs turn alike
        for left, right in (("LeftArm", "RightArm"), ("LeftUpLeg", "RightUpLeg")):
            a, b = self.frames[NAMES.index(left)], self.frames[NAMES.index(right)]
            np.testing.assert_allclose(a[:, 0] * [-1, 1, 1], b[:, 0], atol=1e-9)
            np.testing.assert_allclose(a[:, 1], b[:, 1], atol=1e-9)

    def test_chain_ends_take_the_parent_frame_and_the_foot_gets_a_frame_that_is_not_degenerate(self):
        np.testing.assert_allclose(self.frames[NAMES.index("head")], self.frames[NAMES.index("Spine2")], atol=1e-9)
        np.testing.assert_allclose(self.frames[NAMES.index("LeftHand")], self.frames[NAMES.index("LeftArm")], atol=1e-9)
        # a bone along world Z: y falls back to world Y
        frames = bone_frames([-1, 0], np.array([[0.0, 0, 0], [0.0, 0, 1.0]]))
        np.testing.assert_allclose(frames[0][:, 0], [0, 0, 1], atol=1e-12)
        np.testing.assert_allclose(frames[0][:, 1], [0, 1, 0], atol=1e-12)

    def test_the_joint_positions_and_skinning_matrices_do_not_change(self):
        # frames are an addition: P and the (scaled) skinning matrices are what they were
        self.assertAlmostEqual(float(np.linalg.det(self.fit.linear[NAMES.index("LeftArm")])),
                               float(self.fit.scales[NAMES.index("LeftArm")]) ** 3, places=9)


def skeleton_geo(positions, frames):
    """A skeleton as the Rig Fit gives it: points with ``name`` and a row-vector 9-float ``transform``, and polylines."""
    geo = hou.Geometry()
    points = geo.createPoints([tuple(float(v) for v in p) for p in positions])
    geo.addAttrib(hou.attribType.Point, "name", "")
    geo.addAttrib(hou.attribType.Point, "transform", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    for point, name, frame in zip(points, NAMES, frames):
        point.setAttribValue("name", name)
        point.setAttribValue("transform", tuple(float(v) for v in frame.T.reshape(9)))
    for child, parent in enumerate(PARENTS):
        if parent >= 0:
            line = geo.createPolygon(is_closed=False)
            line.addVertex(points[parent])
            line.addVertex(points[child])
    return geo


def body_geo(positions, count=400, seed=3):
    """Points around the joints, each with a hard selection on the nearest joint (names are the bone names)."""
    rng = np.random.default_rng(seed)
    joints = positions[rng.integers(0, len(positions), size=count)]
    cloud = joints + rng.normal(0.0, 0.03, size=(count, 3))
    owner = np.argmin(((cloud[:, None, :] - positions[None]) ** 2).sum(axis=2), axis=1)
    geo = hou.Geometry()
    points = geo.createPoints([tuple(float(v) for v in p) for p in cloud])
    for number, name in enumerate(NAMES):
        members = [points[i] for i in range(count) if owner[i] == number]
        if members:
            geo.createPointGroup(hou.text.encodeAttrib(name)).add(members)
    return geo, cloud, owner


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestWeightsConvertAndBoneDeformOnFittedFrames(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.fit = fitted(scale=1.1)
        self.rest = make_source(self.network, skeleton_geo(self.fit.positions, self.fit.frames))
        geo, self.cloud, self.owner = body_geo(self.fit.positions)
        self.mesh = make_source(self.network, geo)
        convert = self.network.createNode("a3ob_weights_convert")
        convert.setInput(0, self.mesh)
        convert.setInput(1, self.rest)
        convert.parm("mode").set("tocapture")
        self.assertEqual(cook(convert).errors, ())
        self.capture = convert

    def deform(self, pose):
        node = self.network.createNode("bonedeform")
        node.setInput(0, self.capture)
        node.setInput(1, self.rest)
        node.setInput(2, pose)
        self.assertEqual(cook(node).errors, ())
        return np.array(node.geometry().pointFloatAttribValues("P")).reshape(-1, 3)

    def posed(self, joint=None, degrees=40.0):
        geo = hou.Geometry()
        geo.copy(self.rest.geometry())
        if joint is not None:
            c, s = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
            turn = np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])  # about Z, row-vector form
            point = geo.points()[joint]
            matrix = np.array(point.attribValue("transform")).reshape(3, 3)
            point.setAttribValue("transform", tuple(float(v) for v in (matrix @ turn).reshape(9)))
        return make_source(self.network, geo)

    def test_the_fitted_skeleton_is_pure_rotations_in_the_scene(self):
        matrices = np.array(self.rest.geometry().pointFloatAttribValues("transform")).reshape(-1, 3, 3)
        for matrix in matrices:
            np.testing.assert_allclose(matrix @ matrix.T, np.eye(3), atol=1e-5)

    def test_a_zero_pose_leaves_the_mesh_within_0_1_mm(self):
        result = self.deform(self.posed())
        self.assertLess(float(np.abs(result - self.cloud).max()), 1e-4)

    def test_turning_one_joint_moves_only_its_region(self):
        joint = NAMES.index("LeftArm")
        result = self.deform(self.posed(joint))
        moved = np.linalg.norm(result - self.cloud, axis=1) > 1e-4
        self.assertTrue(moved.any())
        self.assertFalse((moved & (self.owner != joint)).any())


if __name__ == "__main__":
    unittest.main()

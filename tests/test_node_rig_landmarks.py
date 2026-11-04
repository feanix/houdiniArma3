"""The Rig Landmarks node in hython (task #71). The group tests need only ``hou``; the comparison with Rig Fit needs KineFX
(do not run it with HOUDINI_PACKAGE_SKIP=1) and the Arma 3 Samples (it skips without them).

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


def line_mesh(count=10):
    """A strip of ``count`` points with one triangle, as a File SOP geometry."""
    geo = hou.Geometry()
    points = geo.createPoints([(float(i), 0.0, 0.0) for i in range(count)])
    prim = geo.createPolygon()
    for point in points[:3]:
        prim.addVertex(point)
    return geo


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestRigLandmarksGroups(NodeTestCase):
    def make(self, geo=None, **fields):
        source = make_source(self.network, geo if geo is not None else line_mesh())
        node = self.network.createNode("a3ob_rig_landmarks")
        node.setInput(0, source)
        for name, text in fields.items():
            node.parm(name).set(text)
        return node

    def members(self, node):
        return {g.name(): sorted(p.number() for p in g.points()) for g in node.geometry().pointGroups()}

    def test_the_fields_are_the_landmarks_of_rig_fit_in_order(self):
        node = self.make()
        names = [p.name() for p in node.parms() if p.name().startswith("lm_")]
        self.assertEqual(names, list(LANDMARK_NAMES))

    def test_a_set_field_writes_its_group_and_an_empty_field_writes_none(self):
        node = self.make(lm_chin="0 1", lm_knee_l="5")
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(self.members(node), {"lm_chin": [0, 1], "lm_knee_l": [5]})
        self.assertIn("2 of 14 landmarks set", "\n".join(result.messages))
        self.assertIn("Left to the guess in Rig Fit", "\n".join(result.messages))

    def test_the_mesh_itself_is_not_changed(self):
        node = self.make(lm_chin="2")
        source = node.inputs()[0]
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(node.geometry().pointFloatAttribValues("P"), source.geometry().pointFloatAttribValues("P"))
        self.assertEqual(len(node.geometry().prims()), len(source.geometry().prims()))

    def test_a_field_that_matches_no_points_is_a_warning_naming_it(self):
        node = self.make(lm_chin="999999", lm_groin="3")
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("Chin", " ".join(result.warnings))
        self.assertEqual(self.members(node), {"lm_groin": [3]})

    def test_existing_groups_are_replaced_by_a_set_field_and_kept_otherwise(self):
        geo = line_mesh()
        points = geo.points()
        geo.createPointGroup("lm_chin").add(points[0])
        geo.createPointGroup("lm_groin").add(points[1])
        geo.createPointGroup("other").add(points[2])
        node = self.make(geo, lm_chin="7")
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertEqual(self.members(node), {"lm_chin": [7], "lm_groin": [1], "other": [2]})
        self.assertIn("Kept from the input (field empty): Groin", "\n".join(result.messages))

    def test_a_field_may_name_a_group_of_the_input(self):
        geo = line_mesh()
        geo.createPointGroup("armpit").add(geo.points()[4:6])
        node = self.make(geo, lm_shoulder_l="armpit")
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(self.members(node)["lm_shoulder_l"], [4, 5])

    def test_the_fields_carry_the_viewport_select_button(self):
        node = self.make()
        template = node.parm("lm_chin").parmTemplate()
        self.assertIn("selectGroupParm", template.tags()["script_action"])
        self.assertIn("Points", template.tags()["script_action"])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestRigLandmarksWithRigFit(NodeTestCase):
    def test_rig_fit_on_the_node_equals_rig_fit_on_group_nodes(self):
        path = samples_support.path
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(path(SAMPLE).replace("\\", "/"))
        body = self.network.createNode("a3ob_extract_lod")
        body.setInput(0, importer)
        body.parm("lod").set("Resolution 1")
        skeleton = self.network.createNode("a3ob_rig_skeleton")
        skeleton.parm("cfg").set(path(SAMPLE_CFG).replace("\\", "/"))
        skeleton.parm("pivots").set(path(SAMPLE_PIVOTS).replace("\\", "/"))
        for node in (body, skeleton):
            self.assertEqual(cook(node).errors, ())
        from a3ob_houdini import rig_weights
        info = rig_weights.read_skeleton("test", skeleton.geometry())
        rest = np.array(info.positions)
        index = {n.lower(): i for i, n in enumerate(info.names)}
        flat = np.array(body.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        # landmarks: the 6 points of the body closest to the joint of each of 6 landmarks; the others are guessed
        chosen = {}
        for name in ("lm_shoulder_l", "lm_shoulder_r", "lm_elbow_l", "lm_elbow_r", "lm_knee_l", "lm_knee_r"):
            joint = rest[index[BONE_OF[name].lower()]]
            chosen[name] = " ".join(str(int(i)) for i in np.argsort(np.linalg.norm(flat - joint, axis=1))[:6])

        node = self.network.createNode("a3ob_rig_landmarks")
        node.setInput(0, body)
        for name, text in chosen.items():
            node.parm(name).set(text)
        by_node = self.network.createNode("a3ob_rig_fit")
        by_node.setInput(0, node)
        by_node.setInput(1, skeleton)
        by_node.setInput(2, body)

        previous = body
        for name, text in chosen.items():
            group = self.network.createNode("groupcreate", name)
            group.setInput(0, previous)
            group.parm("groupname").set(name)
            group.parm("grouptype").set("point")
            group.parm("groupbase").set(1)
            group.parm("basegroup").set(text)
            previous = group
        by_groups = self.network.createNode("a3ob_rig_fit")
        by_groups.setInput(0, previous)
        by_groups.setInput(1, skeleton)
        by_groups.setInput(2, body)

        results = [cook(by_node), cook(by_groups)]
        for result in results:
            self.assertEqual(result.errors, ())
        self.assertEqual(results[0].messages, results[1].messages)
        self.assertIn("6 from groups, 8 guessed", "\n".join(results[0].messages))
        for part in ("fitted_skeleton", "fitted_body", "landmarks"):
            first = rig_weights.read_part("test", by_node.geometry(), part).pointFloatAttribValues("P")
            second = rig_weights.read_part("test", by_groups.geometry(), part).pointFloatAttribValues("P")
            self.assertEqual(first, second, part)


if __name__ == "__main__":
    unittest.main()

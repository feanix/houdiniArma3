"""Group pickers (task 50): the Group field of Material Assign and Mass is a real Houdini group picker.

Checks, on the real assets from ``houdini/otls``: the parameter template carries the menu script and the select action
(the ones Houdini's own SOPs carry), the menu lists the input's groups of the right kind (both Group Type settings),
the menu shows the decoded P3D name of an encoded selection, and a cook with a picked group or with point numbers gives
what it gave before. The generator side (table, DialogScript text) also runs under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_houdini import docs_common, hda_source, nodes, params
from a3ob_houdini.params import NodeSpec, Parm
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

if HAS_HOU:
    from a3ob_houdini import names

CUBE_FACES = ((0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))


class TestGeneratorKind(unittest.TestCase):
    def test_the_two_nodes_use_the_group_kind(self):
        material = nodes.load("material_assign").SPEC.parm_index["group"]
        self.assertEqual((material.kind, material.group_follow), ("group", "grouptype"))
        mass = nodes.load("mass").SPEC.parm_index["group"]
        self.assertEqual((mass.kind, mass.group_type, mass.group_follow), ("group", "points", ""))

    def test_the_dialog_script_has_the_menu_and_the_select_action(self):
        text = hda_source._dialog_script(nodes.load("material_assign").SPEC)
        self.assertIn("menutoggle {", text)
        self.assertIn("group_menu", text)
        self.assertIn('"script_action"', text)
        self.assertIn("soputils.selectGroupParm(kwargs)", text)
        self.assertIn("parmTuple('grouptype')", text)
        self.assertIn('"script_action_icon" "BUTTONS_reselect"', text)

    def test_a_fixed_type_hands_selectgroupparm_a_geometry_type(self):
        self.assertIn("[hou.geometryType.Points]", params.group_action_script(Parm("g", "G", "group", group_type="points")))

    def test_wrong_use_is_refused(self):
        with self.assertRaises(ValueError):
            Parm("g", "G", "group", group_type="edges")
        with self.assertRaises(ValueError):
            Parm("g", "G", "string", group_follow="x")
        with self.assertRaises(ValueError):
            NodeSpec("x", "X", "s", (Parm("g", "G", "group", group_follow="missing"),))

    def test_the_docs_say_group_picker(self):
        for kind in ("material_assign", "mass"):
            self.assertIn("Group picker", docs_common.type_text(nodes.load(kind).SPEC.parm_index["group"]))


def box_with_groups():
    """A cube with a point group, a primitive group and a primitive group whose name is an encoded selection."""
    geo = hou.Geometry()
    points = [geo.createPoint() for _ in range(8)]
    for number, (x, y, z) in enumerate([(a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]):
        points[number].setPosition((x, y, z))
    for face in CUBE_FACES:
        polygon = geo.createPolygon()
        for number in face:
            polygon.addVertex(points[number])
    geo.createPointGroup("lid").add(points[:4])
    geo.createPrimGroup("top").add(geo.prims()[:2])
    geo.createPrimGroup(names.encode_group_name("Zbrojění")).add(geo.prims()[2:4])
    return geo


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestPickerOnAssets(NodeTestCase):
    def source(self):
        return make_source(self.network, box_with_groups())

    def node(self, kind):
        node = self.network.createNode(kind)
        node.setInput(0, self.source())
        return node

    def test_templates_carry_the_menu_script_and_the_select_action(self):
        for kind in ("a3ob_material_assign", "a3ob_mass"):
            template = self.network.createNode(kind).parmTemplateGroup().find("group")
            self.assertEqual(template.menuType(), hou.menuType.StringToggle)
            self.assertIn("group_menu", template.itemGeneratorScript())
            self.assertEqual(template.itemGeneratorScriptLanguage(), hou.scriptLanguage.Python)
            tags = template.tags()
            self.assertIn("soputils.selectGroupParm(kwargs)", tags["script_action"])
            self.assertEqual(tags["script_action_icon"], "BUTTONS_reselect")
            self.assertTrue(tags["script_action_help"])
        self.assertIn("parmTuple('grouptype')",
                      self.network.createNode("a3ob_material_assign").parmTemplateGroup().find("group").tags()["script_action"])

    def test_material_assign_menu_follows_group_type(self):
        node = self.node("a3ob_material_assign")
        node.parm("grouptype").set("prims")
        tokens = [t for t in node.parm("group").menuItems() if not t.startswith("@")]
        self.assertEqual(sorted(tokens), sorted(["top", names.encode_group_name("Zbrojění")]))
        node.parm("grouptype").set("points")
        self.assertEqual([t for t in node.parm("group").menuItems() if not t.startswith("@")], ["lid"])

    def test_mass_menu_lists_point_groups(self):
        node = self.node("a3ob_mass")
        self.assertEqual([t for t in node.parm("group").menuItems() if not t.startswith("@")], ["lid"])

    def test_an_encoded_selection_shows_its_p3d_name_and_keeps_its_group_name_as_token(self):
        node = self.node("a3ob_material_assign")
        encoded = names.encode_group_name("Zbrojění")
        self.assertNotEqual(encoded, "Zbrojění")
        items = node.parm("group").menuItems()
        labels = node.parm("group").menuLabels()
        self.assertTrue(labels[items.index(encoded)].startswith("Zbrojění"), labels)  # Houdini adds the token
        self.assertEqual(labels[items.index("top")], "top")

    def test_no_input_gives_an_empty_menu(self):
        self.assertEqual(self.network.createNode("a3ob_mass").parm("group").menuItems(), ())

    def test_cooks_with_a_picked_group_and_with_numbers_are_as_before(self):
        node = self.node("a3ob_material_assign")
        node.parm("texture").set("a.paa")
        node.parm("setmat").set(0)
        node.parm("group").set("top")
        cook(node)
        self.assertEqual([t == "a.paa" for t in node.geometry().primStringAttribValues("a3ob_texture")],
                         [True, True, False, False, False, False])
        node.parm("group").set("0-2")
        cook(node)
        self.assertEqual(sum(t == "a.paa" for t in node.geometry().primStringAttribValues("a3ob_texture")), 3)
        node.parm("grouptype").set("points")
        node.parm("group").set("lid")
        cook(node)
        self.assertEqual([t == "a.paa" for t in node.geometry().primStringAttribValues("a3ob_texture")],
                         [True, False, False, False, False, False])  # only face 0 has all its points in "lid"
        mass = self.node("a3ob_mass")
        mass.parm("mode").set("each")
        mass.parm("mass_each").set(2.0)
        mass.parm("group").set("lid")
        cook(mass)
        self.assertEqual(list(mass.geometry().pointFloatAttribValues("a3ob_mass")), [2.0] * 4 + [0.0] * 4)
        mass.parm("group").set("0-3")
        cook(mass)
        self.assertEqual(list(mass.geometry().pointFloatAttribValues("a3ob_mass")), [2.0] * 4 + [0.0] * 4)


if __name__ == "__main__":
    unittest.main()

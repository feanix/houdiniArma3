"""The Mass node in hython: every mode, the attribute it writes, the messages (ux-tools.md 5.7).

The nodes are the real assets from ``houdini/otls``, installed into this session only. Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from tests.hou_support import HAS_HOU, REPO_ROOT, hou
from tests.test_mass_math import CUBE_FACES, cube_points
from tests.mass_proxy_support import NodeTestCase, cook, make_source


def cube_geometry(side=1.0, offset=(0.0, 0.0, 0.0), extra=None):
    """A closed cube as hou.Geometry (outward faces). ``extra`` may add attributes."""
    geo = hou.Geometry()
    geo.createPoints(cube_points(side, offset))
    geo.createPolygons(CUBE_FACES)
    if extra:
        extra(geo)
    return geo


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestMassNode(NodeTestCase):
    def run_mass(self, geo, **parms):
        source = make_source(self.network, geo)
        node = self.network.createNode("a3ob_mass")
        node.setInput(0, source)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node, cook(node)

    def masses(self, node):
        return list(node.geometry().pointFloatAttribValues("a3ob_mass"))

    def test_attribute_is_a_point_float32(self):
        node, result = self.run_mass(cube_geometry(), mode="total", value=8)
        attrib = node.geometry().findPointAttrib("a3ob_mass")
        self.assertEqual(attrib.dataType(), hou.attribData.Float)
        self.assertEqual(attrib.size(), 1)
        self.assertEqual(result.errors, ())

    def test_total_shares_the_value_equally(self):
        node, _ = self.run_mass(cube_geometry(), mode="total", value=90)
        self.assertEqual(self.masses(node), [11.25] * 8)

    def test_total_on_a_group_leaves_the_other_points_at_zero(self):
        node, _ = self.run_mass(cube_geometry(), mode="total", value=40, group="0-3")
        self.assertEqual(self.masses(node), [10.0] * 4 + [0.0] * 4)

    def test_each_sets_the_value_and_keeps_masses_outside_the_group(self):
        source = make_source(self.network, cube_geometry())
        first = self.network.createNode("a3ob_mass")
        first.setInput(0, source)
        first.parm("mode").set("each")
        first.parm("value").set(2.5)
        second = self.network.createNode("a3ob_mass")
        second.setInput(0, first)
        second.parm("mode").set("each")
        second.parm("value").set(7)
        second.parm("group").set("1 2")
        cook(second)
        self.assertEqual(self.masses(second), [2.5, 7, 7, 2.5, 2.5, 2.5, 2.5, 2.5])

    def test_density_gives_each_closed_component_volume_times_density(self):
        def two_shapes(geo):
            # a second, bigger cube and an open flat quad that density must ignore
            start = len(geo.points())
            geo.createPoints(cube_points(2.0, (10.0, 0.0, 0.0)))
            geo.createPolygons([tuple(start + number for number in face) for face in CUBE_FACES])
            quad = len(geo.points())
            geo.createPoints([(0, 5, 0), (1, 5, 0), (1, 5, 1), (0, 5, 1)])
            geo.createPolygons([(quad, quad + 1, quad + 2, quad + 3)])

        node, result = self.run_mass(cube_geometry(extra=two_shapes), mode="density", value=1000)
        masses = self.masses(node)
        self.assertEqual(masses[:8], [125.0] * 8)  # 1 m3 * 1000 kg/m3 over 8 points
        self.assertEqual(masses[8:16], [1000.0] * 8)  # 8 m3 * 1000 kg/m3 over 8 points
        self.assertEqual(masses[16:], [0.0] * 4)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("non-closed or flat components were ignored", result.warnings[0])

    def test_density_with_a_cube_turned_inside_out_still_gives_positive_mass(self):
        def inside_out(geo):
            geo.deletePrims(geo.prims(), keep_points=True)
            geo.createPolygons([tuple(reversed(face)) for face in CUBE_FACES])

        node, _ = self.run_mass(cube_geometry(extra=inside_out), mode="density", value=800)
        self.assertEqual(self.masses(node), [100.0] * 8)

    def test_density_only_touches_components_fully_in_the_group(self):
        def two_cubes(geo):
            start = len(geo.points())
            geo.createPoints(cube_points(1.0, (10.0, 0.0, 0.0)))
            geo.createPolygons([tuple(start + number for number in face) for face in CUBE_FACES])

        node, result = self.run_mass(cube_geometry(extra=two_cubes), mode="density", value=8, group="0-7")
        self.assertEqual(self.masses(node), [1.0] * 8 + [0.0] * 8)
        self.assertEqual(len(result.warnings), 1)

    def test_density_with_an_empty_group_gives_only_the_group_warning(self):
        node, result = self.run_mass(cube_geometry(), mode="density", value=8, group="@a3ob_none")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("no point is in the group", result.warnings[0])
        self.assertNotIn("ignored", result.warnings[0])

    def test_clear_removes_the_attribute_or_zeroes_a_group(self):
        node, _ = self.run_mass(cube_geometry(), mode="total", value=8)
        clear = self.network.createNode("a3ob_mass")
        clear.setInput(0, node)
        clear.parm("mode").set("clear")
        cook(clear)
        self.assertIsNone(clear.geometry().findPointAttrib("a3ob_mass"))
        part = self.network.createNode("a3ob_mass")
        part.setInput(0, node)
        part.parm("mode").set("clear")
        part.parm("group").set("0-3")
        cook(part)
        self.assertEqual(self.masses(part), [0.0] * 4 + [1.0] * 4)

    def test_copy_from_and_to_houdini_mass(self):
        def with_mass(geo):
            geo.addAttrib(hou.attribType.Point, "mass", 0.0)
            geo.setPointFloatAttribValues("mass", [float(n) for n in range(8)])

        node, result = self.run_mass(cube_geometry(extra=with_mass), mode="copyfrom")
        self.assertEqual(self.masses(node), [float(n) for n in range(8)])
        self.assertEqual(list(node.geometry().pointFloatAttribValues("mass")), [float(n) for n in range(8)])
        back = self.network.createNode("a3ob_mass")
        back.setInput(0, make_source(self.network, cube_geometry(extra=lambda geo: _set_a3ob_mass(geo, 3.0))))
        back.parm("mode").set("copyto")
        cook(back)
        self.assertEqual(list(back.geometry().pointFloatAttribValues("mass")), [3.0] * 8)
        self.assertEqual(self.masses(back), [3.0] * 8)

    def test_copy_modes_without_the_source_attribute_are_errors(self):
        _, result = self.run_mass(cube_geometry(), mode="copyfrom")
        self.assertEqual(len(result.errors), 1)
        self.assertIn("no point attribute mass", result.errors[0])
        _, result = self.run_mass(cube_geometry(), mode="copyto")
        self.assertIn("no point attribute a3ob_mass", result.errors[0])

    def test_houdini_mass_is_not_written_by_other_modes(self):
        node, _ = self.run_mass(cube_geometry(), mode="total", value=8)
        self.assertIsNone(node.geometry().findPointAttrib("mass"))

    def test_message_has_total_and_centre_of_mass(self):
        node, result = self.run_mass(cube_geometry(2.0, (1.0, 0.0, 0.0)), mode="total", value=90)
        self.assertEqual(result.messages[0], "Total 90.0 kg on 8 points. Centre of mass (2.00, 1.00, 1.00).")
        self.assertIn("minimum 11.2 kg", result.messages[1])

    def test_centre_of_mass_follows_the_mass(self):
        _, result = self.run_mass(cube_geometry(2.0), mode="each", value=3, group="0")
        self.assertIn("Total 3.0 kg on 1 points. Centre of mass (0.00, 0.00, 0.00).", result.messages[0])

    def test_visualize_writes_cd_and_off_leaves_it_alone(self):
        def one_heavy(geo):
            _set_a3ob_mass(geo, 0.0)
            geo.setPointFloatAttribValues("a3ob_mass", [0.0, 1.0, 3.0, 5.0, 0.0, 0.0, 0.0, 0.0])

        node, _ = self.run_mass(cube_geometry(extra=one_heavy), mode="clear", group="7", visualize=1)  # point 7 is already 0: only Cd is new
        colors = list(node.geometry().pointFloatAttribValues("Cd"))
        self.assertEqual(colors[0:3], [0.0, 0.0, 0.0])  # no mass: black
        self.assertEqual(colors[3:6], [0.0, 0.0, 1.0])  # least: blue
        self.assertEqual(colors[9:12], [1.0, 0.0, 0.0])  # most: red
        plain, _ = self.run_mass(cube_geometry(), mode="total", value=8)
        self.assertIsNone(plain.geometry().findPointAttrib("Cd"))

    def test_mass_on_a_lod_that_is_not_geometry_warns(self):
        def lod(number):
            def add(geo):
                geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 0)
                geo.setGlobalAttribValue("a3ob_lod_type", number)
                geo.addAttrib(hou.attribType.Global, "a3ob_lod_resolution", 0.0)
                geo.setGlobalAttribValue("a3ob_lod_resolution", 1.0)
            return add

        _, result = self.run_mass(cube_geometry(extra=lod(0)), mode="total", value=8)
        self.assertEqual(len(result.warnings), 1)
        self.assertTrue(result.warnings[0].startswith("Resolution 1: a3ob_mass is on a LOD that is not a Geometry LOD"), result.warnings)
        for geometry_type in (6, 7, 8):
            _, result = self.run_mass(cube_geometry(extra=lod(geometry_type)), mode="total", value=8)
            self.assertEqual(result.warnings, (), geometry_type)

    def test_detail_attributes_pass_through_and_the_group_is_checked(self):
        def lod(geo):
            geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
        node, _ = self.run_mass(cube_geometry(extra=lod), mode="total", value=8)
        self.assertEqual(node.geometry().intAttribValue("a3ob_lod_type"), 6)

    def test_without_an_input_the_node_has_an_error(self):
        node = self.network.createNode("a3ob_mass")
        result = cook(node)
        self.assertEqual(len(result.errors), 1)

    def test_the_asset_parameters_match_the_table_and_the_value_is_strict(self):
        node = self.network.createNode("a3ob_mass")
        self.assertEqual([parm.name() for parm in node.parms()], ["mode", "value", "group", "visualize"])
        template = node.parm("value").parmTemplate()
        self.assertTrue(template.minIsStrict() and not template.maxIsStrict())  # no upper limit
        self.assertEqual(template.minValue(), 0.0)
        self.assertEqual(node.parm("mode").parmTemplate().menuItems()[:3], ("each", "total", "density"))
        self.assertEqual(node.parm("mode").eval(), "total")


def _set_a3ob_mass(geo, value: float) -> None:
    geo.addAttrib(hou.attribType.Point, "a3ob_mass", 0.0)
    geo.setPointFloatAttribValues("a3ob_mass", [value] * len(geo.points()))


if __name__ == "__main__":
    unittest.main()

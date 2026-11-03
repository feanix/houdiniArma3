"""The Mass node in hython: every mode, the attribute it writes, the messages (ux-tools.md 5.7).

The nodes are the real assets from ``houdini/otls``, installed into this session only. Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import unittest

from tests.hou_support import HAS_HOU, REPO_ROOT, hou
from tests.test_mass_math import CUBE_FACES, cube_points
from tests.mass_proxy_support import NodeTestCase, cook, make_source

if HAS_HOU:
    from a3ob_houdini import node_visualizer
    from a3ob_houdini.nodes import mass as mass_node


MODE_PARM = {"each": "mass_each", "total": "mass_total", "density": "density"}  # the one parameter each mode reads


class FakeViz:
    """Stands in for hou.ViewportVisualizer (hython has none): keeps what the code sets."""

    def __init__(self, owner, node):
        self.owner, self.node, self.type, self._name, self.parms, self.active = owner, node, None, "", {}, False

    def setType(self, vtype):
        self.type = vtype

    def setName(self, name):
        self._name = name

    def name(self):
        return self._name

    def setLabel(self, label):
        self.label = label

    def setParm(self, name, value):
        self.parms[name] = value

    def evalParmAsFloat(self, name):
        return float(self.parms.get(name, 0.0))

    def setIsActive(self, on):
        self.active = bool(on)

    def destroy(self):
        self.owner.items.remove(self)


class FakeVisualizers:
    """Stands in for hou.viewportVisualizers, with the calls the code uses (signatures from the Houdini docs)."""

    def __init__(self):
        self.items = []

    def type(self, name):
        return name

    def createVisualizer(self, vtype, category=None, node=None):
        viz = FakeViz(self, node)
        viz.type = vtype
        self.items.append(viz)
        return viz

    def visualizers(self, category=None, node=None):
        return tuple(viz for viz in self.items if viz.node == node)

    def only(self):
        assert len(self.items) == 1, self.items
        return self.items[0]


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
            node.parm(MODE_PARM[parms["mode"]] if name == "value" else name).set(value)
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
        first.parm("mass_each").set(2.5)
        second = self.network.createNode("a3ob_mass")
        second.setInput(0, first)
        second.parm("mode").set("each")
        second.parm("mass_each").set(7)
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

    def test_visualize_does_not_write_cd(self):
        # #32: Visualize is a node visualizer; an input Cd passes through unchanged, and none is made
        def with_cd(geo):
            geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
            geo.setPointFloatAttribValues("Cd", [0.25, 0.5, 0.75] * 8)

        node, result = self.run_mass(cube_geometry(extra=with_cd), mode="total", value=8, visualize=1)
        self.assertEqual(result.errors, ())
        self.assertEqual(list(node.geometry().pointFloatAttribValues("Cd")), [0.25, 0.5, 0.75] * 8)
        self.assertEqual(self.masses(node), [1.0] * 8)
        bare, _ = self.run_mass(cube_geometry(), mode="total", value=8, visualize=1)
        self.assertIsNone(bare.geometry().findPointAttrib("Cd"))

    def test_visualize_toggle_runs_the_visualizer_callback(self):
        template = self.network.createNode("a3ob_mass").parm("visualize").parmTemplate()
        self.assertIn("e.button('mass', 'visualize', kwargs)", template.scriptCallback())

    def test_hython_has_no_viewport_visualizers_and_the_toggle_is_harmless(self):
        # Research finding (#32): hou.viewportVisualizers exists only with the Houdini window.
        node, _ = self.run_mass(cube_geometry(), mode="total", value=8, visualize=1)
        self.assertEqual(hasattr(hou, "viewportVisualizers"), hou.isUIAvailable())
        mass_node.visualize({"node": node})
        mass_node.loaded(node)
        self.assertIsNone(node_visualizer.find(node, mass_node.VISUALIZER))

    def test_visualize_makes_and_removes_a_node_color_visualizer(self):
        fake = FakeVisualizers()
        hou.viewportVisualizers = fake
        try:
            node, _ = self.run_mass(cube_geometry(), mode="each", value=2.0, visualize=1)
            mass_node.visualize({"node": node})
            viz = fake.only()
            self.assertEqual((viz.name(), viz.type, viz.parms["attrib"], viz.parms["colortype"]),
                             ("a3ob_mass_color", "vis_color", "a3ob_mass", "attribramped"))
            self.assertTrue(viz.active)
            self.assertEqual((viz.evalParmAsFloat("minscalar"), viz.evalParmAsFloat("maxscalar")), (0.0, 2.0))
            ramp = viz.parms["colorramp"]
            for got, want in zip(ramp.keys(), (0.0, 0.001, 0.25, 0.5, 0.75, 1.0)):
                self.assertAlmostEqual(got, want, places=6)
            self.assertEqual((ramp.values()[0], ramp.values()[1], ramp.values()[-1]),
                             ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)))  # black, blue, red
            node.parm("mass_each").set(5.0)  # the cook keeps the range on the masses
            cook(node)
            self.assertEqual(viz.evalParmAsFloat("maxscalar"), 5.0)
            mass_node.visualize({"node": node})  # again: no second visualizer
            self.assertEqual(len(fake.items), 1)
            node.parm("visualize").set(0)
            mass_node.visualize({"node": node})
            self.assertEqual(fake.items, [])
        finally:
            del hou.viewportVisualizers

    def test_loading_a_scene_with_visualize_on_makes_the_visualizer(self):
        fake = FakeVisualizers()
        hou.viewportVisualizers = fake
        try:
            node, _ = self.run_mass(cube_geometry(), mode="each", value=2.0, visualize=1)
            mass_node.loaded(node)
            self.assertEqual(len(fake.items), 1)
        finally:
            del hou.viewportVisualizers

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

    def test_each_mode_reads_only_its_own_parameter(self):
        # B3: the two other numbers get values that would change the result if the mode read them
        wanted = {"each": [3.0] * 8, "total": [3.0] * 8, "density": [3.0 / 8] * 8}
        number = {"each": 3.0, "total": 24.0, "density": 3.0}
        for mode, own in MODE_PARM.items():
            node, _ = self.run_mass(cube_geometry(), mode=mode)
            node.parm(own).set(number[mode])
            for name in MODE_PARM.values():
                if name != own:
                    node.parm(name).set(555.0)
            cook(node)
            self.assertEqual(self.masses(node), wanted[mode], mode)

    def test_a_mode_parameter_is_hidden_unless_its_mode_is_chosen(self):
        node = self.network.createNode("a3ob_mass")
        for token, name in MODE_PARM.items():
            condition = node.parm(name).parmTemplate().conditionals()[hou.parmCondType.HideWhen]
            self.assertEqual(condition, "{ mode != %s }" % token, name)  # so Clear and the copy modes hide all three

    def old_style_node(self, mode, value):
        """A node as an old scene holds it: the shared hidden "value" set, the three own parameters untouched."""
        node = self.network.createNode("a3ob_mass")
        node.setInput(0, make_source(self.network, cube_geometry()))
        node.parm("mode").set(mode)
        node.parm("value").set(value)
        return node

    def test_the_cook_no_longer_reads_the_legacy_value(self):
        node = self.old_style_node("each", 2.5)
        cook(node)
        self.assertEqual(self.masses(node), [1.0] * 8)  # own parameter at its default: the old value waits for the load

    def test_loading_moves_the_old_value_into_the_mode_parameter(self):
        # #32 (follow-up of #39): saved in a hip and loaded again, the OnLoaded event does the move
        wanted = (("each", 2.5, "mass_each", [2.5] * 8), ("total", 40.0, "mass_total", [5.0] * 8),
                  ("density", 1000.0, "density", [1000.0 / 8] * 8))
        paths = []
        for mode, value, _, _ in wanted:
            paths.append(self.old_style_node(mode, value).path())
        hip = os.path.join(tempfile.mkdtemp(prefix="a3ob_mass_hip_"), "old.hip")
        hou.hipFile.save(hip)
        hou.hipFile.load(hip, suppress_save_prompt=True)
        self.network = hou.node(os.path.dirname(paths[0]))  # the old reference is gone after a load
        for path, (mode, value, own, expected) in zip(paths, wanted):
            node = hou.node(path)
            self.assertEqual(node.parm(own).eval(), value, mode)
            self.assertEqual(node.parm("value").eval(), -1.0, mode)
            cook(node)
            self.assertEqual(self.masses(node), expected, mode)
        # after the load a typed 1.0 takes effect (it used to be mistaken for "not typed")
        node = hou.node(paths[0])
        node.parm("mass_each").set(1.0)
        cook(node)
        self.assertEqual(self.masses(node), [1.0] * 8)

    def test_loading_keeps_a_number_already_typed_in_the_mode_parameter(self):
        node = self.old_style_node("each", 40.0)
        node.parm("mass_each").set(7.0)
        hip = os.path.join(tempfile.mkdtemp(prefix="a3ob_mass_hip_"), "both.hip")
        path = node.path()
        hou.hipFile.save(hip)
        hou.hipFile.load(hip, suppress_save_prompt=True)
        node = hou.node(path)
        self.network = node.parent()
        self.assertEqual((node.parm("mass_each").eval(), node.parm("value").eval()), (7.0, -1.0))

    def test_loaded_with_nothing_old_changes_nothing(self):
        node, _ = self.run_mass(cube_geometry(), mode="total", value=8)
        mass_node.loaded(node)
        self.assertEqual((node.parm("mass_total").eval(), node.parm("value").eval()), (8.0, -1.0))

    def test_loaded_in_a_mode_without_a_number_just_drops_the_old_value(self):
        node = self.old_style_node("clear", 7.0)
        mass_node.loaded(node)
        self.assertEqual([node.parm(name).eval() for name in ("mass_each", "mass_total", "density", "value")],
                         [1.0, 1.0, 1.0, -1.0])

    def test_the_asset_has_an_on_loaded_event(self):
        definition = hou.nodeType(hou.sopNodeTypeCategory(), "a3ob_mass").definition()
        self.assertIn("a3ob_houdini.entry", definition.sections()["OnLoaded"].contents())
        self.assertIn("except ImportError", definition.sections()["OnLoaded"].contents())

    def test_the_asset_parameters_match_the_table_and_the_value_is_strict(self):
        node = self.network.createNode("a3ob_mass")
        self.assertEqual([parm.name() for parm in node.parms()],
                         ["mode", "mass_each", "mass_total", "density", "value", "group", "visualize"])
        for name in ("mass_each", "mass_total", "density"):
            template = node.parm(name).parmTemplate()
            self.assertTrue(template.minIsStrict() and not template.maxIsStrict(), name)  # no upper limit
            self.assertEqual(template.minValue(), 0.0)
            self.assertEqual(node.parm(name).eval(), 1.0)
        self.assertTrue(node.parm("value").parmTemplate().isHidden())
        self.assertEqual(node.parm("value").eval(), -1.0)
        self.assertEqual(node.parm("mode").parmTemplate().menuItems()[:3], ("each", "total", "density"))
        self.assertEqual(node.parm("mode").eval(), "total")


def _set_a3ob_mass(geo, value: float) -> None:
    geo.addAttrib(hou.attribType.Point, "a3ob_mass", 0.0)
    geo.setPointFloatAttribValues("a3ob_mass", [value] * len(geo.points()))


if __name__ == "__main__":
    unittest.main()

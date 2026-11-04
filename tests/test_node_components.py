"""The Components node (task #47): the hull maths under plain Python, the node and its export in hython.

Purpose: catch a wrong group count or order, old components not removed, a hull that is not convex or not closed, a
proxy touched, and a result the exporter or Validate does not take as components.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import random
import unittest

from a3ob_core import p3d
from a3ob_houdini import hull_math
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_mass_math import CUBE_FACES, cube_points

if HAS_HOU:
    from a3ob_houdini import messages, p3d_export
    from a3ob_houdini.nodes import components as components_node
    from a3ob_houdini.p3d_export import ExportOptions

PATH = "\\a3\\data_f\\proxies\\weapon_slots\\top"


def volume(points, triangles):
    total = 0.0
    for a, b, c in triangles:
        p, q, r = points[a], points[b], points[c]
        cross = (q[1] * r[2] - q[2] * r[1], q[2] * r[0] - q[0] * r[2], q[0] * r[1] - q[1] * r[0])
        total += (p[0] * cross[0] + p[1] * cross[1] + p[2] * cross[2]) / 6.0
    return total


class TestHullMath(unittest.TestCase):
    def test_a_cube_with_an_inner_point_gives_the_cube(self):
        points = cube_points(2.0) + [(1.0, 1.0, 1.0)]
        triangles = hull_math.convex_hull(points)
        self.assertEqual(len(triangles), 12)
        self.assertAlmostEqual(volume(points, triangles), 8.0)  # positive: wound outwards
        self.assertNotIn(8, {i for triangle in triangles for i in triangle})

    def test_random_points_give_a_closed_outward_hull(self):
        rng = random.Random(4)
        points = [(rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(60)]
        triangles = hull_math.convex_hull(points)
        edges = {}
        for a, b, c in triangles:
            for edge in ((a, b), (b, c), (c, a)):
                edges[edge] = edges.get(edge, 0) + 1
        self.assertTrue(all(count == 1 and edges.get((e[1], e[0])) == 1 for e, count in edges.items()))
        self.assertGreater(volume(points, triangles), 0.0)

    def test_flat_or_small_input_has_no_hull(self):
        self.assertIsNone(hull_math.convex_hull([(0, 0, 0), (1, 0, 0), (0, 1, 0)]))
        self.assertIsNone(hull_math.convex_hull([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)]))


def two_boxes_and_a_quad():
    """Box at the origin, box at x=5, and an open quad at x=10. Point group component07 on the first box."""
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_resolution", 0.0)
    for offset in (0.0, 5.0):
        first = len(geo.points())
        geo.createPoints(cube_points(1.0, (offset, 0.0, 0.0)))
        geo.createPolygons([tuple(first + n for n in face) for face in CUBE_FACES])
    first = len(geo.points())
    geo.createPoints([(10, 0, 0), (11, 0, 0), (11, 1, 0), (10, 1, 0)])
    geo.createPolygons([(first, first + 1, first + 2, first + 3)])
    group = geo.createPointGroup("component07")
    group.add(geo.points()[:8])
    geo.createPointGroup("head").add(geo.points()[:2])
    return geo


def dented_box():
    """A closed box whose corner 7 is pushed inside: one piece that is not convex."""
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_resolution", 0.0)
    points = cube_points(2.0)
    points[7] = (0.6, 0.6, 0.6)
    geo.createPoints(points)
    geo.createPolygons(CUBE_FACES)
    geo.createPointGroup("dent").add(geo.points()[6:8])
    geo.createPointGroup("corner").add(geo.points()[:1])
    return geo


def group_names(geo):
    return sorted(group.name() for group in geo.pointGroups())


def group_size(geo, name):
    return len(geo.findPointGroup(name).points())


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestComponentsNode(NodeTestCase):
    def run_node(self, geo, with_proxy=False, **parms):
        source = make_source(self.network, geo)
        if with_proxy:
            proxy = self.network.createNode("a3ob_proxy")
            proxy.setInput(0, source)
            proxy.parm("path").set(PATH)
            source = proxy
        node = self.network.createNode("a3ob_components")
        node.setInput(0, source)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node, cook(node)

    def test_find_makes_a_group_per_closed_piece_and_counts_the_open_one(self):
        node, result = self.run_node(two_boxes_and_a_quad())
        geo = node.geometry()
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.messages, ("2 components (Component01-Component02). 1 open piece skipped.",))
        self.assertEqual(group_names(geo), ["Component01", "Component02", "head"])  # component07 is gone
        self.assertEqual(group_size(geo, "Component01"), 8)
        self.assertEqual(group_size(geo, "Component02"), 8)
        self.assertEqual(len(geo.prims()), 13)  # the geometry itself is not changed

    def test_keep_leaves_old_components_and_numbers_after_them(self):
        node, result = self.run_node(two_boxes_and_a_quad(), keep=1)
        self.assertEqual(group_names(node.geometry()), ["Component08", "Component09", "component07", "head"])
        self.assertEqual(result.messages, ("2 components (Component08-Component09). 1 open piece skipped.",))

    def test_nothing_found_warns(self):
        geo = hou.Geometry()
        geo.createPoints([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
        geo.createPolygons([(0, 1, 2)])
        _, result = self.run_node(geo)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("no component was made", result.warnings[0])

    def test_hull_makes_a_convex_closed_piece_with_its_group(self):
        node, result = self.run_node(dented_box(), mode="hull")
        geo = node.geometry()
        self.assertEqual(result.messages, ("1 component hulled (Component01).",))
        self.assertEqual(len(geo.points()), 7)  # the dent point is inside the hull and goes
        self.assertEqual(len(geo.prims()), 10)  # triangles: 2 * 7 - 4
        self.assertTrue(all(len(prim.vertices()) == 3 for prim in geo.prims()))
        self.assertEqual(group_size(geo, "Component01"), 7)
        self.assertEqual(group_size(geo, "corner"), 1)  # point groups of the kept corners stay
        self.assertEqual(group_size(geo, "dent"), 1)
        report = messages.Report()
        p3d_export.run([geo], ExportOptions(), report)
        self.assertEqual([text for text in report.texts(messages.WARNING) if "T2-CONVEX" in text or "T2-COMP" in text], [])
        self.assertEqual([text for text in report.texts(messages.WARNING) if "T2-CONTIG" in text], [])

    def test_hull_leaves_small_pieces_and_proxies(self):
        geo = two_boxes_and_a_quad()
        small = len(geo.points())
        geo.createPoints([(20, 0, 0), (21, 0, 0), (21, 1, 0)])
        geo.createPolygons([(small, small + 1, small + 2)])
        node, result = self.run_node(geo, with_proxy=True, mode="hull")
        out = node.geometry()
        self.assertEqual(sum(isinstance(prim, hou.PackedPrim) for prim in out.prims()), 1)
        self.assertEqual(sum(1 for g in out.pointGroups() if g.name().startswith("Component")), 2)
        # two boxes become hulls (12 triangles each); the quad and the triangle are left as they are
        self.assertEqual(sum(isinstance(prim, hou.Polygon) for prim in out.prims()), 12 * 2 + 2)
        self.assertEqual(result.messages, ("2 components hulled (Component01-Component02). "
                                           "2 pieces with fewer than 4 points or no volume left as they are.",))

    def test_find_does_not_touch_a_proxy(self):
        node, _ = self.run_node(two_boxes_and_a_quad(), with_proxy=True)
        self.assertEqual(sum(isinstance(prim, hou.PackedPrim) for prim in node.geometry().prims()), 1)

    def test_lossless_export_writes_the_components_and_validate_and_auto_components_agree(self):
        node, _ = self.run_node(two_boxes_and_a_quad(), with_proxy=True)
        geo = node.geometry()
        report = messages.Report()
        data = p3d_export.build_bytes([geo], ExportOptions(), report)
        self.assertIsNotNone(data, report.texts(messages.ERROR))
        written = [n for n in p3d.read_p3d(data, strict=False).lods[0].selection_names() if n.startswith("Component")]
        self.assertEqual(written, ["Component01", "Component02"])
        self.assertEqual([t for t in report.texts(messages.WARNING) if "T2-COMP" in t], [])
        again = p3d_export.build_bytes([geo], ExportOptions(auto_components=True), messages.Report())
        names = p3d.read_p3d(again, strict=False).lods[0].selection_names()
        self.assertEqual([n for n in names if n.lower().startswith("component")], ["Component01", "Component02"])

    def test_is_component_name_follows_a3ob(self):
        for name in ("component01", "COMPONENT7", "Component12x"):
            self.assertTrue(components_node.is_component_name(name), name)
        for name in ("components", "mycomponent01", "component"):
            self.assertFalse(components_node.is_component_name(name), name)


if __name__ == "__main__":
    unittest.main()

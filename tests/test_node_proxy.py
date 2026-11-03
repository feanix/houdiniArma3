"""The Proxy node in hython: the packed proxy it makes, its placement, numbering, messages, and a comparison with the
proxy the P3D importer makes from a fixture (ux-tools.md 5.5, spec 13.3). Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from tests.hou_support import HAS_HOU, hou, imported_fixture, packed_prims, polygons
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_mass_math import CUBE_FACES, cube_points

PATH = "\\a3\\data_f\\proxies\\weapon_slots\\top"
TOLERANCE = 1e-5
#: A fixture proxy triangle is the canonical one only up to the float32 rounding of the file (about 2e-5).
EMBEDDED_TOLERANCE = 1e-4


def cube_geometry():
    geo = hou.Geometry()
    geo.createPoints(cube_points())
    geo.createPolygons(CUBE_FACES)
    return geo


def points_geometry(positions, orients=None):
    geo = hou.Geometry()
    geo.createPoints(positions)
    if orients is not None:
        geo.addAttrib(hou.attribType.Point, "orient", (0.0, 0.0, 0.0, 1.0))
        geo.setPointFloatAttribValues("orient", [c for q in orients for c in q])
    return geo


def matrix_rows(matrix):
    return [list(row) for row in matrix.asTupleOfTuples()]


def assert_matrix_close(case, actual, expected, tolerance=TOLERANCE):
    for a_row, e_row in zip(matrix_rows(actual), matrix_rows(expected)):
        for a, e in zip(a_row, e_row):
            case.assertAlmostEqual(a, e, delta=tolerance)


def attribs(geo):
    """``{class: {name: (type, size)}}`` of a geometry, to compare two geometries."""
    classes = {"point": geo.pointAttribs(), "vertex": geo.vertexAttribs(), "prim": geo.primAttribs(),
               "detail": geo.globalAttribs()}
    return {key: {a.name(): (a.dataType(), a.size(), a.isArrayType()) for a in found} for key, found in classes.items()}


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestProxyNode(NodeTestCase):
    def run_proxy(self, points=None, orients=None, **parms):
        node = self.network.createNode("a3ob_proxy")
        node.setInput(0, make_source(self.network, cube_geometry()))
        if points is not None:
            node.setInput(1, make_source(self.network, points_geometry(points, orients)))
        parms.setdefault("path", PATH)
        for name, value in parms.items():
            if isinstance(value, tuple):
                node.parmTuple(name).set(value)
            else:
                node.parm(name).set(value)
        return node, cook(node)

    def test_one_proxy_is_a_packed_prim_after_all_polygons_with_the_outer_attributes(self):
        node, result = self.run_proxy(index=7)
        geo = node.geometry()
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.messages, ("1 proxy added: %s 7." % PATH,))
        self.assertEqual(len(polygons(geo)), 6)
        proxies = packed_prims(geo)
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].number(), 6)  # after the 6 polygons
        self.assertEqual(proxies[0].attribValue("a3ob_proxy_path"), PATH)
        self.assertEqual(proxies[0].attribValue("a3ob_proxy_index"), 7)
        self.assertEqual(geo.findPrimAttrib("a3ob_proxy_path").dataType(), hou.attribData.String)
        self.assertEqual(geo.findPrimAttrib("a3ob_proxy_index").dataType(), hou.attribData.Int)
        # a new proxy has no source values, so no snap-guard arrays
        self.assertIsNone(geo.findPrimAttrib("a3ob_proxy_src_P"))
        self.assertIsNone(geo.findPrimAttrib("a3ob_proxy_src_N"))
        self.assertEqual(len(geo.points()), 8 + 1)  # the 8 cube points, then the pack point

    def test_the_embedded_triangle_has_the_attributes_of_spec_13_3(self):
        node, _ = self.run_proxy(index=7)
        proxy = packed_prims(node.geometry())[0]
        inner = proxy.getEmbeddedGeometry()
        self.assertEqual(len(inner.points()), 3)
        self.assertEqual(len(inner.prims()), 1)
        triangle = inner.prims()[0]
        self.assertTrue(triangle.isClosed())
        self.assertEqual([v.point().number() for v in triangle.vertices()], [0, 2, 1])
        self.assertEqual([tuple(p.position()) for p in inner.points()], [(0, 0, 0), (0, 2, 0), (0, 0, -1)])
        self.assertEqual(triangle.attribValue("a3ob_proxy_path"), PATH)
        self.assertEqual(triangle.attribValue("a3ob_proxy_index"), 7)
        self.assertEqual(triangle.attribValue("a3ob_face_flags"), 0)
        self.assertEqual(triangle.attribValue("a3ob_texture"), "")
        self.assertEqual(triangle.attribValue("a3ob_material"), "")
        self.assertEqual([p.attribValue("a3ob_vertex_flags") for p in inner.points()], [0, 0, 0])
        self.assertEqual([v.attribValue("N") for v in triangle.vertices()], [(-1.0, 0.0, 0.0)] * 3)
        self.assertEqual([v.attribValue("uv") for v in triangle.vertices()], [(0.0, 0.0, 0.0)] * 3)
        self.assertIsNone(inner.findGlobalAttrib("a3ob_lod_type"))
        self.assertEqual(inner.pointGroups(), ())
        assert_matrix_close(self, proxy.fullTransform(), hou.Matrix4(1))

    def test_path_slashes_and_ending_are_normalised(self):
        node, _ = self.run_proxy(path=" /a3/data_f/proxies/weapon_slots/top.P3D ")
        self.assertEqual(packed_prims(node.geometry())[0].attribValue("a3ob_proxy_path"), PATH)

    def test_translate_and_rotate_place_the_proxy_without_scale(self):
        node, _ = self.run_proxy(t=(1.0, 2.0, 3.0), r=(10.0, 20.0, 30.0))
        expected = hou.hmath.buildRotate((10.0, 20.0, 30.0), "xyz") * hou.hmath.buildTranslate((1.0, 2.0, 3.0))
        matrix = packed_prims(node.geometry())[0].fullTransform()
        assert_matrix_close(self, matrix, expected)
        self.assertAlmostEqual(matrix.extractRotationMatrix3().determinant(), 1.0, places=6)

    def test_points_make_one_proxy_each_numbered_from_index(self):
        node, result = self.run_proxy(points=[(1, 0, 0), (0, 2, 0), (0, 0, 3)], index=4)
        proxies = packed_prims(node.geometry())
        self.assertEqual([p.attribValue("a3ob_proxy_index") for p in proxies], [4, 5, 6])
        translations = [p.fullTransform().extractTranslates() for p in proxies]
        self.assertEqual([tuple(round(c, 6) for c in t) for t in translations],
                         [(1, 0, 0), (0, 2, 0), (0, 0, 3)])
        self.assertEqual(result.messages, ("3 proxies added: %s 4 to 6." % PATH,))
        self.assertEqual(result.warnings, ())

    def test_numbering_off_gives_all_the_same_index_and_warns_about_the_clash(self):
        node, result = self.run_proxy(points=[(1, 0, 0), (0, 2, 0)], index=2, autoindex=0)
        self.assertEqual([p.attribValue("a3ob_proxy_index") for p in packed_prims(node.geometry())], [2, 2])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("used more than once (index 2)", result.warnings[0])
        self.assertEqual(result.messages, ("2 proxies added: %s 2." % PATH,))

    def test_a_proxy_that_repeats_an_existing_path_and_index_warns(self):
        first, _ = self.run_proxy(index=3)
        second = self.network.createNode("a3ob_proxy")
        second.setInput(0, first)
        second.parm("path").set(PATH.replace("\\", "/") + ".p3d")
        second.parm("index").set(3)
        result = cook(second)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("used more than once (index 3)", result.warnings[0])
        self.assertEqual(len(packed_prims(second.geometry())), 2)
        second.parm("index").set(4)
        self.assertEqual(cook(second).warnings, ())

    def test_orient_turns_the_proxy_and_useorient_off_ignores_it(self):
        turn = hou.Quaternion()
        turn.setToAngleAxis(90.0, hou.Vector3(0, 1, 0))
        orient = tuple(turn)
        node, _ = self.run_proxy(points=[(1, 2, 3)], orients=[orient])
        expected = hou.hmath.buildRotate((0.0, 90.0, 0.0), "xyz") * hou.hmath.buildTranslate((1.0, 2.0, 3.0))
        assert_matrix_close(self, packed_prims(node.geometry())[0].fullTransform(), expected)
        off, _ = self.run_proxy(points=[(1, 2, 3)], orients=[orient], useorient=0)
        assert_matrix_close(self, packed_prims(off.geometry())[0].fullTransform(),
                            hou.hmath.buildTranslate((1.0, 2.0, 3.0)))

    def test_points_without_orient_get_no_rotation_and_translate_rotate_are_not_used(self):
        node, _ = self.run_proxy(points=[(1, 2, 3)], t=(9.0, 9.0, 9.0), r=(0.0, 45.0, 0.0))
        assert_matrix_close(self, packed_prims(node.geometry())[0].fullTransform(),
                            hou.hmath.buildTranslate((1.0, 2.0, 3.0)))

    def test_an_empty_points_input_warns_and_adds_nothing(self):
        node, result = self.run_proxy(points=[])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("second input has no points", result.warnings[0])
        self.assertEqual(packed_prims(node.geometry()), [])

    def test_errors(self):
        _, result = self.run_proxy(path="  ")
        self.assertEqual(len(result.errors), 1)
        self.assertIn("the Path is empty", result.errors[0])
        _, result = self.run_proxy(path="\\a3\\日本")
        self.assertIn("cannot be written to a P3D file", result.errors[0])
        _, result = self.run_proxy(points=[(0, 0, 0)] * 3, index=998)
        self.assertIn("would get index 1000", result.errors[0])
        lone = self.network.createNode("a3ob_proxy")
        lone.parm("path").set(PATH)
        self.assertEqual(len(cook(lone).errors), 1)

    def test_index_is_clamped_to_0_999(self):
        node = self.network.createNode("a3ob_proxy")
        node.parm("index").set(5000)
        self.assertEqual(node.parm("index").eval(), 999)
        node.parm("index").set(-3)
        self.assertEqual(node.parm("index").eval(), 0)

    def test_the_common_proxies_menu_fills_the_path_and_resets(self):
        import a3ob_houdini.entry as entry
        node = self.network.createNode("a3ob_proxy")
        node.parm("common").set("weapon_optic")
        entry.button("proxy", "fillcommon", {"node": node, "parm": node.parm("common")})
        self.assertEqual(node.parm("path").eval(), PATH)
        self.assertEqual(node.parm("common").eval(), "none")

    def test_the_asset_parameters_match_the_table(self):
        node = self.network.createNode("a3ob_proxy")
        names = [parm.name() for parm in node.parms()]
        self.assertEqual(names, ["path", "common", "index", "autoindex", "tx", "ty", "tz", "rx", "ry", "rz",
                                 "useorient"])
        self.assertEqual(node.type().minNumInputs(), 1)
        self.assertEqual(node.type().maxNumInputs(), 2)

    def test_detail_attributes_and_existing_prims_pass_through(self):
        geo = cube_geometry()
        geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
        node = self.network.createNode("a3ob_proxy")
        node.setInput(0, make_source(self.network, geo))
        node.parm("path").set(PATH)
        cook(node)
        self.assertEqual(node.geometry().intAttribValue("a3ob_lod_type"), 6)
        self.assertEqual(len(polygons(node.geometry())), 6)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestProxyMatchesImporter(NodeTestCase):
    """A proxy made by the node and the importer's proxy of a fixture are interchangeable (same attributes, same
    transform within float32 tolerance), so the M4 exporter treats them alike."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, geo, _, _ = imported_fixture("sample_1_character.p3d")
        cls.lod = geo.prims()[0].getEmbeddedGeometry()
        cls.fixture_proxies = {}
        for prim in packed_prims(cls.lod):
            cls.fixture_proxies.setdefault(prim.attribValue("a3ob_proxy_path"), prim)

    def make_like(self, fixture):
        """Run the Proxy node so that it places a proxy where ``fixture`` is. Returns the new packed prim."""
        matrix = fixture.fullTransform()
        rotation = hou.Quaternion()
        rotation.setToRotationMatrix(matrix.extractRotationMatrix3())
        position = tuple(matrix.extractTranslates())
        node = self.network.createNode("a3ob_proxy")
        node.setInput(0, make_source(self.network, cube_geometry()))
        node.setInput(1, make_source(self.network, points_geometry([position], [tuple(rotation)])))
        node.parm("path").set(fixture.attribValue("a3ob_proxy_path").replace("\\", "/") + ".p3d")
        node.parm("index").set(fixture.attribValue("a3ob_proxy_index"))
        result = cook(node)
        self.assertEqual((result.errors, result.warnings), ((), ()))
        return packed_prims(node.geometry())[0]

    def compare(self, made, fixture):
        assert_matrix_close(self, made.fullTransform(), fixture.fullTransform())
        self.assertEqual(made.attribValue("a3ob_proxy_path"), fixture.attribValue("a3ob_proxy_path"))
        self.assertEqual(made.attribValue("a3ob_proxy_index"), fixture.attribValue("a3ob_proxy_index"))
        mine, theirs = made.getEmbeddedGeometry(), fixture.getEmbeddedGeometry()
        expected = attribs(theirs)
        self.assertEqual(attribs(mine), expected)
        self.assertEqual(len(mine.points()), len(theirs.points()))
        for a, b in zip(mine.points(), theirs.points()):
            for x, y in zip(a.position(), b.position()):
                self.assertAlmostEqual(x, y, delta=EMBEDDED_TOLERANCE)
            self.assertEqual(a.attribValue("a3ob_vertex_flags"), b.attribValue("a3ob_vertex_flags"))
        a_prim, b_prim = mine.prims()[0], theirs.prims()[0]
        self.assertEqual([v.point().number() for v in a_prim.vertices()], [v.point().number() for v in b_prim.vertices()])
        for a, b in zip(a_prim.vertices(), b_prim.vertices()):
            for name in ("N", "uv"):
                for x, y in zip(a.attribValue(name), b.attribValue(name)):
                    self.assertAlmostEqual(x, y, delta=EMBEDDED_TOLERANCE)
        for name in ("a3ob_face_flags", "a3ob_texture", "a3ob_material", "a3ob_proxy_path", "a3ob_proxy_index"):
            self.assertEqual(a_prim.attribValue(name), b_prim.attribValue(name), name)
        self.assertEqual(mine.findVertexAttrib("uv").options(), theirs.findVertexAttrib("uv").options())

    def test_a_proxy_in_the_canonical_position(self):
        fixture = self.fixture_proxies["\\a3\\characters_f\\proxies\\backpack"]
        self.compare(self.make_like(fixture), fixture)

    def test_a_proxy_that_is_moved_and_turned(self):
        fixture = self.fixture_proxies["\\a3\\characters_f\\proxies\\binoculars"]
        self.assertGreater(abs(fixture.fullTransform().extractTranslates()[2]), 0.01)  # it really is moved
        self.compare(self.make_like(fixture), fixture)

    def test_every_fixture_proxy_matches_in_transform_and_attributes(self):
        """The importer keeps the file's vertex order, so a few fixture triangles list their corners in another
        order than the node's C, B, A. Those still match in transform and attribute scheme; the point values are
        compared only where the order is the same."""
        same_order = 0
        for path, fixture in self.fixture_proxies.items():
            made = self.make_like(fixture)
            if self.has_canonical_points(fixture):
                self.compare(made, fixture)
                same_order += 1
            else:
                assert_matrix_close(self, made.fullTransform(), fixture.fullTransform())
                self.assertEqual(attribs(made.getEmbeddedGeometry()), attribs(fixture.getEmbeddedGeometry()))
        self.assertGreaterEqual(same_order, 1)
        print("\n%d of %d fixture proxies have the canonical corner order" % (same_order, len(self.fixture_proxies)))

    @staticmethod
    def has_canonical_points(fixture):
        found = [tuple(p.position()) for p in fixture.getEmbeddedGeometry().points()]
        canonical = [(0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, -1.0)]
        return all(abs(a - b) < EMBEDDED_TOLERANCE for f, c in zip(found, canonical) for a, b in zip(f, c))


if __name__ == "__main__":
    unittest.main()

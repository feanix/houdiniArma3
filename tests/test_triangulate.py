"""Task #49 (D28): the export triangulates the polygons that break the rules (more than 4 points anywhere, non-convex quads
in Geometry and Shadow Volume LODs, every quad in Underground LODs). The decision tests run under CPython; the export
tests need hython and skip otherwise.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import math
import unittest

from a3ob_core import p3d
from a3ob_core.p3d.lod_types import LodType
from a3ob_houdini import messages, settings as sm
from a3ob_houdini.nodes import _export_common as common
from a3ob_houdini.p3d_export import ExportOptions, triangulate
from a3ob_houdini.p3d_export.data import Details, LodData, MeshData, Poly
from tests import hou_support as hs
from tests.mass_proxy_support import NodeTestCase, cook, make_source

if hs.HAS_HOU:
    import hou
    from a3ob_houdini import p3d_export

TEXTURE = "a3\\tex_co.paa"


def plain_lod(lod_type, polys):
    """A ``LodData`` whose polygons are the given corner lists, on a flat grid of points (z = 0 except where given)."""
    points = [(float(k % 8), float(k // 8), 0.0) for k in range(32)]
    points[10] = (0.5, 0.5, 0.0)  # the reflex corner of DENT_QUAD below
    mesh = MeshData(points=points, polys=[Poly(n, tuple(p)) for n, p in enumerate(polys)])
    return LodData(0, mesh, Details(lod_type=int(lod_type), resolution=1.0))


class TestDecision(unittest.TestCase):
    CONVEX_QUAD = (0, 1, 9, 8)
    DENT_QUAD = (0, 2, 10, 16)  # (0,0) (2,0) (0.5,0.5) (0,2): an arrow head, not convex
    TRI = (0, 1, 9)
    LONG = tuple(range(5))

    def decide(self, lod_type, polys, **options):
        return triangulate.polygons_to_triangulate(plain_lod(lod_type, polys), ExportOptions(**options))

    def test_more_than_four_points_in_every_lod_type(self):
        for lod_type in (LodType.VISUAL if hasattr(LodType, "VISUAL") else 0, LodType.GEOMETRY, LodType.SHADOW,
                         LodType.UNDERGROUND, LodType.MEMORY):
            found = self.decide(lod_type, [self.TRI, self.LONG])
            self.assertEqual(found, {triangulate.REASON_LONG: [1]}, lod_type)

    def test_valid_polygons_are_left_alone(self):
        for lod_type in (LodType.GEOMETRY, LodType.SHADOW):
            self.assertEqual(self.decide(lod_type, [self.TRI, self.CONVEX_QUAD]), {})
        self.assertEqual(self.decide(0, [self.TRI, self.CONVEX_QUAD, self.DENT_QUAD]), {})  # T2-TRI is not a visual rule
        self.assertEqual(self.decide(LodType.UNDERGROUND, [self.TRI]), {})

    def test_nonconvex_quad_in_geometry_and_shadow(self):
        for lod_type in (LodType.GEOMETRY, LodType.SHADOW):
            self.assertEqual(self.decide(lod_type, [self.CONVEX_QUAD, self.DENT_QUAD]), {triangulate.REASON_QUAD: [1]})

    def test_every_quad_in_underground(self):
        found = self.decide(LodType.UNDERGROUND, [self.TRI, self.CONVEX_QUAD, self.DENT_QUAD])
        self.assertEqual(found, {triangulate.REASON_QUAD: [1, 2]})

    def test_quads_stay_when_the_tri_rule_is_off(self):
        found = self.decide(LodType.UNDERGROUND, [self.CONVEX_QUAD, self.LONG], rules_off=("T2-TRI",))
        self.assertEqual(found, {triangulate.REASON_LONG: [1]})

    def test_the_decision_matches_the_check(self):
        """Every polygon the check calls bad is picked, and nothing else (D24 T2-TRI)."""
        from a3ob_houdini.p3d_export import type_rules
        for lod_type in (LodType.GEOMETRY, LodType.SHADOW, LodType.UNDERGROUND):
            data = plain_lod(lod_type, [self.TRI, self.CONVEX_QUAD, self.DENT_QUAD])
            picked = {n for numbers in triangulate.polygons_to_triangulate(data, ExportOptions()).values() for n in numbers}
            warned = type_rules.rule_by_id("T2-TRI").check(type_rules._Context(data, ExportOptions())) is not None
            self.assertEqual(bool(picked), warned, lod_type)

    def test_warning_text(self):
        text = triangulate.triangulated_warning("Geometry", {triangulate.REASON_LONG: list(range(8))}, int(LodType.GEOMETRY))
        self.assertEqual(text, "Geometry: 8 polygons triangulated (more than 4 points: 8). Add a Divide on this LOD's "
                               "branch to control the result yourself.")


class TestOptions(unittest.TestCase):
    def test_default_on_in_options_presets_and_the_parameter(self):
        self.assertTrue(ExportOptions().triangulate)
        self.assertTrue(sm.BUILTIN_PRESETS[sm.LOSSLESS]["triangulate"])
        self.assertTrue(sm.BUILTIN_PRESETS[sm.RELEASE]["triangulate"])
        self.assertTrue(common.options_from({}).triangulate)
        self.assertFalse(common.options_from({"triangulate": False}).triangulate)
        parm = {p.name: p for p in common.rule_parms()}["triangulate"]
        self.assertTrue(parm.default)
        self.assertIn("Divide", parm.help)

    def test_an_old_preset_without_the_key_reads_it_as_on(self):
        settings = sm.Settings(presets={"old": {"lowercase": True}})
        self.assertTrue(sm.options_for(settings, "old").triangulate)
        settings.presets["off"] = {"triangulate": False}
        self.assertFalse(sm.options_for(settings, "off").triangulate)


# -- Houdini ---------------------------------------------------------------------------------------------------------

def build_geo(lod_type, shapes):
    """A LOD geometry. ``shapes`` is a list of ``(point positions, group name)``; every shape is one polygon with a
    texture, a UV per corner, a point group and a prim group of the shape's group name."""
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_type", 0)
    geo.addAttrib(hou.attribType.Global, "a3ob_lod_resolution", 1.0)
    geo.setGlobalAttribValue("a3ob_lod_type", int(lod_type))
    geo.setGlobalAttribValue("a3ob_lod_resolution", 1000.0 if lod_type == LodType.SHADOW else 0.0)
    geo.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
    geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
    for positions, name in shapes:
        points = [geo.createPoint() for _ in positions]
        for point, position in zip(points, positions):
            point.setPosition(position)
        prim = geo.createPolygon()
        for point in points:
            prim.addVertex(point)
        prim.setAttribValue("a3ob_texture", TEXTURE)
        for k, vertex in enumerate(prim.vertices()):
            vertex.setAttribValue("uv", (positions[k][0] * 0.1, positions[k][1] * 0.1, 0.0))
        if name:
            group = geo.findPointGroup(name) or geo.createPointGroup(name)
            for point in points:
                group.add(point)
            (geo.findPrimGroup(name) or geo.createPrimGroup(name)).add(prim)
    return geo


def circle(count, x=0.0):
    return [(x + math.cos(2 * math.pi * k / count), math.sin(2 * math.pi * k / count), 0.0) for k in range(count)]


QUAD = [(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 1.0, 0.0), (10.0, 1.0, 0.0)]
DENT_QUAD = [(20.0, 0.0, 0.0), (22.0, 0.0, 0.0), (20.5, 0.5, 0.0), (20.0, 2.0, 0.0)]  # corner 3 -> reflex at point 2
TRI = [(30.0, 0.0, 0.0), (31.0, 0.0, 0.0), (30.0, 1.0, 0.0)]


def faces_of(data):
    model = p3d.read_p3d(data, strict=False)
    return model.lods[0]


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestExportTriangulates(unittest.TestCase):
    def run_export(self, geo, **options):
        report = messages.Report()
        data = p3d_export.build_bytes([geo], ExportOptions(**options), report)
        return data, report

    def test_a_16_point_polygon_in_geometry(self):
        geo = build_geo(LodType.GEOMETRY, [(circle(16), "sel"), (QUAD, "sel2"), (TRI, "")])
        before = len(geo.prims())
        data, report = self.run_export(geo)
        self.assertIsNotNone(data, report.texts(messages.ERROR))
        self.assertEqual(report.texts(messages.ERROR), [])
        lod = faces_of(data)
        self.assertEqual(lod.face_count, 14 + 1 + 1)
        self.assertTrue(all(len(face.vertices) in (3, 4) for face in lod.faces()))
        warnings = [t for t in report.texts(messages.WARNING) if "triangulated" in t]
        self.assertEqual(warnings, ["Geometry: 1 polygons triangulated (more than 4 points: 1). Add a Divide on this "
                                    "LOD's branch to control the result yourself."])
        self.assertFalse([t for t in report.texts(messages.WARNING) if "T2-TRI" in t])
        self.assertEqual(len(geo.prims()), before)  # the geometry of the node is not changed

    def test_groups_uvs_and_textures_survive(self):
        geo = build_geo(LodType.GEOMETRY, [(circle(16), "sel"), (QUAD, "sel2")])
        data, report = self.run_export(geo)
        lod = faces_of(data)
        self.assertEqual(sorted(lod.selection_names()), ["sel", "sel2"])
        self.assertEqual(len(lod.selection("sel").members()), 16)
        self.assertEqual(len(lod.selection("sel").face_members()), 14)  # every new triangle is in the face group
        self.assertEqual(len(lod.selection("sel2").face_members()), 1)
        self.assertEqual({face.texture for face in lod.faces()}, {TEXTURE})
        self.assertTrue(all(any(abs(u) > 1e-6 or abs(v - 1.0) > 1e-6 for u, v in face.uvs) for face in lod.faces()))

    def test_nonconvex_quad_in_geometry_and_quad_in_underground(self):
        geo = build_geo(LodType.GEOMETRY, [(QUAD, "a"), (DENT_QUAD, "b")])
        data, report = self.run_export(geo)
        self.assertEqual(faces_of(data).face_count, 1 + 2)
        self.assertIn("Geometry: 1 polygons triangulated (quads that are not convex: 1). Add a Divide on this LOD's branch "
                      "to control the result yourself.", report.texts(messages.WARNING))
        self.assertFalse([t for t in report.texts(messages.WARNING) if "T2-TRI" in t])
        under = build_geo(LodType.UNDERGROUND, [(QUAD, "a"), (TRI, "")])
        data, report = self.run_export(under)
        self.assertEqual(faces_of(data).face_count, 2 + 1)
        self.assertIn("Underground (VBS): 1 polygons triangulated (quads in an Underground LOD: 1). Add a Divide on this LOD's "
                      "branch to control the result yourself.", report.texts(messages.WARNING))
        self.assertFalse([t for t in report.texts(messages.WARNING) if "T2-TRI" in t])

    def test_off_gives_todays_errors_and_warnings(self):
        geo = build_geo(LodType.GEOMETRY, [(circle(16), "sel")])
        data, report = self.run_export(geo, triangulate=False)
        self.assertIsNone(data)
        self.assertTrue([t for t in report.texts(messages.ERROR) if "1 polygons have more than 4 points" in t])
        self.assertFalse([t for t in report.texts(messages.WARNING) if "polygons triangulated" in t])
        self.assertTrue([t for t in report.texts(messages.WARNING) if "T2-TRI" in t])  # the check warns as it did before
        under = build_geo(LodType.UNDERGROUND, [(QUAD, "a")])
        data, report = self.run_export(under, triangulate=False)
        self.assertIsNotNone(data)
        self.assertTrue([t for t in report.texts(messages.WARNING) if "T2-TRI" in t])

    def test_a_valid_lod_is_byte_identical(self):
        geo = build_geo(LodType.GEOMETRY, [(QUAD, "a"), (TRI, "b")])
        on, report_on = self.run_export(geo, triangulate=True)
        off, report_off = self.run_export(geo, triangulate=False)
        self.assertIsNotNone(on)
        self.assertEqual(on, off)
        self.assertEqual(report_on.items, report_off.items)

    def test_the_shadow_lod_is_sharp_after_dividing(self):
        geo = build_geo(LodType.SHADOW, [(circle(8), "a")])
        data, report = self.run_export(geo)
        lod = faces_of(data)
        self.assertEqual(lod.face_count, 6)
        sharp = [tagg for tagg in lod.taggs if isinstance(tagg, p3d.SharpEdgesTagg)][0]
        self.assertEqual(len(sharp.pairs()), 5)  # the 5 new diagonals (the outline edges have one face, so no pair)


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestValidateAgrees(NodeTestCase):
    def messages_of(self, kind, source, **parms):
        node = self.network.createNode(kind)
        node.setFirstInput(source)
        for name, value in parms.items():
            node.parm(name).set(value)
        return [m for m in cook(node).messages + cook(node).warnings if "triangulat" in m or "T2-TRI" in m
                or "more than 4" in m]

    def test_validate_and_export_give_the_same_text_with_the_toggle_on_and_off(self):
        geo = build_geo(LodType.GEOMETRY, [(circle(16), "sel"), (DENT_QUAD, "b")])
        source = make_source(self.network, geo)
        for value in (1, 0):
            from_validate = self.messages_of("a3ob_validate", source, triangulate=value)
            from_export = self.messages_of("a3ob_p3d_export", source, triangulate=value)
            strip = lambda items: sorted(text.split(": ", 1)[1] for text in items)  # drop the node path
            self.assertEqual(strip(from_validate), strip(from_export))
            self.assertTrue(from_validate)
        on = self.messages_of("a3ob_validate", source, triangulate=1)
        self.assertTrue([t for t in on if "2 polygons triangulated (more than 4 points: 1, quads that are not convex: 1)" in t], on)
        off = self.messages_of("a3ob_validate", source, triangulate=0)
        self.assertFalse([t for t in off if "polygons triangulated" in t])


if __name__ == "__main__":
    unittest.main()

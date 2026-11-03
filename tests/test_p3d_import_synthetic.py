"""P3D Import on small hand-made models: the paths the two fixtures do not reach.

The fixtures have no editor TAGGs, no blobs, no inline proxies and no unusual active bytes (spec 11.2, "Fixtures"),
so these models cover them: blobs and the TAGG order hint, active bytes, weights, face groups, sharp edge extras,
property tails, trailing bytes, inline and out-of-order proxies, face UVs, odd names and strings. Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import base64
import unittest

from a3ob_core import binary, p3d
from tests import hou_support
from tests.hou_support import HAS_HOU, packed_prims, polygons

if HAS_HOU:
    from a3ob_houdini import names

POINTS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
QUADS = [(0, 1, 2, 3), (1, 4, 5, 2)]


def make_lod(lod_type=p3d.LodType.VISUAL, resolution=1.0):
    """Two quads sharing an edge, with flags, strings and two normals."""
    lod = p3d.LOD(lod_type, resolution)
    lod.set_mesh(
        points=POINTS, polygons=QUADS, normals=[(0, 0, -1), (0, 1, 0)], normal_indices=[0] * 4 + [1] * 4,
        uvs=[0, 0, 1, 0, 1, 1, 0, 1, 1, 0, 2, 0, 2, 1, 1, 1], point_flags=[0, 1, 2, 3, 4, 0xFE000000],
        face_flags=[0x100, 0xFE000000], textures=["a\\b.paa", "b.paa"], materials=["m.rvmat", ""])
    return lod


def model_of(*lods):
    model = p3d.P3DModel(lods)
    return model


def inner_of(geo, index=0):
    return geo.prims()[index].getEmbeddedGeometry()


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestBlobsAndHints(unittest.TestCase):
    def setUp(self):
        lod = make_lod()
        lod.flags = 0x80000001
        lod.eof_active = 0
        lod.set_sharp_edges([(0, 1), (0, 2), (3, 3), (1, 0)])
        lod.insert_tagg(p3d.UVSetTagg(0, lod.corner_uvs()))
        lod.insert_tagg(p3d.UVSetTagg(0, [0.5] * 16))            # repeated id -> blob
        lod.insert_tagg(p3d.UVSetTagg(2, [0.25] * 16, active=3))  # live, active byte 3
        tail = bytearray(b"key\x00" + b"\x7f" * 60 + b"val\x00" + b"\x7f" * 60)
        lod.insert_tagg(p3d.PropertyTagg.from_payload(bytes(tail)))
        lod.insert_tagg(p3d.MassTagg([1, 2, 3, 4, 5, 6], active=0))  # inactive known TAGG -> blob
        lod.taggs.append(p3d.SelectionTagg("weighted", [1, 255, 2, 0, 0, 1], [0, 2]))
        lod.taggs.append(p3d.SelectionTagg("", bytes(6), bytes(2)))
        lod.taggs.append(p3d.SelectionTagg("twice", [1, 1, 0, 0, 0, 0], [0, 0]))
        lod.taggs.append(p3d.SelectionTagg("twice", [0, 0, 1, 1, 0, 0], [0, 0]))
        lod.taggs.append(p3d.SelectionTagg("Head", [1, 0, 0, 0, 0, 0], [0, 0]))
        lod.taggs.append(p3d.SelectionTagg("head", [0, 1, 0, 0, 0, 0], [0, 0]))
        lod.taggs.append(p3d.MaskTagg("#Selected#", [0, 1, 0, 0, 0, 1], [1, 0], active=1))
        lod.taggs.append(p3d.AnimationTagg(0.5, [0.0] * 18, active=1))
        lod.taggs.append(p3d.AnimationTagg(1.5, [1.0] * 18, active=2))
        lod.taggs.append(p3d.RawTagg("#Unknown#", b"\x00\x01\xff", active=7))
        self.lod = lod
        self.tail_payload = bytes(tail)
        self.assertEqual(lod.structure_errors(), [])
        model = model_of(lod)
        model.trailing = b"\x01\x02\xfe"
        self.geo, self.report, _ = hou_support.import_model(model)
        self.inner = inner_of(self.geo)

    def test_order_hint_and_blob_tokens(self):
        self.assertEqual(list(self.inner.attribValue("a3ob_tagg_order")), [
            "#SharpEdges#", "#UVSet#0", "#?0", "#UVSet#2", "#Property#", "#?1", "weighted", "#?2", "twice", "#?3",
            "Head", "head", "#?4", "#?5", "#?6", "#?7"])

    def test_blob_arrays_keep_name_active_byte_and_payload(self):
        inner = self.inner
        self.assertEqual(list(inner.attribValue("a3ob_tagg_unknown_names")), [
            "#UVSet#", "#Mass#", "", "twice", "#Selected#", "#Animation#", "#Animation#", "#Unknown#"])
        self.assertEqual(list(inner.attribValue("a3ob_tagg_unknown_active")), [1, 0, 1, 1, 1, 1, 2, 7])
        payloads = [base64.b64decode(text) for text in inner.attribValue("a3ob_tagg_unknown_data")]
        by_name = {}
        for name, payload in zip(inner.attribValue("a3ob_tagg_unknown_names"), payloads):
            by_name.setdefault(name, []).append(payload)
        self.assertEqual(by_name["#Unknown#"], [b"\x00\x01\xff"])
        self.assertEqual(by_name["#Selected#"], [bytes([0, 1, 0, 0, 0, 1, 1, 0])])
        self.assertEqual(len(by_name["#Animation#"]), 2)
        self.assertNotEqual(by_name["#Animation#"][0], by_name["#Animation#"][1])  # one entry per frame, in order
        self.assertEqual(by_name["#Mass#"], [binary.array_to_bytes(p3d.MassTagg([1, 2, 3, 4, 5, 6]).masses)])

    def test_active_bytes_of_live_taggs(self):
        self.assertEqual(list(self.inner.attribValue("a3ob_tagg_active")),
                         [1, 1, 1, 3, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 7])
        self.assertEqual(self.inner.attribValue("a3ob_eof_active"), 0)
        self.assertEqual(self.inner.attribValue("a3ob_lod_flags"), u32_to_i32(0x80000001))

    def test_mass_with_active_zero_is_not_a_live_attribute(self):
        self.assertIsNone(self.inner.findPointAttrib("a3ob_mass"))

    def test_property_tail_is_kept(self):
        inner = self.inner
        self.assertEqual(list(inner.attribValue("a3ob_prop_names")), ["key"])
        self.assertEqual(list(inner.attribValue("a3ob_prop_values")), ["val"])
        self.assertEqual(base64.b64decode(inner.attribValue("a3ob_prop_raw")[0]), self.tail_payload)

    def test_sharp_edges_and_extras(self):
        inner = self.inner
        group = inner.findEdgeGroup("a3ob_sharp")
        self.assertEqual([tuple(p.number() for p in e.points()) for e in group.edges()], [(0, 1)])
        # the diagonal is not an edge, (3, 3) is degenerate, (1, 0) is the edge (0, 1) again: all kept as extras
        self.assertEqual(list(inner.attribValue("a3ob_sharp_extra")), [0, 2, 3, 3, 1, 0])

    def test_weights_and_face_groups(self):
        inner = self.inner
        weights = [inner.points()[i].attribValue("a3ob_w_weighted") for i in range(6)]
        self.assertEqual(weights, [1.0, 0.0, p3d.f32(253 / 254), 1.0, 1.0, 1.0])  # byte 255 is weight 0.0
        self.assertEqual(inner.findPointAttrib("a3ob_w_weighted").defaultValue(), 1.0)
        self.assertEqual([p.number() for p in inner.findPointGroup("weighted").points()], [0, 1, 2, 5])
        self.assertEqual([p.number() for p in inner.findPrimGroup("weighted").prims()], [1])
        self.assertEqual([p.attribValue("a3ob_fw_weighted") for p in polygons(inner)], [1.0, p3d.f32(253 / 254)])

    def test_groups_for_blob_and_case_names(self):
        groups = {g.name() for g in self.inner.pointGroups()}
        self.assertEqual(groups, {"weighted", "twice", "Head", "head"})
        self.assertEqual([p.number() for p in self.inner.findPointGroup("twice").points()], [0, 1])  # the first one

    def test_warnings_name_the_lod(self):
        warnings = self.report.texts("warning")
        self.assertEqual(len(warnings), 4)
        text = "\n".join(warnings)
        for part in ("empty name", "'twice' appears more than once", "differ only in case", "UV set id 0"):
            self.assertIn(part, text)
        self.assertTrue(all(w.startswith("Resolution 1: ") for w in warnings))

    def test_trailing_bytes_are_stored_on_the_container(self):
        self.assertEqual(base64.b64decode(self.geo.attribValue("a3ob_file_trailing")), b"\x01\x02\xfe")

    def test_uv_sets_by_id(self):
        names_ = sorted(a.name() for a in self.inner.vertexAttribs() if a.name().startswith("uv"))
        self.assertEqual(names_, ["uv", "uv3"])
        vertex = polygons(self.inner)[0].vertices()[1]  # file corner 3 of the first quad
        self.assertEqual(tuple(vertex.attribValue("uv3")), (0.25, 0.75, 0.0))


def u32_to_i32(value):
    return value - 2 ** 32 if value >= 2 ** 31 else value


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestProxies(unittest.TestCase):
    def test_inline_proxy_keeps_group_and_marks_member_faces(self):
        lod = make_lod()
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\x.001", [1, 1, 1, 1, 1, 1], [0, 1]))
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\y.1", [1, 1, 0, 0, 0, 0], [0, 0]))
        geo, report, _ = hou_support.import_model(model_of(lod))
        inner = inner_of(geo)
        self.assertEqual(packed_prims(inner), [])
        self.assertEqual([p.attribValue("a3ob_proxy_path") for p in polygons(inner)], ["", "\\a3\\x"])
        self.assertEqual([p.attribValue("a3ob_proxy_index") for p in polygons(inner)], [0, 1])
        groups = {g.name() for g in inner.pointGroups()}
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(report.texts("warning")), 2)
        self.assertIn("inline triangle", report.texts("warning")[0])

    def test_proxy_that_is_not_last_is_still_packed_and_the_mesh_is_renumbered(self):
        lod = p3d.LOD(p3d.LodType.VISUAL, 1.0)
        points = [(5, 5, 5), (5, 6, 5), (5, 5, 7)] + POINTS
        lod.set_mesh(points=points, polygons=[(0, 1, 2)] + [tuple(v + 3 for v in q) for q in QUADS],
                     normals=[(0, 0, -1)], normal_indices=[0] * 11)
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\x.001", [1, 1, 1] + [0] * 6, [1, 0, 0]))
        lod.set_sharp_edges([(3, 4), (0, 1)])
        geo, report, _ = hou_support.import_model(model_of(lod))
        inner = inner_of(geo)
        self.assertEqual(len(packed_prims(inner)), 1)
        self.assertEqual(len(polygons(inner)), 2)
        self.assertEqual(len(inner.points()), 6 + 1)  # mesh points renumbered 0..5, then the pack point
        self.assertEqual(tuple(inner.points()[1].position()), (1, 0, 0))
        self.assertEqual([v.point().number() for v in polygons(inner)[0].vertices()], [0, 3, 2, 1])
        self.assertIn("not the last points and faces", "\n".join(report.texts("warning")))
        self.assertEqual(list(inner.attribValue("a3ob_sharp_extra")), [0, 1])  # touches a proxy point
        self.assertEqual(len(list(inner.findEdgeGroup("a3ob_sharp").edges())), 1)
        self.assertEqual(list(inner.attribValue("a3ob_source_counts")), [9, 3, 11])

    def test_degenerate_proxy_triangle_stays_inline(self):
        lod = p3d.LOD(p3d.LodType.VISUAL, 1.0)
        lod.set_mesh(points=[(0, 0, 0), (1, 0, 0), (2, 0, 0)], polygons=[(0, 1, 2)], normals=[(0, 1, 0)],
                     normal_indices=[0, 0, 0])
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\x.001", [1, 1, 1], [1]))
        geo, report, _ = hou_support.import_model(model_of(lod))
        self.assertEqual(packed_prims(inner_of(geo)), [])
        self.assertIn("degenerate", "\n".join(report.texts("warning")))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestCornerData(unittest.TestCase):
    def test_face_uv_is_kept_only_when_it_carries_data(self):
        lod = make_lod()  # no #UVSet#, face UVs are not all zero
        inner = inner_of(hou_support.import_model(model_of(lod))[0])
        self.assertIsNone(inner.findVertexAttrib("uv"))
        face_uv = [tuple(v.attribValue("a3ob_face_uv")) for v in polygons(inner)[0].vertices()]
        self.assertEqual(face_uv, [(0.0, 1.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)])
        lod.insert_tagg(p3d.UVSetTagg(0, lod.corner_uvs()))
        inner = inner_of(hou_support.import_model(model_of(lod))[0])
        self.assertIsNotNone(inner.findVertexAttrib("uv"))
        self.assertIsNone(inner.findVertexAttrib("a3ob_face_uv"))
        lod.taggs[-1].uvs[0] = 0.5
        inner = inner_of(hou_support.import_model(model_of(lod))[0])
        self.assertIsNotNone(inner.findVertexAttrib("a3ob_face_uv"))

    def test_uv_keeps_float64_precision(self):
        lod = make_lod()
        lod.insert_tagg(p3d.UVSetTagg(0, [0.1] * 16))
        inner = inner_of(hou_support.import_model(model_of(lod))[0])
        value = polygons(inner)[0].vertices()[0].attribValue("uv")
        self.assertEqual(value[1], 1.0 - p3d.f32(0.1))  # v' = 1 - v in double, not rounded to float32

    def test_faces_with_two_sides_become_polygons_with_two_vertices(self):
        lod = make_lod()
        lod.face_sides[1] = 2
        lod.taggs.append(p3d.SelectionTagg("s", [0, 1, 0, 0, 1, 0], [0, 0]))
        geo, _, _ = hou_support.import_model(model_of(lod))
        inner = inner_of(geo)
        self.assertEqual([len(p.vertices()) for p in polygons(inner)], [4, 2])
        self.assertEqual([v.point().number() for v in polygons(inner)[1].vertices()], [1, 4])
        self.assertEqual(list(inner.attribValue("a3ob_source_counts")), [6, 2, 6])

    def test_odd_strings_and_names_go_in_unchanged(self):
        text = binary.decode_string(bytes(range(1, 256)))
        odd_names = ["-proxies", "pos driver", "café €\x81", "xn__abc", "proxy:\\a b.c"]
        lod = make_lod()
        lod.face_textures[0] = text
        lod.face_materials[1] = text[::-1]
        for name in odd_names:
            lod.add_selection(name, {0: 1.0})
        inner = inner_of(hou_support.import_model(model_of(lod))[0])
        self.assertEqual(polygons(inner)[0].attribValue("a3ob_texture"), text)
        self.assertEqual(polygons(inner)[1].attribValue("a3ob_material"), text[::-1])
        self.assertEqual(sorted(names.decode_group_name(g.name()) for g in inner.pointGroups()), sorted(odd_names))

    def test_empty_lod_imports(self):
        lod = p3d.LOD(p3d.LodType.LANDCONTACT)
        geo, report, _ = hou_support.import_model(model_of(lod))
        inner = inner_of(geo)
        self.assertEqual(len(inner.points()), 0)
        self.assertEqual(list(inner.attribValue("a3ob_source_counts")), [0, 0, 0])
        self.assertEqual(report.items, [])

    def test_damaged_file_is_a_user_error(self):
        from a3ob_houdini.errors import UserError
        lod = make_lod()
        lod.face_vertices[0] = 99
        with self.assertRaises(UserError) as caught:
            hou_support.import_model(model_of(lod))
        self.assertIn("faces use points that do not exist", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

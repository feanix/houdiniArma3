"""Semantic API of the P3D model: edits go through the raw fields and survive write -> read.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import math
import os
import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LOD, LodType, P3DError, P3DModel, decode_weight, encode_weight

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")


def crate():
    return p3d.read_p3d_file(os.path.join(FIXTURES, "sample_2_crate.p3d"))


def reread(model):
    return p3d.read_p3d(p3d.write_p3d(model))


class TestWeights(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(decode_weight(0), 0.0)
        self.assertEqual(decode_weight(1), 1.0)
        self.assertEqual(decode_weight(128), 0.5)
        self.assertEqual(decode_weight(255), 0.0)

    def test_encode(self):
        self.assertEqual(encode_weight(None), 0)
        self.assertEqual(encode_weight(0.0), 0)
        self.assertEqual(encode_weight(-1.0), 0)
        self.assertEqual(encode_weight(1.0), 1)
        self.assertEqual(encode_weight(2.0), 1)
        self.assertEqual(encode_weight(0.5), 128)
        self.assertEqual(encode_weight(0.9999), 1)
        self.assertEqual(encode_weight(1e-9), 255)

    def test_bytes_1_to_254_round_trip(self):
        for value in range(1, 255):
            self.assertEqual(encode_weight(decode_weight(value)), value)


class TestSelectionEdits(unittest.TestCase):
    def test_change_weight(self):
        model = crate()
        lod = model.lods[0]
        lid = lod.selection("lid")  # 48 of the 96 points
        members = lid.members()
        target = members[0]
        lid.set_weight(target, 0.5)
        non_member = next(i for i in range(lod.point_count) if i not in members)
        lid.set_weight(non_member, 0.25)
        lid.set_weight(members[1], None)

        lod = reread(model).lods[0]
        lid = lod.selection("lid")
        self.assertEqual(lid.weight(target), 0.5)
        self.assertAlmostEqual(lid.weight(non_member), 0.25, delta=1 / 254)
        self.assertIsNone(lid.weight(members[1]))
        self.assertEqual(len(lid.members()), len(members))
        self.assertEqual(lid.weight_list()[target], 0.5)
        self.assertEqual(lid.weights()[target], 0.5)

    def test_add_and_remove_selection(self):
        model = crate()
        lod = model.lods[2]  # shadow: 16 points, 24 faces
        face = lod.face(0)
        lod.add_selection("new_sel", {v: 1.0 for v in face.vertices})
        self.assertEqual(lod.selection_names()[-1], "new_sel")
        with self.assertRaises(ValueError):
            lod.add_selection("NEW_SEL")
        with self.assertRaises(ValueError):
            lod.add_selection("#bad#")

        lod = reread(model).lods[2]
        selection = lod.selection("new_sel")
        self.assertEqual(selection.members(), sorted(face.vertices))
        self.assertIn(0, selection.face_members())  # derived: all its vertices are members
        self.assertTrue(lod.remove_selection("new_sel"))
        self.assertFalse(lod.remove_selection("new_sel"))


class TestPropertyEdits(unittest.TestCase):
    def test_add_set_remove(self):
        model = crate()
        geometry = model.lods[3]
        geometry.set_property("damper", "tank")
        geometry.set_property("Buoyancy", "0")  # case-insensitive key: edits the existing property

        geometry = reread(model).lods[3]
        self.assertEqual(geometry.properties(), [("autocenter", "0"), ("buoyancy", "0"), ("damper", "tank")])
        self.assertEqual(geometry.get_property("DAMPER"), "tank")
        self.assertIsNone(geometry.get_property("missing"))
        # The new property goes after the existing ones, before #Mass# and the selections.
        names = [t.name for t in geometry.taggs]
        self.assertEqual(names.index("#Property#") + 3, names.index("#Mass#"))

        self.assertEqual(geometry.remove_property("autocenter"), 1)
        self.assertEqual(geometry.properties(), [("buoyancy", "0"), ("damper", "tank")])

    def test_property_on_lod_without_any(self):
        model = crate()
        memory = model.lods[5]
        memory.set_property("autocenter", "0")
        memory = reread(model).lods[5]
        self.assertEqual(memory.properties(), [("autocenter", "0")])
        self.assertEqual([t.name for t in memory.taggs][:3], ["#SharpEdges#", "#UVSet#", "#Property#"])

    def test_limits(self):
        lod = LOD()
        with self.assertRaises(ValueError):
            lod.set_property("k" * 64, "v")
        lod.set_property("k" * 63, "v" * 63)
        self.assertEqual(len(lod.taggs[0].payload()), 128)


class TestGeometryEdits(unittest.TestCase):
    def test_add_point_and_face_keep_taggs_consistent(self):
        model = crate()
        geometry = model.lods[3]
        a = geometry.add_point((0.0, 5.0, 0.0))
        b = geometry.add_point((1.0, 5.0, 0.0), flags=0x02000000)
        c = geometry.add_point((0.0, 5.0, 1.0))
        n = geometry.add_normal((0.0, -1.0, 0.0))
        face = geometry.add_face((a, b, c), (n, n, n), ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
                                 material="a3\\data_f\\penetration\\plastic.rvmat")
        geometry.masses[a] = 3.0
        self.assertEqual(geometry.check(), [])

        geometry = reread(model).lods[3]
        self.assertEqual(geometry.point_count, 19)
        self.assertEqual(geometry.face_count, 25)
        self.assertEqual(geometry.point(b), (1.0, 5.0, 0.0))
        self.assertEqual(geometry.point_flags[b], 0x02000000)
        self.assertEqual(geometry.face(face).vertices, (a, b, c))
        self.assertEqual(geometry.face(face).uvs, ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        self.assertEqual(len(geometry.uv_set(0)), 2 * geometry.corner_count)
        self.assertEqual(tuple(geometry.uv_set(0)[-6:]), (0.0, 0.0, 1.0, 0.0, 0.0, 1.0))
        self.assertEqual(geometry.total_mass, 23.0)
        self.assertIsNone(geometry.selection("lid").weight(a))

    def test_set_point(self):
        model = crate()
        model.lods[0].set_point(0, (1.5, -2.25, 3.0))
        self.assertEqual(reread(model).lods[0].point(0), (1.5, -2.25, 3.0))
        with self.assertRaises(IndexError):
            model.lods[0].set_point(1000, (0.0, 0.0, 0.0))

    def test_bad_face(self):
        lod = LOD()
        with self.assertRaises(ValueError):
            lod.add_face((0, 1))
        with self.assertRaises(ValueError):
            lod.add_face((0, 1, 2, 3, 4))
        with self.assertRaises(ValueError):
            lod.add_point((0.0, 1.0))
        with self.assertRaises(ValueError):
            lod.add_normal((0.0, 1.0, 0.0, 0.0))
        self.assertEqual(lod.point_count, 0)
        self.assertEqual(len(lod.points), 0)

    def test_inconsistent_model_refuses_to_write(self):
        model = crate()
        model.lods[0].points.append(1.0)  # a coordinate without a point
        with self.assertRaises(P3DError) as ctx:
            p3d.write_p3d(model)
        self.assertIn("LOD 0", str(ctx.exception))

        model = crate()
        model.lods[3].masses.append(1.0)
        with self.assertRaises(P3DError):
            p3d.write_p3d(model)

        model = crate()
        model.lods[0].selection("lid").vertex_weights.pop()
        with self.assertRaises(P3DError):
            p3d.write_p3d(model)

        with self.assertRaises(P3DError):
            p3d.write_p3d(P3DModel())


class TestOtherEdits(unittest.TestCase):
    def test_masses(self):
        model = crate()
        geometry = model.lods[3]
        geometry.set_masses([2.0] * 16)
        with self.assertRaises(ValueError):
            geometry.set_masses([1.0])
        model.lods[4].set_masses([0.5] * 8)
        result = reread(model)
        self.assertEqual(result.lods[3].total_mass, 32.0)
        self.assertEqual(result.lods[4].total_mass, 4.0)
        result.lods[3].set_masses(None)
        self.assertIsNone(reread(result).lods[3].masses)

    def test_lod_type(self):
        model = crate()
        model.lods[1].set_lod_type(LodType.VISUAL, 2.5)
        model.lods[4].set_lod_type(LodType.VIEW_CARGO_GEOMETRY, 3.0)
        result = reread(model)
        self.assertEqual((result.lods[1].lod_type, result.lods[1].resolution), (LodType.VISUAL, 2.5))
        self.assertEqual(result.lods[4].lod_type, LodType.VIEW_CARGO_GEOMETRY)
        self.assertEqual(result.lods[4].resolution, 3.0)
        self.assertEqual(result.lods[4].name, "View - Cargo Geometry 3")

        model.lods[1].set_lod_type(LodType.VISUAL, 1.0)
        self.assertEqual(model.duplicate_lods(), [1])

    def test_sharp_edges(self):
        model = crate()
        model.lods[3].set_sharp_edges([(0, 1), (1, 2)])
        model.lods[0].remove_sharp_edges()
        result = reread(model)
        self.assertEqual(result.lods[3].sharp_edges(), [(0, 1), (1, 2)])
        self.assertEqual(result.lods[0].sharp_edges(), [])
        self.assertNotIn("#SharpEdges#", [t.name for t in result.lods[0].taggs])

    def test_uv_sets(self):
        model = crate()
        lod = model.lods[0]
        uvs = [0.25] * (2 * lod.corner_count)
        lod.set_uv_set(1, uvs)
        with self.assertRaises(ValueError):
            lod.set_uv_set(2, [0.0])
        result = reread(model).lods[0]
        self.assertEqual(result.uv_set_ids(), [0, 1])
        self.assertEqual(list(result.uv_set(1)), uvs)
        self.assertIsNone(result.uv_set(5))
        # Set 0: the TAGG matches the UVs embedded in the faces for this file.
        self.assertEqual(list(result.uv_set(0)), list(result.corner_uvs()))
        self.assertEqual(result.remove_uv_set(1), 1)

    def test_untouched_lods_stay_identical(self):
        data = p3d.write_p3d(crate())
        model = crate()
        model.lods[6].set_property("map", "road")
        edited = p3d.write_p3d(model)
        prefix = len(data) - len(p3d.write_p3d(P3DModel([crate().lods[6]]))) + 12
        self.assertEqual(edited[:prefix], data[:prefix])


class TestProxies(unittest.TestCase):
    def test_names(self):
        self.assertEqual(p3d.parse_proxy_name("proxy:\\a3\\x\\box.001"), ("\\a3\\x\\box", 1))
        self.assertEqual(p3d.parse_proxy_name("Proxy:a.b.12"), ("a.b", 12))
        self.assertIsNone(p3d.parse_proxy_name("proxy:noindex"))
        self.assertIsNone(p3d.parse_proxy_name("proxy:a.001x"))  # A3OB accepted trailing text
        self.assertIsNone(p3d.parse_proxy_name("camo1"))
        self.assertEqual(p3d.format_proxy_name("\\a3\\x\\box", 1), "proxy:\\a3\\x\\box.001")
        self.assertEqual(p3d.format_proxy_name("p", 1234), "proxy:p.1234")

    def test_frame_rule(self):
        # Right angle at the origin, short leg along +Z (forward), long leg along +Y (up): identity frame.
        frame = p3d.proxy_frame((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 2.0, 0.0))
        self.assertEqual(frame, p3d.ProxyFrame((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        self.assertEqual(frame.matrix_rows()[3], (0.0, 0.0, 0.0, 1.0))
        with self.assertRaises(ValueError):
            p3d.proxy_frame((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0))

    def test_frame_inverse(self):
        angle = 0.7
        y_axis = (0.0, math.cos(angle), math.sin(angle))
        z_axis = (0.0, -math.sin(angle), math.cos(angle))
        frame = p3d.ProxyFrame((1.0, 2.0, 3.0), (1.0, 0.0, 0.0), y_axis, z_axis)
        result = p3d.proxy_frame(*p3d.proxy_triangle(frame))
        for got, expected in zip(sum(result, ()), sum(frame, ())):
            self.assertAlmostEqual(got, expected, places=12)


if __name__ == "__main__":
    unittest.main()

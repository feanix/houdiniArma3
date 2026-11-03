"""P3D Import on both fixtures: every attribute of the data-model spec is checked against the M1 model.

The expected values are worked out here from the M1 columns with the spec's formulas (sections 3 to 13), not by
calling the importer's own helpers. Needs ``hou`` (skipped under plain Python).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_core import p3d
from a3ob_core.p3d import model as m1
from tests import hou_support
from tests.hou_support import FIXTURE_NAMES, HAS_HOU, SOURCE_KEY, hou, packed_prims, polygons

if HAS_HOU:
    from a3ob_houdini import names

ENC = (lambda name: hou.text.encodeAttrib(name)) if HAS_HOU else None


def u32_to_i32(value):
    return value - 2 ** 32 if value >= 2 ** 31 else value


def detail(geo, name):
    attrib = geo.findGlobalAttrib(name)
    return None if attrib is None else geo.attribValue(name)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestFixtureImport(unittest.TestCase):
    """One subTest per fixture and LOD."""

    def test_p3d_geometry_has_one_named_packed_prim_per_lod(self):
        for name in FIXTURE_NAMES:
            model, geo, report, _ = hou_support.imported_fixture(name)
            with self.subTest(fixture=name):
                prims = geo.prims()
                self.assertEqual(len(prims), len(model.lods))
                self.assertTrue(all(isinstance(prim, hou.PackedPrim) for prim in prims))
                self.assertEqual([prim.attribValue("name") for prim in prims], [lod.name for lod in model.lods])
                self.assertIsNone(geo.findGlobalAttrib("a3ob_file_trailing"))
                self.assertEqual(report.items, [])
                for prim in prims:  # Pack keeps the geometry in place
                    self.assertEqual(prim.fullTransform(), hou.hmath.identityTransform())

    def test_expected_lod_counts(self):
        self.assertEqual(len(hou_support.imported_fixture("sample_1_character.p3d")[1].prims()), 9)
        self.assertEqual(len(hou_support.imported_fixture("sample_2_crate.p3d")[1].prims()), 7)

    def test_every_lod_matches_the_m1_model(self):
        for name in FIXTURE_NAMES:
            model, geo, _, _ = hou_support.imported_fixture(name)
            for index, (lod, prim) in enumerate(zip(model.lods, geo.prims())):
                with self.subTest(fixture=name, lod=index):
                    LodChecker(self, lod, index, prim.getEmbeddedGeometry()).check()

    def test_facegroups_option_adds_a_prim_group_for_every_selection(self):
        model = hou_support.read_fixture("sample_2_crate.p3d")
        geo, _, _ = hou_support.import_model(model, facegroups=True)
        for lod, prim in zip(model.lods, geo.prims()):
            inner = prim.getEmbeddedGeometry()
            for selection in lod.selections():
                group = inner.findPrimGroup(ENC(selection.name))
                self.assertIsNotNone(group, selection.name)
                self.assertEqual([p.number() for p in group.prims()],
                                 [i for i, byte in enumerate(selection.face_weights) if byte])

    def test_lod_filter_imports_only_the_chosen_lods(self):
        model = hou_support.read_fixture("sample_2_crate.p3d")
        geo, _, _ = hou_support.import_model(model, lod_indices=[3, 1])
        self.assertEqual([prim.attribValue("name") for prim in geo.prims()], [model.lods[3].name, model.lods[1].name])
        uids = [polygons(prim.getEmbeddedGeometry())[0].attribValue("a3ob_lod_uid") for prim in geo.prims()]
        self.assertEqual(uids, [names.lod_uid(SOURCE_KEY, 3), names.lod_uid(SOURCE_KEY, 1)])

    def test_report_import_time(self):
        """Not a pass/fail check on speed: it prints the sample_1 import time so that a run shows it."""
        model, geo, _, seconds = hou_support.imported_fixture("sample_1_character.p3d")
        size = 5_300_000
        print("\nsample_1_character.p3d import (about %.1f MB, %d LODs): %.2f s" % (size / 1e6, len(geo.prims()), seconds))
        self.assertLess(seconds, 60)


class LodChecker:
    """Compares one imported LOD geometry (``inner``) with its M1 LOD, spec table by spec table."""

    def __init__(self, case, lod, index, inner):
        self.case = case
        self.lod = lod
        self.index = index
        self.inner = inner
        self.refs = lod.proxies()
        self.mesh_points = lod.point_count - 3 * len(self.refs)
        self.mesh_faces = lod.face_count - len(self.refs)
        self.starts = []
        total = 0
        for sides in lod.face_sides:
            self.starts.append(total)
            total += sides
        self.normal_of_corner = [lod.face_normals[4 * f + k] for f in range(lod.face_count)
                                 for k in range(lod.face_sides[f])]

    def check(self):
        case = self.case
        # The fixtures keep all proxies as the last points and faces (spec section 6), so numbers map one to one.
        case.assertEqual(sorted(v for ref in self.refs for v in ref.selection.members()),
                         list(range(self.mesh_points, self.lod.point_count)))
        case.assertEqual(sorted(f for ref in self.refs for f in ref.selection.face_members()),
                         list(range(self.mesh_faces, self.lod.face_count)))
        self.check_topology()
        self.check_corner_data()
        self.check_face_data()
        self.check_point_data()
        self.check_selections()
        self.check_details()
        self.check_sharp_edges()
        self.check_proxies()

    # -- section 4, 5 ------------------------------------------------------------------------------------------------
    def check_topology(self):
        lod, inner, case = self.lod, self.inner, self.case
        points = inner.points()
        case.assertEqual(len(points), self.mesh_points + len(self.refs))  # mesh points, then one pack point per proxy
        for i in range(self.mesh_points):
            x, y, z = lod.point(i)
            case.assertEqual(tuple(points[i].position()), (x, y, -z), "point %d" % i)  # exact float32 values
        polys = polygons(inner)
        case.assertEqual(len(polys), self.mesh_faces)
        for f, poly in enumerate(polys):
            sides = lod.face_sides[f]
            file_points = lod.face_vertices[4 * f:4 * f + sides]
            expected = [file_points[(-k) % sides] for k in range(sides)]
            case.assertEqual([v.point().number() for v in poly.vertices()], expected, "face %d" % f)

    def check_corner_data(self):
        lod, inner, case = self.lod, self.inner, self.case
        uv_ids = lod.uv_set_ids()
        case.assertEqual(sorted(a.name() for a in inner.vertexAttribs() if a.name().startswith("uv")),
                         sorted("uv" if k == 0 else "uv%d" % (k + 1) for k in uv_ids))
        case.assertIsNone(inner.findVertexAttrib("a3ob_face_uv"))  # fixtures: face UVs equal set 0 (spec 8)
        normal_attrib = inner.findVertexAttrib("N")
        case.assertTrue(normal_attrib.isTransformedAsNormal())
        for set_id in uv_ids:
            attrib = inner.findVertexAttrib("uv" if set_id == 0 else "uv%d" % (set_id + 1))
            case.assertEqual(attrib.numericDataType(), hou.numericData.Float64)
            case.assertEqual(attrib.qualifier(), "Texture Coord")
        for f, poly in enumerate(polygons(inner)):
            sides = lod.face_sides[f]
            for k, vertex in enumerate(poly.vertices()):
                corner = self.starts[f] + (-k) % sides
                nx, ny, nz = lod.normal(self.normal_of_corner[corner])
                case.assertEqual(tuple(vertex.attribValue("N")), (-nx, -ny, nz), "N face %d corner %d" % (f, k))
                for set_id in uv_ids:
                    uvs = lod.uv_set(set_id)
                    attrib = "uv" if set_id == 0 else "uv%d" % (set_id + 1)
                    case.assertEqual(tuple(vertex.attribValue(attrib)),
                                     (uvs[2 * corner], 1.0 - uvs[2 * corner + 1], 0.0))

    def check_face_data(self):
        lod, inner, case = self.lod, self.inner, self.case
        uid = names.lod_uid(SOURCE_KEY, self.index)
        for f, poly in enumerate(polygons(inner)):
            case.assertEqual(poly.attribValue("a3ob_face_flags"), u32_to_i32(lod.face_flags[f]))
            case.assertEqual(poly.attribValue("a3ob_texture"), lod.face_textures[f])
            case.assertEqual(poly.attribValue("a3ob_material"), lod.face_materials[f])
            case.assertEqual(poly.attribValue("a3ob_lod_uid"), uid)
        for prim in packed_prims(inner):
            case.assertEqual(prim.attribValue("a3ob_lod_uid"), 0)

    def check_point_data(self):
        lod, inner, case = self.lod, self.inner, self.case
        points = inner.points()
        flags = inner.findPointAttrib("a3ob_vertex_flags")
        case.assertEqual(flags.defaultValue(), 0x02000000)
        for i in range(self.mesh_points):
            case.assertEqual(points[i].attribValue("a3ob_vertex_flags"), u32_to_i32(lod.point_flags[i]))
        if lod.masses is None:
            case.assertIsNone(inner.findPointAttrib("a3ob_mass"))
        else:
            for i in range(self.mesh_points):
                case.assertEqual(points[i].attribValue("a3ob_mass"), lod.masses[i])

    # -- section 7 ---------------------------------------------------------------------------------------------------
    def check_selections(self):
        lod, inner, case = self.lod, self.inner, self.case
        proxy_names = {ref.selection.name for ref in self.refs}
        expected_groups = []
        for selection in lod.selections():
            if selection.name in proxy_names:
                case.assertIsNone(inner.findPointGroup(ENC(selection.name)), "a packed proxy is not a group")
                continue
            expected_groups.append(ENC(selection.name))
            self.check_one_selection(selection)
        case.assertEqual(sorted(g.name() for g in inner.pointGroups()), sorted(expected_groups))

    def check_one_selection(self, selection):
        lod, inner, case = self.lod, self.inner, self.case
        enc = ENC(selection.name)
        group = inner.findPointGroup(enc)
        case.assertIsNotNone(group, selection.name)
        members = [i for i in range(self.mesh_points) if selection.vertex_weights[i]]
        case.assertEqual(sorted(p.number() for p in group.points()), members)
        weighted = any(selection.vertex_weights[i] != 1 for i in members)
        attrib = inner.findPointAttrib("a3ob_w_" + enc)
        if not weighted:
            case.assertIsNone(attrib, "no weight attribute when every weight is 1: " + selection.name)
        else:
            case.assertEqual(attrib.defaultValue(), 1.0)
            points = inner.points()
            for i in range(self.mesh_points):
                want = p3d.f32(p3d.decode_weight(selection.vertex_weights[i])) if i in members else 1.0
                case.assertEqual(points[i].attribValue("a3ob_w_" + enc), want)
        derived = [1 if all(selection.vertex_weights[v] for v in lod.face_vertices[4 * f:4 * f + lod.face_sides[f]])
                   else 0 for f in range(self.mesh_faces)]
        actual = [selection.face_weights[f] for f in range(self.mesh_faces)]
        prim_group = inner.findPrimGroup(enc)
        if actual == derived:
            case.assertIsNone(prim_group, "face bytes follow the rule, so no prim group: " + selection.name)
        else:
            case.assertIsNotNone(prim_group, selection.name)
            case.assertEqual(sorted(p.number() for p in prim_group.prims()), [f for f, b in enumerate(actual) if b])
        case.assertIsNone(inner.findPrimAttrib("a3ob_fw_" + enc))  # no fixture face byte is outside {0, 1}

    # -- section 3, 10, 11 -------------------------------------------------------------------------------------------
    def check_details(self):
        lod, inner, case = self.lod, self.inner, self.case
        case.assertEqual(detail(inner, "a3ob_spec_version"), 1)
        case.assertEqual(detail(inner, "a3ob_lod_type"), int(lod.lod_type))
        case.assertEqual(detail(inner, "a3ob_lod_resolution"), p3d.f32(lod.resolution))
        bits = detail(inner, "a3ob_lod_signature")
        case.assertEqual(names.bits_float(bits), lod.resolution_signature)
        case.assertEqual(detail(inner, "a3ob_lod_flags"), lod.flags)
        case.assertIsNone(inner.findGlobalAttrib("a3ob_eof_active"))
        case.assertEqual(tuple(detail(inner, "a3ob_source_counts")), (lod.point_count, lod.face_count, lod.corner_count))
        properties = lod.properties()
        if properties:
            case.assertEqual(list(detail(inner, "a3ob_prop_names")), [k for k, _ in properties])
            case.assertEqual(list(detail(inner, "a3ob_prop_values")), [v for _, v in properties])
        else:
            case.assertIsNone(inner.findGlobalAttrib("a3ob_prop_names"))
        case.assertIsNone(inner.findGlobalAttrib("a3ob_prop_raw"))  # fixture payloads are NUL padded
        case.assertEqual(list(detail(inner, "a3ob_tagg_order") or []), self.expected_order())
        for absent in ("a3ob_tagg_active", "a3ob_tagg_unknown_names", "a3ob_tagg_unknown_active",
                       "a3ob_tagg_unknown_data", "a3ob_sharp_extra", "a3ob_lod_copy_types"):
            case.assertIsNone(inner.findGlobalAttrib(absent), absent)
        case.assertIsNone(inner.findGlobalAttrib("a3ob_file_trailing"))

    def expected_order(self):
        order = []
        for tagg in self.lod.taggs:
            if isinstance(tagg, m1.SharpEdgesTagg):
                order.append("#SharpEdges#")
            elif isinstance(tagg, m1.UVSetTagg):
                order.append("#UVSet#%d" % tagg.set_id)
            elif isinstance(tagg, m1.PropertyTagg):
                order.append("#Property#")
            elif isinstance(tagg, m1.MassTagg):
                order.append("#Mass#")
            else:
                order.append(tagg.name)
        return order

    # -- section 9.2 -------------------------------------------------------------------------------------------------
    def check_sharp_edges(self):
        inner, case = self.inner, self.case
        has_tagg = any(isinstance(t, m1.SharpEdgesTagg) for t in self.lod.taggs)
        group = inner.findEdgeGroup("a3ob_sharp")
        if not has_tagg:
            case.assertIsNone(group)
            return
        case.assertIsNotNone(group)
        got = [tuple(p.number() for p in edge.points()) for edge in group.edges()]
        case.assertEqual(got, self.lod.sharp_edges(include_degenerate=True))

    # -- section 13 --------------------------------------------------------------------------------------------------
    def check_proxies(self):
        inner, case = self.inner, self.case
        packed = packed_prims(inner)
        case.assertEqual(len(packed), len(self.refs))
        ordered = sorted(self.refs, key=lambda ref: ref.selection.face_members()[0])
        for number, (ref, prim) in enumerate(zip(ordered, packed)):
            face = ref.selection.face_members()[0]
            case.assertEqual(face, self.mesh_faces + number)
            case.assertEqual(prim.attribValue("a3ob_proxy_path"), ref.path)
            case.assertEqual(prim.attribValue("a3ob_proxy_index"), ref.index)
            self.check_one_proxy(ref, prim, face)

    def check_one_proxy(self, ref, prim, face):
        lod, case = self.lod, self.case
        members = ref.selection.members()
        case.assertEqual(list(prim.attribValue("a3ob_proxy_src_P")), [c for v in members for c in lod.point(v)])
        corner_normals = [lod.normal(lod.face_normals[4 * face + k]) for k in range(3)]
        case.assertEqual(list(prim.attribValue("a3ob_proxy_src_N")), [c for n in corner_normals for c in n])
        emb = prim.getEmbeddedGeometry()
        case.assertEqual(len(emb.points()), 3)
        case.assertEqual(len(emb.prims()), 1)
        case.assertIsNone(emb.findGlobalAttrib("a3ob_lod_type"))
        triangle = emb.prims()[0]
        case.assertEqual(triangle.attribValue("a3ob_proxy_path"), ref.path)
        case.assertEqual(triangle.attribValue("a3ob_proxy_index"), ref.index)
        self.check_proxy_frame(members, face, prim, emb)
        self.check_proxy_attributes(members, face, emb)

    def check_proxy_frame(self, members, face, prim, emb):
        """The packed transform applied to the embedded triangle gives back the Houdini positions (spec 13.4)."""
        lod, case = self.lod, self.case
        full = prim.fullTransform()
        rows = [full.asTuple()[4 * r:4 * r + 4] for r in range(4)]
        det = hou.Matrix3(tuple(tuple(row[:3]) for row in rows[:3])).determinant()
        case.assertAlmostEqual(det, 1.0, places=5)  # a proper rotation, never a mirror
        for slot, v in enumerate(members):
            x, y, z = lod.point(v)
            local = emb.points()[slot].position()
            world = [sum(local[i] * rows[i][c] for i in range(3)) + rows[3][c] for c in range(3)]
            for got, want in zip(world, (x, y, -z)):
                case.assertAlmostEqual(got, want, places=4)
        sides = 3
        file_points = lod.face_vertices[4 * face:4 * face + 3]
        expected = [members.index(file_points[(-k) % sides]) for k in range(sides)]
        case.assertEqual([v.point().number() for v in emb.prims()[0].vertices()], expected)
        for k, vertex in enumerate(emb.prims()[0].vertices()):
            nx, ny, nz = lod.normal(lod.face_normals[4 * face + (-k) % sides])
            local = vertex.attribValue("N")
            world = [sum(local[i] * rows[i][c] for i in range(3)) for c in range(3)]
            for got, want in zip(world, (-nx, -ny, nz)):
                case.assertAlmostEqual(got, want, places=5)

    def check_proxy_attributes(self, members, face, emb):
        """The embedded triangle carries the LOD's attribute scheme and the groups of its three points."""
        lod, case = self.lod, self.case
        triangle = emb.prims()[0]
        case.assertEqual(triangle.attribValue("a3ob_face_flags"), u32_to_i32(lod.face_flags[face]))
        case.assertEqual(triangle.attribValue("a3ob_texture"), lod.face_textures[face])
        case.assertEqual(triangle.attribValue("a3ob_material"), lod.face_materials[face])
        case.assertIsNone(emb.findPrimAttrib("a3ob_lod_uid"))
        for slot, v in enumerate(members):
            case.assertEqual(emb.points()[slot].attribValue("a3ob_vertex_flags"), u32_to_i32(lod.point_flags[v]))
        proxy_names = {ref.selection.name for ref in self.refs}
        expected = []
        for selection in lod.selections():
            if selection.name in proxy_names:
                continue
            slots = [slot for slot, v in enumerate(members) if selection.vertex_weights[v]]
            group = emb.findPointGroup(ENC(selection.name))
            if slots:
                expected.append(ENC(selection.name))
                case.assertIsNotNone(group, selection.name)
                case.assertEqual(sorted(p.number() for p in group.points()), slots)
        case.assertEqual(sorted(g.name() for g in emb.pointGroups()), sorted(expected))
        if lod.uv_set_ids():
            uvs = lod.uv_set(0)
            start = self.starts[face]
            for k, vertex in enumerate(emb.prims()[0].vertices()):
                corner = start + (-k) % 3
                case.assertEqual(tuple(vertex.attribValue("uv")), (uvs[2 * corner], 1.0 - uvs[2 * corner + 1], 0.0))


if __name__ == "__main__":
    unittest.main()

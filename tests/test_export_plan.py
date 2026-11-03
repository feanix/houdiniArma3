"""P3D export decisions that need no Houdini: options, path rules, transforms, collection plan, selection bytes, TAGG
order, the build from plain data (spec sections 3, 5, 7, 11, 13 and 16), and the write function.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from a3ob_core import p3d
from a3ob_houdini import messages, names
from a3ob_houdini.p3d_export import ExportOptions, build, to_bytes, write_file
from a3ob_houdini.p3d_export import collect_plan, normals, options as options_mod, transforms
from a3ob_houdini.p3d_export.data import Collection, Details, GroupData, LodData, MeshData, Poly, ProxyData
from a3ob_houdini.p3d_export.lod_build import signature_for
from a3ob_houdini.p3d_export.mesh_build import uv_set_id
from a3ob_houdini.p3d_export.proxy_rebuild import file_corner_order
from a3ob_houdini.p3d_export.selections import weight_byte
from a3ob_houdini.p3d_export.taggs import Item, order_items


def quad_mesh():
    """Two triangles of a square in the XY plane, in Houdini axes, with normals, UVs and one group."""
    points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    normal = (0.0, 0.0, -1.0)
    uvs = {"uv": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]}
    first = Poly(0, (0, 1, 2), [normal] * 3, dict(uvs), 0, "a.paa", "")
    second = Poly(1, (0, 2, 3), [normal] * 3, {"uv": [(0.0, 0.0), (1.0, 1.0), (0.0, 1.0)]}, 0, "b.paa", "")
    mesh = MeshData(points=points, polys=[first, second], uv_names=["uv"])
    mesh.groups.append(GroupData("head", "head", [0, 1, 2], weights=[0.5, 1.0, 1.0, 1.0]))
    return mesh


def lod_data(mesh=None, **kwargs):
    details = Details(lod_type=0, resolution=1.0)
    for key, value in kwargs.items():
        setattr(details, key, value)
    return LodData(0, mesh or quad_mesh(), details)


def build_one(data, options=None):
    report = messages.Report()
    model = build(Collection([data]), options or ExportOptions(), report)
    return model, report


class TestOptionsAndPaths(unittest.TestCase):
    def test_defaults(self):
        options = ExportOptions()
        self.assertEqual((options.lowercase, options.relative_paths, options.sort_sections, options.sort_lods,
                          options.auto_components), (False,) * 5)
        self.assertEqual(options.duplicate_lods, "fail")
        with self.assertRaises(ValueError):
            ExportOptions(duplicate_lods="maybe")

    def test_selection_mask(self):
        mask = options_mod.DEFAULT_SELECTION_MASK
        self.assertTrue(options_mod.matches_mask(mask, "head"))
        self.assertFalse(options_mod.matches_mask(mask, "__temp"))
        self.assertTrue(options_mod.matches_mask(mask, "xn__proxies_cka"))
        self.assertTrue(options_mod.matches_mask("comp*", "component01"))
        self.assertFalse(options_mod.matches_mask("comp*", "head"))
        self.assertFalse(options_mod.matches_mask("* ^head", "head"))

    def test_make_relative_keeps_case(self):
        self.assertEqual(options_mod.make_relative("P:\\A3\\x.paa", "P:\\"), "A3\\x.paa")
        self.assertEqual(options_mod.make_relative("p:\\a3\\x.paa", "P:"), "a3\\x.paa")
        self.assertEqual(options_mod.make_relative("D:\\Other\\x.paa", "P:\\"), "Other\\x.paa")
        self.assertEqual(options_mod.make_relative("a3\\x.paa", "P:\\"), "a3\\x.paa")
        self.assertEqual(options_mod.make_relative("", "P:\\"), "")
        self.assertEqual(options_mod.relative_proxy_name("proxy:P:\\a3\\p.001", "P:\\"), "proxy:a3\\p.001")
        self.assertEqual(options_mod.relative_proxy_name("head", "P:\\"), "head")

    def test_fold_collisions(self):
        self.assertEqual(options_mod.fold_collisions(["Head", "head", "Hip", "HEAD"]),
                         [("Head", "head"), ("Head", "HEAD")])
        self.assertEqual(options_mod.fold_collisions(["a", "a"]), [])


class TestTransforms(unittest.TestCase):
    def test_det_and_identity(self):
        self.assertEqual(transforms.det3(transforms.IDENTITY), 1.0)
        mirror = (-1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0)
        self.assertLess(transforms.det3(mirror), 0)
        self.assertTrue(transforms.is_identity(transforms.IDENTITY))
        self.assertFalse(transforms.is_identity(mirror))

    def test_point_uses_row_vectors(self):
        move = (1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 5.0, 6.0, 7.0, 1.0)
        self.assertEqual(transforms.transform_point(move, (1.0, 2.0, 3.0)), (6.0, 8.0, 10.0))

    def test_normals_use_the_inverse_transpose(self):
        scale = (2.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0)
        matrix = transforms.normal_matrix(scale)
        self.assertEqual(transforms.transform_normal(matrix, (1.0, 0.0, 0.0)), (0.5, 0.0, 0.0))
        flat = (1.0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0)
        self.assertIsNone(transforms.normal_matrix(flat))
        self.assertEqual(transforms.transform_normal(None, (0.0, 1.0, 0.0)), (0.0, 1.0, 0.0))


class TestPlans(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(collect_plan.classify(True, 5, 2, 9, False)[0], collect_plan.LOD)
        self.assertEqual(collect_plan.classify(False, 3, 3, 3, False)[0], collect_plan.P3D)
        self.assertIn("mixes", collect_plan.classify(False, 3, 2, 3, False)[1])
        self.assertIn("primitive attribute", collect_plan.classify(False, 3, 3, 3, True)[1])
        self.assertIn("empty", collect_plan.classify(False, 0, 0, 0, False)[1])
        self.assertIn("no LOD data", collect_plan.classify(False, 0, 0, 4, False)[1])
        self.assertIn("not a LOD", collect_plan.classify(False, 2, 0, 4, False)[1])

    def test_fragments_of_one_source_are_one_lod_at_the_first_fragment(self):
        infos = [(False, 5), (True, 7), (False, 6), (True, 7), (True, 8)]
        self.assertEqual(collect_plan.group_prims(infos), [[0], [1, 3], [2], [4]])

    def test_trailing_bytes(self):
        self.assertEqual(collect_plan.decode_trailing("AQID"), b"\x01\x02\x03")
        self.assertIsNone(collect_plan.decode_trailing("not base64!"))

    def test_uv_set_ids(self):
        self.assertEqual([uv_set_id(n) for n in ("uv", "uv2", "uv5", "uv1", "uv0", "uv01", "uvx", "N")],
                         [0, 1, 4, None, None, None, None, None])

    def test_file_corner_order_is_its_own_inverse(self):
        self.assertEqual(file_corner_order([0, 1, 2, 3]), [0, 3, 2, 1])
        self.assertEqual(file_corner_order(file_corner_order([0, 1, 2, 3])), [0, 1, 2, 3])
        self.assertEqual(file_corner_order([0, 1, 2], mirrored=True), [2, 0, 1])

    def test_weight_byte(self):
        self.assertEqual([weight_byte(w) for w in (1.0, 2.0, 0.0, -1.0, 0.5)], [1, 1, 255, 255, 128])
        for byte in range(1, 256):
            self.assertEqual(weight_byte(p3d.decode_weight(byte)), byte)
        with self.assertRaises(ValueError):
            weight_byte(float("nan"))

    def test_order_follows_the_hint_then_the_canonical_order(self):
        def item(token, active=1):
            return Item(token, p3d.SelectionTagg(token, b"", b"", active))
        items = [item("#SharpEdges#"), item("a"), item("b"), item("c")]
        ordered = order_items(items, ["c", "gone", "a"], [0, 9, 7])
        self.assertEqual([t.name for t in ordered], ["c", "a", "#SharpEdges#", "b"])
        self.assertEqual([t.active for t in ordered], [0, 7, 1, 1])

    def test_missing_normals_split_at_sharp_edges(self):
        # Two triangles of a hinge, 90 degrees apart, sharing the edge 0-1.
        mesh = MeshData(points=[(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)],
                        polys=[Poly(0, (0, 1, 2)), Poly(1, (0, 3, 1))])
        smooth = normals.compute_missing_normals(mesh, [0, 1])
        self.assertAlmostEqual(smooth[0][0][1], smooth[1][0][1], places=6)  # one shared normal at point 0
        mesh.sharp = [(0, 1)]
        split = normals.compute_missing_normals(mesh, [0, 1])
        self.assertNotEqual(split[0][0], split[1][0])
        # A Houdini polygon with algebraic cross +Z has the normal -Z (spec F5).
        self.assertEqual(normals.face_normal([(0, 0, 0), (1, 0, 0), (0, 1, 0)]), (-0.0, -0.0, -1.0))

    def test_signature_keeps_stored_bits_while_the_view_matches(self):
        odd = names.bits_float(names.float_bits(p3d.f32(1e13 * (1 + 3e-7))))
        details = Details(lod_type=int(p3d.decode_signature(odd)[0]), resolution=0.0, signature=names.float_bits(odd))
        self.assertEqual(signature_for(details), odd)
        details.resolution = 2.0  # the view was edited: encode, and Geometry has no resolution
        with self.assertRaises(ValueError):
            signature_for(details)
        self.assertEqual(signature_for(Details(lod_type=0, resolution=3.0)), 3.0)


class TestBuildFromPlainData(unittest.TestCase):
    def test_a_small_lod_builds_and_round_trips(self):
        model, report = build_one(lod_data())
        self.assertEqual(report.texts(messages.ERROR), [])
        lod = model.lods[0]
        self.assertEqual((lod.point_count, lod.face_count), (4, 2))
        self.assertEqual(lod.point(2), (1.0, 1.0, -0.0))  # z is negated (spec 4.3)
        self.assertEqual(list(lod.face(0).vertices), [0, 2, 1])  # Houdini [0,1,2] -> file corners 0, 2, 1
        self.assertEqual(lod.normal(lod.face(0).normals[0]), (-0.0, -0.0, -1.0))
        self.assertEqual(lod.normal_count, 1)  # first-appearance table, de-duplicated
        self.assertEqual(lod.face(1).texture, "b.paa")
        selection = lod.selection("head")
        self.assertEqual(list(selection.vertex_weights), [128, 1, 1, 0])
        self.assertEqual(list(selection.face_weights), [1, 0])  # derived: only face 0 has all its points in the group
        again = p3d.read_p3d(to_bytes(model, messages.Report()))
        self.assertEqual(p3d.write_p3d(again), p3d.write_p3d(model))

    def test_uv_set_and_record_uv_are_flipped(self):
        model, _ = build_one(lod_data())
        lod = model.lods[0]
        self.assertEqual(lod.uv_set_ids(), [0])
        # Houdini corners (0,0) (1,0) (1,1) become file corners 0, 2, 1 with v' = 1 - v.
        self.assertEqual(list(lod.uv_set(0)[:6]), [0.0, 1.0, 1.0, 0.0, 1.0, 1.0])
        self.assertEqual(list(lod.face(0).uvs), [(0.0, 1.0), (1.0, 0.0), (1.0, 1.0)])

    def test_mirrored_lod_reverses_the_corners(self):
        data = lod_data()
        data.mirrored = True
        model, _ = build_one(data)
        self.assertEqual(list(model.lods[0].face(0).vertices), [2, 0, 1])

    def test_faces_with_fewer_than_three_sides_are_skipped(self):
        mesh = quad_mesh()
        mesh.polys.append(Poly(2, (0, 1), [(0, 0, -1)] * 2, {"uv": [(0, 0), (0, 0)]}))
        model, report = build_one(lod_data(mesh))
        self.assertEqual(model.lods[0].face_count, 2)
        self.assertTrue(any("fewer than 3" in text for text in report.texts(messages.WARNING)))

    def test_five_sided_polygon_is_an_error(self):
        mesh = quad_mesh()
        mesh.points.append((2.0, 0.0, 0.0))
        mesh.polys.append(Poly(2, (0, 1, 2, 3, 4), [(0, 0, -1)] * 5, {"uv": [(0, 0)] * 5}))
        model, report = build_one(lod_data(mesh))
        self.assertIsNone(model)
        self.assertTrue(any("more than 4" in text for text in report.texts(messages.ERROR)))

    def test_unencodable_texture_is_an_error(self):
        mesh = quad_mesh()
        mesh.polys[0].texture = "\u0159.paa"
        model, report = build_one(lod_data(mesh))
        self.assertIsNone(model)
        self.assertTrue(any("a3ob_texture" in text for text in report.texts(messages.ERROR)))

    def test_pack_points_are_not_exported(self):
        mesh = quad_mesh()
        mesh.points.append((9.0, 9.0, 9.0))
        mesh.pack_points = {4}
        model, _ = build_one(lod_data(mesh))
        self.assertEqual(model.lods[0].point_count, 4)

    def test_stale_blobs_are_dropped_and_fresh_ones_kept(self):
        import base64
        payload = base64.b64encode(b"hello").decode()
        extra = dict(unknown_names=["#Marker#"], unknown_active=[1], unknown_data=[payload], tagg_order=["#?0", "head"])
        model, report = build_one(lod_data(source_counts=(4, 2, 6), **extra))
        self.assertEqual([t.name for t in model.lods[0].taggs], ["#Marker#", "head", "#UVSet#"])
        model, report = build_one(lod_data(source_counts=(5, 2, 6), **extra))
        self.assertEqual([t.name for t in model.lods[0].taggs], ["head", "#UVSet#"])
        self.assertTrue(any("opaque TAGGs are dropped" in text for text in report.texts(messages.WARNING)))

    def test_the_count_heuristic_runs_only_for_a_lod_without_a_uid(self):
        """D15: more points and faces than imported is a warning only when no polygon has a uid."""
        def merged_warnings(uid):
            mesh = quad_mesh()
            for poly in mesh.polys:
                poly.uid = uid
            _, report = build_one(lod_data(mesh, source_counts=(3, 1, 3)))
            return [t for t in report.texts(messages.WARNING) if "merged" in t]
        self.assertEqual(len(merged_warnings(0)), 1)
        self.assertEqual(merged_warnings(7), [])

    def test_two_uids_still_warn(self):
        mesh = quad_mesh()
        mesh.polys[0].uid, mesh.polys[1].uid = 7, 8
        _, report = build_one(lod_data(mesh, source_counts=(3, 1, 3)))
        found = [t for t in report.texts(messages.WARNING) if "merged" in t]
        self.assertEqual(len(found), 1)
        self.assertIn("uids", found[0])

    def test_inline_proxy_selection_is_the_union_of_group_and_prim_attributes(self):
        mesh = quad_mesh()
        mesh.polys[1].proxy = ("\\a3\\p", 2)
        model, _ = build_one(lod_data(mesh))
        proxy = model.lods[0].selection("proxy:\\a3\\p.002")
        self.assertEqual(list(proxy.vertex_weights), [1, 0, 1, 1])
        self.assertEqual(list(proxy.face_weights), [0, 1])

    def test_packed_proxy_gets_its_own_selection_and_embedded_groups(self):
        inner = MeshData(points=[(0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, -1.0)],
                         polys=[Poly(0, (0, 2, 1), [(1.0, 0.0, 0.0)] * 3, {"uv": [(0, 0)] * 3})])
        inner.groups.append(GroupData("head", "head", [1]))
        move = (1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 3.0, 0.0, 0.0, 1.0)
        proxy = ProxyData(5, "\\a3\\p", 1, "", 0, move, inner)
        data = lod_data()
        data.mesh.pack_points = set()
        data.proxies.append(proxy)
        model, report = build_one(data)
        self.assertEqual(report.texts(messages.ERROR), [])
        lod = model.lods[0]
        self.assertEqual((lod.point_count, lod.face_count), (7, 3))
        self.assertEqual(lod.point(4), (3.0, 0.0, 0.0))
        self.assertEqual(lod.point(6), (3.0, 0.0, 1.0))  # -z of (0, 0, -1)
        own = lod.selection("proxy:\\a3\\p.001")
        self.assertEqual(list(own.vertex_weights), [0, 0, 0, 0, 1, 1, 1])
        self.assertEqual(list(own.face_weights), [0, 0, 1])
        head = lod.selection("head")  # mesh members and the embedded member, with the mesh weight kept
        self.assertEqual(list(head.vertex_weights), [128, 1, 1, 0, 0, 1, 0])
        self.assertEqual(head.face_weights[2], 0)  # derived from this group: not all three points are members

    def test_packed_prim_without_a_path_is_an_error(self):
        inner = MeshData(points=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], polys=[Poly(0, (0, 1, 2))])
        data = lod_data()
        data.proxies.append(ProxyData(5, "", 0, "", 0, transforms.IDENTITY, inner))
        model, report = build_one(data)
        self.assertIsNone(model)
        self.assertTrue(any("neither a LOD nor a proxy" in text for text in report.texts(messages.ERROR)))

    def test_non_triangle_proxy_gets_a_plain_triangle(self):
        inner = MeshData(points=[(0.0, 0.0, 0.0)], polys=[])
        data = lod_data()
        data.proxies.append(ProxyData(5, "\\a3\\p", 0, "", 0, transforms.IDENTITY, inner))
        model, report = build_one(data)
        lod = model.lods[0]
        self.assertEqual([lod.point(i) for i in (4, 5, 6)], [(0.0, 0.0, 0.0), (0.0, 2.0, -0.0), (0.0, 0.0, 1.0)])
        self.assertTrue(any("plain proxy triangle" in text for text in report.texts(messages.WARNING)))

    def test_duplicate_signatures_and_copies(self):
        report = messages.Report()
        twice = Collection([lod_data(), lod_data()])
        twice.lods[1].index = 1
        self.assertIsNone(build(twice, ExportOptions(), report))
        self.assertTrue(report.has_errors)
        report = messages.Report()
        self.assertEqual(len(build(twice, ExportOptions(duplicate_lods="skip"), report).lods), 1)
        report = messages.Report()
        self.assertEqual(len(build(twice, ExportOptions(duplicate_lods="ignore"), report).lods), 2)
        report = messages.Report()
        copy = Collection([lod_data(copy_types=[0], copy_resolutions=[4.0])])
        model = build(copy, ExportOptions(), report)
        self.assertEqual([lod.name for lod in model.lods], ["Resolution 1", "Resolution 4"])

    def test_nothing_to_export_is_an_error(self):
        report = messages.Report()
        self.assertIsNone(build(Collection(), ExportOptions(), report))
        self.assertTrue(report.has_errors)

    def test_trailing_bytes_are_appended(self):
        report = messages.Report()
        model = build(Collection([lod_data()], b"tail"), ExportOptions(), report)
        self.assertTrue(to_bytes(model, report).endswith(b"tail"))


class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="a3ob_write_")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_writes_through_a_temporary_file(self):
        path = os.path.join(self.folder, "sub", "a.p3d")
        report = messages.Report()
        self.assertTrue(write_file(path, b"one", report))
        self.assertTrue(write_file(path, b"two", report, backup=True))
        with open(path, "rb") as file:
            self.assertEqual(file.read(), b"two")
        with open(path + ".bak", "rb") as file:
            self.assertEqual(file.read(), b"one")
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["a.p3d", "a.p3d.bak"])
        self.assertFalse(report.has_errors)

    def test_a_failure_is_reported_and_leaves_no_temporary_file(self):
        target = os.path.join(self.folder, "taken")
        os.mkdir(target)  # a folder where the file should go: the rename fails
        report = messages.Report()
        self.assertFalse(write_file(target, b"data", report))
        self.assertTrue(any("could not write" in text for text in report.texts(messages.ERROR)))
        self.assertEqual(os.listdir(self.folder), ["taken"])
        self.assertEqual(os.listdir(target), [])

    def test_no_path_is_an_error(self):
        report = messages.Report()
        self.assertFalse(write_file("", b"data", report))
        self.assertTrue(report.has_errors)


if __name__ == "__main__":
    unittest.main()

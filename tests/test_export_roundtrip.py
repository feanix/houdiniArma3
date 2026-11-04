"""P3D export (spec sections 3, 13, 15, 16): L0 round trips of both fixtures and edit tests. Needs Houdini; skips under
plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from a3ob_core import p3d

from tests import hou_support as hs

if hs.HAS_HOU:
    import hou
    from a3ob_houdini import lod_container, messages, p3d_export
    from a3ob_houdini.p3d_export import ExportOptions


def lod_geometry(geo, index):
    """An editable copy of the LOD geometry in prim ``index`` of a P3D geometry (detail, packed proxies and all)."""
    result = hou.Geometry()
    result.merge(lod_container.lod_content(geo.prims()[index]))
    return result


def p3d_of(prims_geo, indices):
    """A new P3D geometry with the given LOD prims of ``prims_geo``, in the given order."""
    result = hou.Geometry()
    for index in indices:
        result.merge(lod_container.only_prim(prims_geo.prims()[index]))
    return result


def export(geometries, options=None):
    """``(model or None, report)``. The model is read back from the bytes."""
    report = messages.Report()
    data = p3d_export.build_bytes(list(geometries), options, report)
    return (p3d.read_p3d(data, strict=False) if data is not None else None), report, data


def texts(report, kind):
    return report.texts(kind)


def one_lod_bytes(lod):
    return p3d.write_p3d(p3d.P3DModel([lod]))


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestRoundTripL0(unittest.TestCase):
    """Spec 15: import then export with no edits gives the bytes of the source file."""

    def check_fixture(self, name):
        model, geo, _, _ = hs.imported_fixture(name)
        with open(os.path.join(hs.FIXTURES, name), "rb") as file:
            source = file.read()
        report = messages.Report()
        data = p3d_export.build_bytes([geo], None, report)
        self.assertEqual(texts(report, messages.ERROR), [])
        self.assertIsNotNone(data)
        self.assertEqual(len(data), len(source))
        self.assertTrue(data == source, "the export differs from the source file")
        return report

    def test_character_is_byte_identical(self):
        self.check_fixture("sample_1_character.p3d")

    def test_crate_is_byte_identical(self):
        self.check_fixture("sample_2_crate.p3d")

    def test_character_selections_split_between_mesh_and_proxies(self):
        """Spec 13.5: head, injury_hands, injury_legs and -proxies live partly or fully inside the packed proxies."""
        model, geo, _, _ = hs.imported_fixture("sample_1_character.p3d")
        lod = lod_geometry(geo, 0)
        proxies = [prim for prim in lod.prims() if isinstance(prim, hou.PackedPrim)]
        self.assertEqual(len(proxies), 11)
        mesh_groups = {group.name(): len(group.points()) for group in lod.pointGroups()}
        for name in ("injury_hands", "injury_legs", "xn__proxies_cka"):
            self.assertEqual(mesh_groups.get(name), 0, name)  # empty in the mesh, all members in proxies
        source = model.lods[0]
        out, report, _ = export([lod])
        self.assertEqual(texts(report, messages.ERROR), [])
        written = out.lods[0]
        for name in ("head", "injury_hands", "injury_legs", "-proxies"):
            self.assertEqual(bytes(written.selection(name).vertex_weights), bytes(source.selection(name).vertex_weights))
            self.assertEqual(bytes(written.selection(name).face_weights), bytes(source.selection(name).face_weights))
        self.assertGreater(sum(1 for b in source.selection("injury_hands").vertex_weights if b), 0)
        self.assertEqual([t.name for t in written.taggs], [t.name for t in source.taggs])


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestEdits(unittest.TestCase):
    """Spec 13.5, 3.2, 11.2, 5: edits in Houdini reach the file the way the spec says."""

    def test_moved_packed_proxy(self):
        model, geo, _, _ = hs.imported_fixture("sample_1_character.p3d")
        lod = lod_geometry(geo, 0)
        proxy = [prim for prim in lod.prims() if isinstance(prim, hou.PackedPrim)][0]
        name = p3d.format_proxy_name(proxy.attribValue("a3ob_proxy_path"), proxy.attribValue("a3ob_proxy_index"))
        source = model.lods[0]
        selection = source.selection(name)
        indices = selection.members()
        self.assertEqual(len(indices), 3)
        shift = (0.5, 0.25, -0.125)  # Houdini axes; the file z is negated
        proxy.setTransform(proxy.fullTransform() * hou.hmath.buildTranslate(shift))
        out, report, _ = export([lod])
        self.assertEqual(texts(report, messages.ERROR), [])
        written = out.lods[0]
        self.assertEqual(written.point_count, source.point_count)
        for i in range(source.point_count):
            expected = source.point(i)
            if i in indices:
                expected = (expected[0] + shift[0], expected[1] + shift[1], expected[2] - shift[2])
            for a, b in zip(written.point(i), expected):
                self.assertAlmostEqual(a, b, places=5)
        # Every selection keeps its bytes on the moved points and on the proxy face.
        face = selection.face_members()[0]
        for tagg in source.selections():
            other = written.selection(tagg.name)
            self.assertEqual([other.vertex_weights[i] for i in indices], [tagg.vertex_weights[i] for i in indices],
                             tagg.name)
            self.assertEqual(other.face_weights[face], tagg.face_weights[face], tagg.name)
        self.assertEqual([t.name for t in written.taggs], [t.name for t in source.taggs])
        self.assertEqual(list(written.face_vertices), list(source.face_vertices))

    def test_untouched_proxies_snap_to_the_source_values(self):
        model, geo, _, _ = hs.imported_fixture("sample_1_character.p3d")
        out, report, _ = export([lod_geometry(geo, 0)])
        source = model.lods[0]
        self.assertEqual(list(out.lods[0].points), list(source.points))
        self.assertEqual(list(out.lods[0].normals), list(source.normals))

    def test_mirrored_lod_transform_reverses_winding(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        mirrored = hou.Geometry()
        mirrored.merge(geo)
        mirrored.prims()[0].setTransform(hou.hmath.buildScale((-1.0, 1.0, 1.0)))
        out, report, _ = export([mirrored])
        self.assertEqual(texts(report, messages.ERROR), [])
        self.assertTrue(any("mirrored" in text for text in texts(report, messages.WARNING)))
        source = model.lods[0]
        written = out.lods[0]
        for i in range(source.point_count):
            x, y, z = source.point(i)
            self.assertEqual(written.point(i), (-x, y, z))
        for face in range(source.face_count):
            s = list(source.face(face).vertices)
            count = len(s)
            # Houdini order [s0, s(n-1), ..., s1], reversed by the mirror, then mapped back (spec 3.2 and 4.3).
            expected = [s[1]] + [s[0]] + [s[k] for k in range(count - 1, 1, -1)]
            self.assertEqual(list(written.face(face).vertices), expected, face)
        # The stored normals follow the cross product of the corner order (spec F3); after the flip they still do.
        wrong = 0
        for face in range(written.face_count):
            f = written.face(face)
            a, b, c = (written.point(v) for v in f.vertices[:3])
            e1 = tuple(q - p for p, q in zip(a, b))
            e2 = tuple(q - p for p, q in zip(a, c))
            cross = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
            n = written.normal(f.normals[0])
            if sum(x * y for x, y in zip(cross, n)) <= 0:
                wrong += 1
        self.assertEqual(wrong, 0)

    def test_added_point_stales_count_bound_blobs(self):
        model = hs.read_fixture("sample_2_crate.p3d")
        lod = model.lods[0]
        lod.taggs.append(p3d.RawTagg("#Marker#", b"keep me"))
        lod.taggs.append(p3d.MaskTagg("#Hidden#", bytes(lod.point_count), bytes(lod.face_count), active=0))
        geo, _, _ = hs.import_model(model)
        out, report, data = export([geo])
        self.assertEqual(data, p3d.write_p3d(model))  # counts unchanged: the blobs are written back
        edited = lod_geometry(geo, 0)
        edited.createPoint().setPosition((9.0, 8.0, 7.0))
        out, report, _ = export([edited])
        self.assertEqual(texts(report, messages.ERROR), [])
        names = [t.name for t in out.lods[0].taggs]
        self.assertNotIn("#Marker#", names)
        self.assertNotIn("#Hidden#", names)
        self.assertIn("lid", names)
        self.assertTrue(any("opaque" in text and "dropped" in text for text in texts(report, messages.WARNING)))
        self.assertEqual(out.lods[0].point_count, model.lods[0].point_count + 1)

    def test_faces_with_fewer_than_three_sides_are_skipped(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        points = edited.points()
        short = edited.createPolygon()
        short.addVertex(points[0])
        short.addVertex(points[1])
        out, report, _ = export([edited])
        self.assertEqual(texts(report, messages.ERROR), [])
        self.assertTrue(any("fewer than 3" in text for text in texts(report, messages.WARNING)))
        self.assertEqual(one_lod_bytes(out.lods[0]), one_lod_bytes(model.lods[0]))

    def test_polygons_with_more_than_four_sides_are_an_error(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        points = edited.points()
        big = edited.createPolygon()
        for point in points[:5]:
            big.addVertex(point)
        out, report, data = export([edited], ExportOptions(triangulate=False))  # task #49: on, it is divided
        self.assertIsNone(data)
        self.assertTrue(any("more than 4" in text for text in texts(report, messages.ERROR)))
        out, report, data = export([edited])
        self.assertIsNotNone(data)
        self.assertTrue(any("1 polygons triangulated" in text for text in texts(report, messages.WARNING)))

    def test_collection_errors(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        mixed = hou.Geometry()
        mixed.merge(geo)
        mixed.createPolygon()
        report = messages.Report()
        self.assertIsNone(p3d_export.build_bytes([mixed], None, report))
        self.assertTrue(any("mixes" in text for text in texts(report, messages.ERROR)))
        empty = messages.Report()
        self.assertIsNone(p3d_export.build_bytes([hou.Geometry()], None, empty))
        self.assertTrue(empty.has_errors)
        bare = hou.Geometry()
        bare.createPoint()
        self.assertIsNone(p3d_export.build_bytes([bare], None, messages.Report()))

    def test_lod_order_is_kept_and_sorting_is_an_option(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        reverse = p3d_of(geo, [3, 1, 0])
        out, report, _ = export([reverse])
        self.assertEqual([lod.name for lod in out.lods], ["Geometry", "Resolution 2", "Resolution 1"])
        self.assertTrue(any("not sorted" in text for text in texts(report, messages.WARNING)))
        out, report, _ = export([reverse], ExportOptions(sort_lods=True))
        self.assertEqual([lod.name for lod in out.lods], ["Resolution 1", "Resolution 2", "Geometry"])

    def test_duplicate_signatures(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        twice = p3d_of(geo, [0, 0])
        out, report, data = export([twice])
        self.assertIsNone(data)
        self.assertTrue(any("same type and resolution" in text for text in texts(report, messages.ERROR)))
        out, report, _ = export([twice], ExportOptions(duplicate_lods="skip"))
        self.assertEqual(len(out.lods), 1)
        out, report, _ = export([twice], ExportOptions(duplicate_lods="ignore"))
        self.assertEqual(len(out.lods), 2)

    def test_lod_copies_become_extra_lods(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        edited.addArrayAttrib(hou.attribType.Global, "a3ob_lod_copy_types", hou.attribData.Int)
        edited.addArrayAttrib(hou.attribType.Global, "a3ob_lod_copy_resolutions", hou.attribData.Float)
        edited.setGlobalAttribValue("a3ob_lod_copy_types", [0])
        edited.setGlobalAttribValue("a3ob_lod_copy_resolutions", [3.0])
        out, report, _ = export([edited])
        self.assertEqual([lod.name for lod in out.lods], ["Resolution 1", "Resolution 3"])
        self.assertEqual(out.lods[0].point_count, out.lods[1].point_count)

    def test_edited_signature_is_encoded_and_unedited_bits_are_kept(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        edited.setGlobalAttribValue("a3ob_lod_resolution", 7.0)
        out, report, _ = export([edited])
        self.assertEqual(out.lods[0].name, "Resolution 7")
        edited.setGlobalAttribValue("a3ob_lod_type", 6)  # Geometry has no resolution: 7.0 cannot be encoded
        out, report, data = export([edited])
        self.assertIsNone(data)
        self.assertTrue(any("cannot be written" in text for text in texts(report, messages.ERROR)))

    def test_packed_fragments_of_one_source_are_one_lod(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        source = lod_geometry(geo, 2)
        source.addAttrib(hou.attribType.Prim, "name", "")
        for prim in source.prims():
            prim.setAttribValue("name", "a" if prim.number() < 12 else "b")
        verb = hou.sopNodeTypeCategory().nodeVerb("pack")
        verb.setParms({"packbyname": 1, "nameattribute": "name"})
        packed = hou.Geometry()
        verb.execute(packed, [source])
        self.assertEqual({type(prim).__name__ for prim in packed.prims()}, {"PackedFragment"})
        out, report, _ = export([packed])
        self.assertEqual(texts(report, messages.ERROR), [])
        self.assertEqual(len(out.lods), 1)
        self.assertEqual(out.lods[0].face_count, model.lods[2].face_count)
        self.assertTrue(any("PackedFragment" in text for text in texts(report, messages.WARNING)))

    def test_prim_level_lod_type_is_an_error(self):
        # A geometry with a prim attribute and no detail attribute is what unpacking several LODs at once gives.
        flat = hou.Geometry()
        flat.createPoint()
        flat.createPolygon()
        flat.addAttrib(hou.attribType.Prim, "a3ob_lod_type", 0)
        report = messages.Report()
        self.assertIsNone(p3d_export.build_bytes([flat], None, report))
        self.assertTrue(any("primitive attribute" in text for text in texts(report, messages.ERROR)))


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestOptions(unittest.TestCase):
    """Export options (spec 16, ux-tools 5.9): all data-changing ones are off by default."""

    def test_defaults_are_off(self):
        options = ExportOptions()
        self.assertFalse(options.lowercase)
        self.assertFalse(options.relative_paths)
        self.assertFalse(options.sort_sections)
        self.assertFalse(options.sort_lods)
        self.assertFalse(options.auto_components)
        self.assertEqual(options.duplicate_lods, "fail")
        self.assertEqual(options.selection_mask, "* ^__*")

    def edited_crate_lod(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        first = edited.prims()[0]
        first.setAttribValue("a3ob_texture", "P:\\A3\\Data_F\\CRATE_CO.PAA")
        first.setAttribValue("a3ob_material", "P:\\A3\\Data_F\\Crate.RVMAT")
        edited.setGlobalAttribValue("a3ob_prop_names", ["LODNoShadow"])
        edited.setGlobalAttribValue("a3ob_prop_values", ["YES"])
        # A second group that equals "lid" up to case.
        lid = edited.findPointGroup("lid")
        upper = edited.createPointGroup("LID")
        upper.add(lid.points())
        return edited

    def test_default_keeps_case_and_warns_about_names_equal_up_to_case(self):
        out, report, _ = export([self.edited_crate_lod()])
        written = out.lods[0]
        self.assertEqual(written.face_textures[0], "P:\\A3\\Data_F\\CRATE_CO.PAA")
        self.assertEqual(written.face_materials[0], "P:\\A3\\Data_F\\Crate.RVMAT")
        self.assertEqual(written.properties(), [("LODNoShadow", "YES")])
        self.assertIn("LID", written.selection_names())
        self.assertTrue(any("differ only in case" in text for text in texts(report, messages.WARNING)))

    def test_lowercase_covers_four_fields_and_warns_about_a_collision(self):
        out, report, _ = export([self.edited_crate_lod()], ExportOptions(lowercase=True))
        self.assertEqual(texts(report, messages.ERROR), [])
        written = out.lods[0]
        self.assertEqual(written.face_textures[0], "p:\\a3\\data_f\\crate_co.paa")
        self.assertEqual(written.face_materials[0], "p:\\a3\\data_f\\crate.rvmat")
        self.assertEqual(written.properties(), [("lodnoshadow", "yes")])
        self.assertEqual(written.selection_names(), ["lid", "camo1", "lid"])
        self.assertTrue(any("lowercase makes the selection names" in text for text in texts(report, messages.WARNING)))

    def test_lowercase_property_collision_warns(self):
        edited = self.edited_crate_lod()
        edited.setGlobalAttribValue("a3ob_prop_names", ["Class", "class"])
        edited.setGlobalAttribValue("a3ob_prop_values", ["a", "b"])
        out, report, _ = export([edited], ExportOptions(lowercase=True))
        self.assertTrue(any("lowercase makes the property names" in text for text in texts(report, messages.WARNING)))
        self.assertEqual(len(out.lods[0].properties()), 2)

    def test_relative_paths(self):
        out, report, _ = export([self.edited_crate_lod()], ExportOptions(relative_paths=True, project_root="P:\\"))
        written = out.lods[0]
        self.assertEqual(written.face_textures[0], "A3\\Data_F\\CRATE_CO.PAA")
        self.assertEqual(written.face_materials[0], "A3\\Data_F\\Crate.RVMAT")

    def test_sort_sections(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        for number in range(6):
            edited.prims()[number].setAttribValue("a3ob_texture", "a.paa" if number % 2 == 0 else "b.paa")
        out, report, _ = export([edited])
        self.assertEqual(out.lods[0].face_textures[:4], ["a.paa", "b.paa", "a.paa", "b.paa"])
        out, report, _ = export([edited], ExportOptions(sort_sections=True))
        written = out.lods[0]
        rest = model.lods[0].face_textures[6]
        self.assertEqual(written.face_textures, ["a.paa"] * 3 + ["b.paa"] * 3 + [rest] * (written.face_count - 6))
        self.assertEqual(written.point_count, model.lods[0].point_count)

    def test_auto_components(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        source = model.lods[3]
        self.assertEqual(source.name, "Geometry")
        edited = lod_geometry(geo, 3)
        for name in ("component01", "component02"):
            edited.findPointGroup(name).destroy()
        out, report, _ = export([edited])
        self.assertFalse([n for n in out.lods[0].selection_names() if n.startswith("Component")])
        out, report, _ = export([edited], ExportOptions(auto_components=True))
        written = out.lods[0]
        # Pieces are numbered by their lowest point, so only the sets of points are compared.
        made = [written.selection("Component01").members(), written.selection("Component02").members()]
        wanted = [source.selection("component01").members(), source.selection("component02").members()]
        self.assertEqual(sorted(made), sorted(wanted))
        self.assertEqual(sorted(made[0] + made[1]), list(range(source.point_count)))
        # With components present nothing is made.
        out, report, _ = export([lod_geometry(geo, 3)], ExportOptions(auto_components=True))
        self.assertFalse([n for n in out.lods[0].selection_names() if n.startswith("Component")])


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestWriteFile(unittest.TestCase):
    def test_export_geometry_writes_the_file(self):
        model, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        folder = tempfile.mkdtemp(prefix="a3ob_export_")
        try:
            path = os.path.join(folder, "out", "crate.p3d")
            report = messages.Report()
            self.assertTrue(p3d_export.export_geometry([geo], path, None, report))
            with open(path, "rb") as file, open(os.path.join(hs.FIXTURES, "sample_2_crate.p3d"), "rb") as source:
                self.assertEqual(file.read(), source.read())
            self.assertEqual(os.listdir(os.path.dirname(path)), ["crate.p3d"])  # no temporary file is left
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_errors_write_nothing_and_do_not_raise(self):
        folder = tempfile.mkdtemp(prefix="a3ob_export_")
        try:
            path = os.path.join(folder, "x.p3d")
            report = messages.Report()
            self.assertFalse(p3d_export.export_geometry([hou.Geometry()], path, None, report))
            self.assertTrue(report.has_errors)
            self.assertEqual(os.listdir(folder), [])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

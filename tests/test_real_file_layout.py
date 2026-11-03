"""What Object Builder's own files do that Houdini geometry cannot hold (task #29, docs/design/evidence/samples/
samples_findings.md), and the layout record that now brings it back (task #37, D23, spec 19). Each test builds a small
synthetic LOD (no Samples bytes) that shows one cause.

Purpose: F-1 to F-5 are claims about the importer and exporter. Before #37 these tests pinned "the content comes back, the
bytes do not". Now an untouched LOD must come back as the same bytes, and an edited LOD must fall back to a valid
content-lossless file with one info line (never a wrong file).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import unittest
from unittest import mock

from a3ob_core import p3d
from a3ob_core.p3d import LodType

from tests import hou_support as hs
from tests import samples_support as ss
from tests.test_permutation import bytes_by_position, interleaved_lod, pairs_by_position

if hs.HAS_HOU:
    from a3ob_houdini import lod_container, messages, p3d_export

SQUARE = [(0.0, 0.0, 0.5), (1.0, 0.0, 0.5), (1.0, 1.0, 0.5), (0.0, 1.0, 0.5)]  # z is not 0: see the signed zero test
UP = (0.0, 0.0, 1.0)


def square_lod(normal_table=None, normal_indices=None):
    """Two triangles (0, 1, 2) and (0, 2, 3) on a square, resolution 1."""
    lod = p3d.LOD(LodType.VISUAL, 1.0)
    lod.set_mesh(SQUARE, [(0, 1, 2), (0, 2, 3)], normal_table, normal_indices)
    return lod


def file_bytes(lod):
    return p3d.write_p3d(p3d.P3DModel([lod]))


def export(geo, options=None):
    """``(bytes, report)`` of a P3D Export of a geometry with no options."""
    report = messages.Report()
    data = p3d_export.build_bytes([geo], options, report)
    assert data is not None, report.texts(messages.ERROR)
    return data, report


def round_trip(lod):
    """The LOD after P3D Import and P3D Export with no options, read back."""
    geo, _, _ = hs.import_model(p3d.P3DModel([lod]))
    data, _ = export(geo)
    return p3d.read_p3d(data, strict=True).lods[0]


def layout_notes(report):
    return [text for text in report.texts(messages.INFO) if "layout record" in text]


def lod_geometry(geo):
    """The first LOD of an imported P3D geometry as an ordinary geometry that a test can edit."""
    lod_geo = hs.hou.Geometry()
    lod_geo.merge(hs.packed_prims(geo)[0].getEmbeddedGeometry())
    lod_container.copy_detail_attributes(hs.packed_prims(geo)[0].getEmbeddedGeometry(), lod_geo)
    return lod_geo


def layout_lod():
    """All of F-1, F-2, F-3 and F-5 in one LOD: repeated normal entries, old values in the unused slots, sharp pairs in
    their own order with a degenerate one, and a point with z = +0.0."""
    lod = p3d.LOD(LodType.VISUAL, 1.0)
    lod.set_mesh([(0.0, 0.0, 0.0)] + SQUARE[1:], [(0, 1, 2), (0, 2, 3)], [UP] * 6, [0, 1, 2, 3, 4, 5])
    lod.face_vertices[3] = 7
    lod.face_normals[7] = 5
    lod.face_uvs[6] = 0.25
    lod.set_sharp_edges([(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)])
    return lod


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestFileLayoutSurvives(unittest.TestCase):
    def test_equal_normals_in_separate_table_entries_stay_separate(self):
        """Object Builder writes a table entry per (point, smoothing) and repeats equal vectors. Finding F-1: 159 of 261
        Samples LODs. The record keeps which corner uses which entry."""
        lod = square_lod([UP] * 6, [0, 1, 2, 3, 4, 5])
        out = round_trip(lod)
        self.assertEqual(out.normal_count, 6)
        self.assertEqual(file_bytes(out), file_bytes(lod))

    def test_a_used_slot_of_a_triangle_record_survives_and_so_does_the_unused_one(self):
        """The fourth vertex, normal and UV slot of a triangle is unused; Object Builder leaves old values there. Finding
        F-2."""
        lod = square_lod([UP], [0] * 6)
        lod.face_vertices[3] = 7
        lod.face_normals[3] = 5
        lod.face_uvs[7] = 0.5
        out = round_trip(lod)
        self.assertEqual(list(out.face_vertices[3::4]), [7, 0])
        self.assertEqual(file_bytes(out), file_bytes(lod))

    def test_sharp_edge_pairs_keep_their_order(self):
        """Object Builder keeps the pairs sorted, a pair (a, a) among them. Finding F-3: 46 of 187 Samples LODs."""
        lod = square_lod([UP], [0] * 6)
        lod.set_sharp_edges([(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)])
        out = round_trip(lod)
        self.assertEqual(out.sharp_edges(True), [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)])
        self.assertEqual(file_bytes(out), file_bytes(lod))

    def test_interleaved_proxies_come_back_in_the_file_order(self):
        """A proxy that is not at the end of the point and face lists is written at the end by the content-lossless
        layout (spec 13.6). The record keeps the permutation, so the file order comes back, and with it masks and
        sharp pairs (task #36) in their file numbers. Finding F-4."""
        lod = interleaved_lod()
        out = round_trip(lod)
        self.assertEqual(list(out.points), list(lod.points))
        self.assertEqual(file_bytes(out), file_bytes(lod))
        self.assertEqual(ss.lod_differences(lod, out), [])

    def test_zero_z_comes_back_as_positive_zero(self):
        """Opening a packed LOD turns the -0.0 the import made into +0.0, and the export negates z again. Finding
        F-5. The record keeps the sign of every zero."""
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        lod.set_mesh([(0.0, 0.0, 0.0), (1.0, -0.0, 0.0), (0.0, 1.0, 0.0)], [(0, 1, 2)], [UP], [0] * 3)
        out = round_trip(lod)
        self.assertEqual(out.points.tobytes(), lod.points.tobytes())
        self.assertEqual(file_bytes(out), file_bytes(lod))

    def test_all_causes_together_and_no_info_line_for_an_untouched_lod(self):
        lod = layout_lod()
        geo, _, _ = hs.import_model(p3d.P3DModel([lod]))
        data, report = export(geo)
        self.assertEqual(data, file_bytes(lod))
        self.assertEqual(layout_notes(report), [])

    def test_a_lod_without_a_record_exports_as_before_without_an_info_line(self):
        """A LOD made by hand has no a3ob_layout. It gets the content-lossless layout and no message."""
        with mock.patch("a3ob_houdini.layout_record.build_record", return_value=None):
            geo, _, _ = hs.import_model(p3d.P3DModel([layout_lod()]))
        self.assertIsNone(hs.packed_prims(geo)[0].getEmbeddedGeometry().findGlobalAttrib("a3ob_layout"))
        data, report = export(lod_geometry(geo))
        self.assertEqual(layout_notes(report), [])
        out = p3d.read_p3d(data, strict=True).lods[0]
        self.assertEqual(out.normal_count, 1)

    def test_the_record_is_a_hidden_detail_and_not_a_group_or_a_user_attribute(self):
        geo, _, _ = hs.import_model(p3d.P3DModel([layout_lod()]))
        inner = hs.packed_prims(geo)[0].getEmbeddedGeometry()
        attrib = inner.findGlobalAttrib("a3ob_layout")
        self.assertIsNotNone(attrib)
        self.assertEqual(attrib.dataType(), hs.hou.attribData.String)
        for kind in (inner.pointAttribs(), inner.primAttribs(), inner.vertexAttribs()):
            self.assertNotIn("a3ob_layout", [a.name() for a in kind])
        self.assertEqual([g.name() for g in inner.pointGroups()], [])

    def test_a_record_that_cannot_follow_the_move_is_dropped_with_a_warning(self):
        """An unknown TAGG may hold point numbers. When the proxies move, it is not kept pointing at other points."""
        lod = interleaved_lod()
        lod.taggs.append(p3d.RawTagg("#Odd#", b"abc"))
        geo, report, _ = hs.import_model(p3d.P3DModel([lod]))
        text = [w for w in report.texts(messages.WARNING) if "#Odd#" in w]
        self.assertEqual(len(text), 1)
        self.assertIn(lod.name, text[0])
        data, _ = export(geo)
        self.assertNotIn("#Odd#", [t.name for t in p3d.read_p3d(data, strict=True).lods[0].taggs])

    def test_sort_sections_moves_the_face_bytes_of_an_editor_mask(self):
        """Sort sections writes the faces in section order. A mask kept as bytes per face follows (task #36). The faces
        no longer match the record, so the layout of the export is written, with the info line."""
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        lod.set_mesh(SQUARE, [(0, 1, 2), (0, 2, 3), (0, 1, 3)], [UP], [0] * 9)
        lod.face_textures[:] = ["a.paa", "b.paa", "a.paa"]
        lod.taggs.append(p3d.MaskTagg("#Selected#", bytes(4), bytes([7, 8, 9])))
        geo, _, _ = hs.import_model(p3d.P3DModel([lod]))
        data, report = export(geo, p3d_export.ExportOptions(sort_sections=True))
        out = p3d.read_p3d(data, strict=True).lods[0]
        mask = [t for t in out.taggs if t.name == "#Selected#"][0]
        self.assertEqual(out.face_textures, ["a.paa", "a.paa", "b.paa"])  # face 1 and face 2 swapped places
        self.assertEqual(bytes(mask.face_bytes), bytes([7, 9, 8]))
        self.assertEqual(len(layout_notes(report)), 1)


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestEditedLodFallsBack(unittest.TestCase):
    """The record is bound to the positions and topology it was taken from (spec 19.3)."""

    def edited(self, edit):
        geo, _, _ = hs.import_model(p3d.P3DModel([layout_lod()]))
        lod_geo = lod_geometry(geo)
        edit(lod_geo)
        return export(lod_geo)

    def check_fallback(self, data, report):
        out = p3d.read_p3d(data, strict=True).lods[0]  # a valid file
        self.assertEqual(out.structure_errors(), [])
        notes = layout_notes(report)
        self.assertEqual(len(notes), 1)
        self.assertIn(out.name, notes[0])
        self.assertEqual(out.normal_count, 1)  # the content-lossless layout: one entry per distinct vector
        self.assertEqual(set(out.face_vertices[3::4]), {0})  # the pads are zero again
        return out

    def test_moving_one_point_ignores_the_record(self):
        def edit(geo):
            geo.points()[1].setPosition((1.5, 0.0, -0.5))
        data, report = self.edited(edit)
        out = self.check_fallback(data, report)
        self.assertEqual(out.point(1), (1.5, 0.0, 0.5))

    def test_deleting_a_face_ignores_the_record(self):
        def edit(geo):
            geo.deletePrims([geo.prims()[1]], keep_points=True)
        data, report = self.edited(edit)
        out = self.check_fallback(data, report)
        self.assertEqual(out.face_count, 1)

    def test_an_edited_normal_keeps_the_rest_of_the_layout_without_a_message(self):
        """Changing N is not a change of positions or topology. Corners that shared a table entry may now differ; then
        the table is the first-appearance table, and the rest of the layout still comes back."""
        def edit(geo):
            values = list(geo.vertexFloatAttribValues("N"))
            values[3:6] = [0.0, 0.5, 0.5]  # corner 1 only
            geo.setVertexFloatAttribValues("N", values)
        data, report = self.edited(edit)
        out = p3d.read_p3d(data, strict=True).lods[0]
        self.assertEqual(layout_notes(report), [])
        self.assertEqual(out.sharp_edges(True), [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)])
        self.assertEqual(list(out.face_vertices[3::4]), [7, 0])
        self.assertEqual(out.points.tobytes(), layout_lod().points.tobytes())


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestRecordTravelsThroughExtractAndAssemble(unittest.TestCase):
    """Extract LOD and Assemble LODs carry the record unchanged: the bytes of import, Extract LOD, Assemble LODs and
    export are the bytes of the file."""

    def test_extract_then_assemble_then_export_gives_the_file_bytes(self):
        rig = ss.Rig()
        try:
            folder = tempfile.mkdtemp(prefix="a3ob_layout_")
            source = os.path.join(folder, "layout.p3d")
            second = interleaved_lod()
            second.set_lod_type(LodType.VISUAL, 2.0)
            with open(source, "wb") as file:
                file.write(file_bytes_model([layout_lod(), second]))
            imported = rig.container.createNode("a3ob_p3d_import")
            imported.parm("file").set(source)
            errors, _ = rig.cook(imported)
            self.assertEqual(errors, [])
            assemble = rig.container.createNode("a3ob_assemble_lods")
            assemble.setInput(0, imported)
            for k in range(2):
                extract = rig.container.createNode("a3ob_extract_lod")
                extract.setInput(0, imported)
                extract.parm("lod").set(str(k))
                assemble.setInput(k + 1, extract)
            errors, _ = rig.cook(assemble)
            self.assertEqual(errors, [])
            out = os.path.join(folder, "out.p3d")
            export_node = rig.exporter(assemble, out, "Lossless")
            report, _ = rig.save(export_node)
            self.assertEqual(report.texts(messages.ERROR), [])
            with open(source, "rb") as file:
                expected = file.read()
            with open(out, "rb") as file:
                self.assertEqual(file.read(), expected)
        finally:
            rig.close()


def file_bytes_model(lods):
    return p3d.write_p3d(p3d.P3DModel(lods))


if __name__ == "__main__":
    unittest.main()

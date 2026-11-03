"""What Object Builder's own files do that Houdini geometry cannot hold (task #29, finding in
docs/design/evidence/samples/samples_findings.md). Each test builds a small synthetic LOD (no Samples bytes) that shows one
cause and pins today's behaviour: the content comes back, the bytes do not.

Purpose: the findings are claims about the importer and exporter. These tests are the minimal reproductions, so that a
later fix (a place in the data model for the file layout) turns them from "known L1" into "identical" on purpose.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LodType

from tests import hou_support as hs
from tests import samples_support as ss

if hs.HAS_HOU:
    from a3ob_houdini import messages, p3d_export

SQUARE = [(0.0, 0.0, 0.5), (1.0, 0.0, 0.5), (1.0, 1.0, 0.5), (0.0, 1.0, 0.5)]  # z is not 0: see the signed zero test
UP = (0.0, 0.0, 1.0)


def square_lod(normal_table=None, normal_indices=None):
    """Two triangles (0, 1, 2) and (0, 2, 3) on a square, resolution 1."""
    lod = p3d.LOD(LodType.VISUAL, 1.0)
    lod.set_mesh(SQUARE, [(0, 1, 2), (0, 2, 3)], normal_table, normal_indices)
    return lod


def round_trip(lod):
    """The LOD after P3D Import and P3D Export with no options, read back."""
    geo, _, _ = hs.import_model(p3d.P3DModel([lod]))
    report = messages.Report()
    data = p3d_export.build_bytes([geo], None, report)
    assert data is not None, report.texts(messages.ERROR)
    return p3d.read_p3d(data, strict=True).lods[0]


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestFileLayoutThatDoesNotSurvive(unittest.TestCase):
    def test_equal_normals_in_separate_table_entries_become_one_entry(self):
        """Object Builder writes a table entry per (point, smoothing) and repeats equal vectors; the exporter writes
        one entry per distinct vector. Finding F-1: 95 of 189 Samples LODs."""
        lod = square_lod([UP] * 6, [0, 1, 2, 3, 4, 5])
        out = round_trip(lod)
        self.assertEqual(lod.normal_count, 6)
        self.assertEqual(out.normal_count, 1)
        self.assertEqual(ss.lod_differences(lod, out), ["normal table"])
        self.assertEqual([out.normal(i) for i in out.face_corner_normal_indices()], [UP] * 6)

    def test_a_used_slot_of_a_triangle_record_survives_but_the_unused_one_does_not(self):
        """The fourth vertex slot of a triangle is unused; Object Builder leaves old values there. The exporter writes
        zero. Finding F-2."""
        lod = square_lod([UP], [0] * 6)
        lod.face_vertices[3] = 7
        lod.face_normals[3] = 5
        out = round_trip(lod)
        self.assertEqual(list(out.face_vertices[3::4]), [0, 0])
        self.assertEqual(ss.lod_differences(lod, out), ["padding"])

    def test_sharp_edge_pairs_that_are_not_edges_move_to_the_end(self):
        """Object Builder keeps the pairs sorted, a pair (a, a) among them. The importer puts such a pair in
        a3ob_sharp_extra and the exporter writes the extras last. Finding F-3: 46 of 187 Samples LODs with sharp edges."""
        lod = square_lod([UP], [0] * 6)
        pairs = [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)]
        lod.set_sharp_edges(pairs)
        out = round_trip(lod)
        self.assertEqual(out.sharp_edges(True), [(0, 1), (0, 2), (0, 3), (1, 2), (2, 3), (1, 1)])
        self.assertEqual(ss.lod_differences(lod, out), ["sharp edge order"])

    def test_interleaved_proxy_moves_to_the_end_and_file_indices_do_not_follow(self):
        """A proxy that is not at the end of the point and face lists is written at the end (spec 13.6, L1). What the
        file keeps as indices, the editor masks and the sharp edge pairs that touch a proxy point, then points at other
        points. Finding F-4 (OQ-11): 19 Samples LODs."""
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        proxy = [(5.0, 0.0, 0.0), (5.0, 1.0, 0.0), (5.0, 0.0, 1.0)]
        lod.set_mesh(proxy + SQUARE, [(0, 1, 2), (3, 4, 5), (3, 5, 6)], [UP], [0] * 9)
        lod.add_selection("proxy:\\a3\\test\\thing.001", {0: 1.0, 1: 1.0, 2: 1.0})
        lod.taggs.append(p3d.MaskTagg("#Hidden#", bytes([1, 0, 0, 0, 0, 0, 0]), bytes([1, 0, 0])))
        lod.set_sharp_edges([(0, 1), (3, 4)])
        out = round_trip(lod)
        self.assertEqual(list(out.points[-9:]), list(lod.points[:9]))  # the proxy points are now last
        mask = [t for t in out.taggs if t.name == "#Hidden#"][0]
        self.assertEqual(bytes(mask.vertex_bytes), bytes([1, 0, 0, 0, 0, 0, 0]))  # still point 0: now a square corner
        # The content differs (a mask and a sharp pair now sit on other points), so the comparison names the tagg kinds.
        self.assertEqual(ss.lod_differences(lod, out), ["content: MaskTagg, SharpEdgesTagg"])

    def test_zero_z_comes_back_as_negative_zero(self):
        """Opening a packed LOD (the unpack verb) turns the -0.0 the import made into +0.0, and the export negates z again.
        The value is equal; the bits are not. Finding F-5."""
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        lod.set_mesh([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [(0, 1, 2)], [UP], [0] * 3)
        out = round_trip(lod)
        self.assertEqual(out.point(0), lod.point(0))
        self.assertNotEqual(out.points.tobytes(), lod.points.tobytes())
        self.assertEqual(ss.lod_differences(lod, out), ["signed zero"])


if __name__ == "__main__":
    unittest.main()

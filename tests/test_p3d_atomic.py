"""Regression tests for the M1 follow-ups (task 4): add_point/add_face/set_mesh leave the LOD unchanged when an
input is bad, non-str texture or material never reaches the writer as a raw TypeError, and a lenient-raw TAGG is only
accepted while it is unchanged since the read.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LOD, LodType, MaskTagg, MassTagg, P3DError, P3DModel, RawTagg, SelectionTagg, UVSetTagg

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")

COLUMNS = ("flags", "points", "point_flags", "normals", "face_sides", "face_vertices", "face_normals", "face_uvs",
           "face_flags", "face_textures", "face_materials", "eof_active", "resolution_signature")


def snapshot(lod):
    """Every column as a plain value, plus the TAGG list (identity, name and payload) and the dropped TAGGs."""
    columns = tuple((name, list(getattr(lod, name)) if hasattr(getattr(lod, name), "__len__")
                     else getattr(lod, name)) for name in COLUMNS)
    taggs = tuple((id(t), t.name, t.payload()) for t in lod.taggs)
    return columns, taggs, tuple(id(t) for t in lod.dropped_taggs)


def rich_lod():
    """A LOD with a selection, mass, mask and UV set so a half-done append would show up."""
    lod = LOD(LodType.VISUAL, 1.0)
    for position in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)):
        lod.add_point(position)
    lod.add_normal((0.0, 0.0, 1.0))
    lod.add_face((0, 1, 2), (0, 0, 0), [(0, 0), (1, 0), (0, 1)], 5, "a.paa", "a.rvmat")
    lod.add_selection("sel", {0: 1.0, 1: 1.0, 2: 1.0})
    lod.set_masses([1.0, 2.0, 3.0, 4.0])
    lod.set_uv_set(1, [0.0] * 6)
    lod.taggs.append(MaskTagg("#Hidden#", bytes(4), bytes(1)))
    lod.taggs.append(RawTagg("#Unknown#", b"\x07"))
    lod.taggs.append(RawTagg("#Lock#", b"\x01\x02", counts=(4, 1, 3)))  # malformed raw, would be dropped on success
    return lod


class TestBadCallsLeaveLodUnchanged(unittest.TestCase):
    def assert_unchanged(self, call, errors=(ValueError, TypeError)):
        lod = rich_lod()
        before = snapshot(lod)
        with self.assertRaises(errors):
            call(lod)
        self.assertEqual(snapshot(lod), before)
        self.assertEqual(lod.structure_errors(), [])

    def test_good_call_still_works_on_the_rich_lod(self):
        lod = rich_lod()
        lod.add_point((1.0, 2.0, 3.0), 7)
        lod.add_face((0, 1, 3), None, [(0, 0), (1, 0), (0, 1)])
        self.assertEqual(lod.structure_errors(), [])

    def test_add_point_bad_position(self):
        self.assert_unchanged(lambda lod: lod.add_point((1.0, 2.0)))
        self.assert_unchanged(lambda lod: lod.add_point(("x", 0.0, 0.0)))

    def test_add_point_bad_flags(self):
        for flags in (-1, 2 ** 32, "x", 1.5, None):
            with self.subTest(flags=flags):
                self.assert_unchanged(lambda lod: lod.add_point((0.0, 0.0, 0.0), flags))

    def test_add_face_bad_vertices(self):
        self.assert_unchanged(lambda lod: lod.add_face((0, 1)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2, 3, 0)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, -1)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, "x")))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2.5)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2 ** 32)))

    def test_add_face_bad_normals(self):
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), (0, 0)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), (0, 0, -1)))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), (0, "x", 0)))

    def test_add_face_bad_uvs(self):
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), uvs=[(0, 0), (1, 0)]))  # too few pairs
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), uvs=[(0, 0), (1, 0), (0.5,)]))  # short pair
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), uvs=[(0, 0), (1, 0), (0, 1, 2)]))  # long pair
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), uvs=[(0, 0), (1, 0), ("a", "b")]))
        self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), uvs=[(0, 0), (1, 0), 5]))

    def test_add_face_bad_flags(self):
        for flags in (-1, 2 ** 32, "x", 1.5, None):
            with self.subTest(flags=flags):
                self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), flags=flags))

    def test_add_face_non_str_texture_or_material(self):
        for value in (None, 5, b"a.paa"):
            with self.subTest(value=value):
                self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), texture=value), TypeError)
                self.assert_unchanged(lambda lod: lod.add_face((0, 1, 2), material=value), TypeError)

    def test_set_mesh_non_str_texture_or_material(self):
        points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        for value in (None, 5, b"a.paa"):
            with self.subTest(value=value):
                self.assert_unchanged(lambda lod: lod.set_mesh(points, [(0, 1, 2)], textures=[value]))
                self.assert_unchanged(lambda lod: lod.set_mesh(points, [(0, 1, 2)], materials=[value]))


class TestWriterNeverLeaksTypeError(unittest.TestCase):
    def test_non_str_texture_set_by_column_edit(self):
        lod = rich_lod()
        lod.face_textures[0] = None
        with self.assertRaises(P3DError):
            p3d.write_p3d(P3DModel([lod]))

    def test_non_str_material_set_by_column_edit(self):
        lod = rich_lod()
        lod.face_materials[0] = 5
        with self.assertRaises(P3DError):
            p3d.write_p3d(P3DModel([lod]))

    def test_non_str_tagg_name(self):
        lod = rich_lod()
        lod.taggs.append(RawTagg(None, b""))
        with self.assertRaises(P3DError):
            p3d.write_p3d(P3DModel([lod]))


class TestLenientRawGuard(unittest.TestCase):
    def lenient_model(self):
        """A model read leniently with a #Mass# whose size does not fit (kept raw)."""
        lod = LOD(LodType.VISUAL, 1.0)
        for position in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
            lod.add_point(position)
        lod.add_face((0, 1, 2))
        lod.taggs.append(RawTagg("#Mass#", b"\x00" * 4, counts=(3, 1, 3)))
        data = p3d.write_p3d(P3DModel([lod]))
        model = p3d.read_p3d(data, strict=False)
        raw = model.lods[0].taggs[0]
        self.assertIsInstance(raw, RawTagg)
        self.assertIsNotNone(raw.counts)
        return data, model

    def test_lenient_round_trip_is_byte_identical(self):
        data, model = self.lenient_model()
        self.assertEqual(model.lods[0].structure_errors(), [])
        self.assertEqual(p3d.write_p3d(model), data)

    def test_edited_data_is_refused(self):
        _, model = self.lenient_model()
        model.lods[0].taggs[0].data = b"\x00" * 8
        self.assertTrue(model.lods[0].structure_errors())
        with self.assertRaisesRegex(P3DError, "would not read back"):
            p3d.write_p3d(model)

    def test_renamed_tagg_is_refused(self):
        _, model = self.lenient_model()
        model.lods[0].taggs[0].name = "#UVSet#"
        with self.assertRaisesRegex(P3DError, "would not read back"):
            p3d.write_p3d(model)

    def test_edit_then_restore_is_accepted(self):
        data, model = self.lenient_model()
        raw = model.lods[0].taggs[0]
        raw.data = b"\x01" * 4
        raw.data = b"\x00" * 4
        self.assertEqual(p3d.write_p3d(model), data)

    def test_fixed_size_names_also_guarded(self):
        lod = LOD(LodType.VISUAL, 1.0)
        lod.taggs.append(RawTagg("#SharpEdges#", b"\x00" * 4, counts=(0, 0, 0)))
        self.assertEqual(lod.structure_errors(), [])
        lod.taggs[0].data = b"\x00" * 5
        self.assertTrue(lod.structure_errors())


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the M1 review: count-dependent TAGGs follow geometry edits, the writer refuses TAGGs that
would not read back, face selection bytes, check() messages, NaN weights, the bulk mesh API.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import struct
import unittest

from a3ob_core import binary, p3d
from a3ob_core.p3d import (
    LOD, AnimationTagg, LodType, MaskTagg, MassTagg, P3DError, P3DModel, RawTagg, SelectionTagg, UVSetTagg,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")


def character():
    return p3d.read_p3d_file(os.path.join(FIXTURES, "sample_1_character.p3d"))


def crate():
    return p3d.read_p3d_file(os.path.join(FIXTURES, "sample_2_crate.p3d"))


def reread(model):
    return p3d.read_p3d(p3d.write_p3d(model))


def triangle_lod():
    lod = LOD(LodType.VISUAL, 1.0)
    for position in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)):
        lod.add_point(position)
    lod.add_normal((0.0, 0.0, 1.0))
    lod.add_face((0, 1, 2), (0, 0, 0))
    return lod


class TestCountDependentRawTaggs(unittest.TestCase):
    """Review major: add_point/add_face left raw #Selected#/#Animation# at the old size and wrote a broken file."""

    def test_raw_selected_and_animation_follow_add_point(self):
        model = character()
        lod = model.lods[0]
        points, faces = lod.point_count, lod.face_count
        selected = bytes([1, 0] * (points // 2) + [1] * (points % 2)) + bytes(faces)
        frame = struct.pack("<f", 0.5) + binary.array_to_bytes(lod.points)
        lod.taggs.append(RawTagg("#Selected#", selected))
        lod.taggs.append(RawTagg("#Animation#", frame))
        index = lod.add_point((9.0, 8.0, 7.0))
        self.assertEqual(lod.structure_errors(), [])
        self.assertEqual(lod.dropped_taggs, [])

        lod = reread(model).lods[0]
        mask = next(t for t in lod.taggs if t.name == "#Selected#")
        self.assertIsInstance(mask, MaskTagg)
        self.assertEqual(len(mask.payload()), points + 1 + faces)
        self.assertEqual(bytes(mask.vertex_bytes[:points]), selected[:points])
        self.assertEqual(mask.vertex_bytes[index], 0)
        animation = next(t for t in lod.taggs if t.name == "#Animation#")
        self.assertIsInstance(animation, AnimationTagg)
        self.assertEqual(animation.frame_time, 0.5)
        self.assertEqual(len(animation.positions), 3 * (points + 1))
        self.assertEqual(tuple(animation.positions[-3:]), (9.0, 8.0, 7.0))

    def test_masks_follow_add_face(self):
        lod = triangle_lod()
        lod.taggs.append(MaskTagg("#Hidden#", bytes((1, 1, 1, 0)), bytes((1,))))
        lod.taggs.append(RawTagg("#Lock#", bytes(5)))
        lod.add_face((1, 3, 2))
        result = reread(P3DModel([lod])).lods[0]
        hidden, lock = result.taggs
        self.assertEqual((hidden.name, bytes(hidden.face_bytes)), ("#Hidden#", b"\x01\x00"))
        self.assertEqual((lock.name, len(lock.vertex_bytes), len(lock.face_bytes)), ("#Lock#", 4, 2))

    def test_malformed_raw_tagg_is_dropped_on_edit(self):
        lod = triangle_lod()
        # A wrong-sized #Selected# as a lenient read keeps it: written verbatim while the counts are unchanged...
        lod.taggs.append(RawTagg("#Selected#", b"\x01\x02", counts=(4, 1, 3)))
        lod.taggs.append(RawTagg("#Unknown#", b"\x07"))
        self.assertEqual(lod.structure_errors(), [])
        # ...but it cannot be resized, so a geometry edit drops it and says so.
        lod.add_point((5.0, 5.0, 5.0))
        self.assertEqual([t.name for t in lod.dropped_taggs], ["#Selected#"])
        self.assertEqual([t.name for t in lod.taggs], ["#Unknown#"])
        self.assertEqual(reread(P3DModel([lod])).lods[0].point_count, 5)

    def test_stale_read_raw_tagg_is_refused(self):
        lod = triangle_lod()
        lod.taggs.append(RawTagg("#Mass#", b"\x00" * 4, counts=(4, 1, 3)))
        self.assertEqual(lod.structure_errors(), [])
        lod.point_flags.append(0)  # direct column edits bypass add_point
        lod.points.extend((0.0, 0.0, 0.0))
        with self.assertRaisesRegex(P3DError, "would not read back"):
            p3d.write_p3d(P3DModel([lod]))

    def test_reader_parses_editor_taggs_and_never_raises_on_them(self):
        lod = triangle_lod()
        lod.taggs.append(MaskTagg("#Selected#", bytes((0, 1, 0, 0)), bytes((0,))))
        data = p3d.write_p3d(P3DModel([lod]))
        self.assertIsInstance(p3d.read_p3d(data).lods[0].taggs[0], MaskTagg)
        broken = data.replace(b"#Selected#\x00" + struct.pack("<I", 5) + bytes((0, 1, 0, 0, 0)),
                              b"#Selected#\x00" + struct.pack("<I", 2) + bytes((0, 1)))
        model = p3d.read_p3d(broken)  # strict: editor-only data is kept raw with a warning
        self.assertEqual(len(model.warnings), 1)
        self.assertIsInstance(model.lods[0].taggs[0], RawTagg)
        self.assertEqual(p3d.write_p3d(model), broken)


class TestWriterRefusesUnreadableTaggs(unittest.TestCase):
    """Review minor: TAGGs that wrote fine but did not read back (or silently corrupted the model)."""

    def assert_refused(self, tagg):
        lod = triangle_lod()
        lod.taggs.append(tagg)
        self.assertTrue(lod.structure_errors())
        with self.assertRaises(P3DError):
            p3d.write_p3d(P3DModel([lod]))

    def test_refused(self):
        renamed_mass = MassTagg([0.0] * 4)
        renamed_mass.name = "#Massive#"
        cases = [
            RawTagg("#EndOfFile#"),                 # ended the TAGG list early on re-read
            RawTagg("", b"x"),                       # '' is a selection name: needs 4 + 1 bytes
            RawTagg("#Property#", b"x"),             # strict re-read raised
            SelectionTagg("#x", bytes(4), bytes(1)),  # came back as a RawTagg
            SelectionTagg("x#", bytes(4), bytes(1)),
            MaskTagg("#Visible#", bytes(4), bytes(1)),
            renamed_mass,
            SelectionTagg("#EndOfFile#", bytes(4), bytes(1)),
        ]
        for tagg in cases:
            with self.subTest(tagg=tagg):
                self.assert_refused(tagg)

    def test_accepted_raw_taggs_read_back_typed(self):
        lod = triangle_lod()
        lod.taggs.append(RawTagg("", bytes((1, 1, 1, 0, 1))))
        lod.taggs.append(RawTagg("#Mass#", struct.pack("<4f", 1, 2, 3, 4)))
        data = p3d.write_p3d(P3DModel([lod]))
        result = p3d.read_p3d(data).lods[0]  # strict
        self.assertIsInstance(result.taggs[0], SelectionTagg)
        self.assertEqual(result.taggs[0].name, "")
        self.assertEqual(list(result.masses), [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(p3d.write_p3d(p3d.read_p3d(data)), data)

    def test_add_selection_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            triangle_lod().add_selection("")


class TestFaceWeights(unittest.TestCase):
    """Review minor: face selection bytes went stale on add_face and set_weight."""

    def test_add_face_derives_face_bytes(self):
        lod = triangle_lod()
        selection = lod.add_selection("sel", {1: 1.0, 2: 1.0, 3: 0.5})
        self.assertEqual(bytes(selection.face_weights), b"\x00")
        lod.add_face((1, 3, 2))            # all members
        lod.add_face((0, 1, 3))            # point 0 is not a member
        lod.add_face((1, 3, 99))           # missing point: not a member, no crash
        self.assertEqual(bytes(selection.face_weights), b"\x00\x01\x00\x00")

    def test_derive_all_face_weights(self):
        lod = triangle_lod()
        selection = lod.add_selection("sel", {0: 1.0, 1: 1.0, 2: 1.0})
        other = lod.add_selection("other", {0: 1.0})
        self.assertEqual((bytes(selection.face_weights), bytes(other.face_weights)), (b"\x01", b"\x00"))
        selection.set_weight(0, None)
        other.set_weight(1, 1.0)
        other.set_weight(2, 1.0)
        self.assertEqual(bytes(selection.face_weights), b"\x01")  # documented: set_weight leaves faces alone
        lod.derive_all_face_weights()
        self.assertEqual((bytes(selection.face_weights), bytes(other.face_weights)), (b"\x00", b"\x01"))


class TestCheckMessages(unittest.TestCase):
    """Review minor: check() missed degenerate faces, duplicate UV ids, case-duplicate names, broken proxies."""

    def test_fixtures_are_clean(self):
        for model in (character(), crate()):
            for lod in model.lods:
                self.assertEqual(lod.check(), [])

    def test_degenerate_face(self):
        lod = triangle_lod()
        lod.face_sides[0] = 2
        self.assertEqual(lod.structure_errors(), [])
        self.assertTrue(any("2 sides" in e for e in lod.check()))
        self.assertEqual(p3d.write_p3d(P3DModel([lod]))[:4], b"MLOD")  # still writable (raw round trips)

    def test_duplicate_uv_set(self):
        lod = triangle_lod()
        lod.taggs.append(UVSetTagg(1, [0.0] * 6))
        lod.taggs.append(UVSetTagg(1, [0.5] * 6))
        self.assertTrue(any("#UVSet# id 1" in e for e in lod.check()))

    def test_case_duplicate_selections(self):
        lod = triangle_lod()
        lod.add_selection("Camo")
        lod.taggs.append(SelectionTagg("camo", bytes(4), bytes(1)))
        self.assertTrue(any("up to case" in e for e in lod.check()))

    def test_proxy_without_triangle(self):
        lod = triangle_lod()
        lod.add_selection("proxy:\\a3\\box.001", {0: 1.0, 3: 1.0})
        self.assertTrue(any("no triangle" in e for e in lod.check()))
        lod.add_selection("proxy:\\a3\\box.002", {0: 1.0, 1: 1.0, 2: 1.0})
        self.assertEqual(sum("no triangle" in e for e in lod.check()), 1)


class TestNaNWeight(unittest.TestCase):
    def test_nan_raises_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "NaN"):
            p3d.encode_weight(float("nan"))
        lod = triangle_lod()
        selection = lod.add_selection("sel")
        with self.assertRaisesRegex(ValueError, "NaN"):
            selection.set_weight(0, float("nan"))
        self.assertEqual(selection.vertex_weights[0], 0)


class TestBulkMesh(unittest.TestCase):
    """Review minor: no bulk constructor or corner accessor for the Houdini glue."""

    def rebuild(self, source):
        sides, corner_points = source.face_corner_indices()
        polygons, position = [], 0
        for count in sides:
            polygons.append(corner_points[position:position + count])
            position += count
        lod = LOD()
        lod.set_mesh(source.points, polygons, source.normals, source.face_corner_normal_indices(),
                     source.corner_uvs(), source.point_flags, source.face_flags, source.face_textures,
                     source.face_materials)
        return lod

    def test_fixture_lods_rebuild_identically(self):
        for model in (character(), crate()):
            for source in model.lods:
                with self.subTest(lod=source.name):
                    lod = self.rebuild(source)
                    for column in ("points", "point_flags", "normals", "face_sides", "face_vertices", "face_normals",
                                   "face_uvs", "face_flags", "face_textures", "face_materials"):
                        self.assertEqual(getattr(lod, column), getattr(source, column), column)
                    self.assertEqual(lod.check(), [])

    def test_corner_indices(self):
        roadway = crate().lods[6]  # quads
        sides, corner_points = roadway.face_corner_indices()
        self.assertEqual(list(sides), [4] * 5)
        self.assertEqual(list(corner_points), list(roadway.face_vertices))
        lod = triangle_lod()
        lod.add_face((1, 3, 2, 0), (0, 0, 0, 0))
        lod.face_vertices[3] = 77  # garbage in the unused slot is left out
        sides, corner_points = lod.face_corner_indices()
        self.assertEqual((list(sides), list(corner_points)), ([3, 4], [0, 1, 2, 1, 3, 2, 0]))
        self.assertEqual(len(lod.face_corner_normal_indices()), 7)

    def test_mixed_polygons_and_defaults(self):
        lod = crate().lods[3]  # geometry: has selections, #Mass#, #UVSet#, properties
        properties = lod.properties()
        points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0), (0.5, 2.0, 0.0)]
        lod.set_mesh(points, [(0, 1, 2, 3), (3, 2, 4)], normals=[(0.0, 0.0, -1.0)] * 5,
                     uvs=[0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.5, 2.0])
        self.assertEqual(lod.properties(), properties)
        self.assertEqual(lod.selections(), [])
        self.assertIsNone(lod.masses)
        self.assertEqual(lod.uv_set_ids(), [])
        self.assertTrue(any(isinstance(t, MassTagg) for t in lod.dropped_taggs))
        self.assertEqual(list(lod.face_vertices), [0, 1, 2, 3, 3, 2, 4, 0])
        self.assertEqual(list(lod.face_normals), [0, 1, 2, 3, 3, 2, 4, 0])  # one normal per point
        self.assertEqual(lod.face(1).uvs, ((0.0, 1.0), (1.0, 1.0), (0.5, 2.0)))
        self.assertEqual(tuple(lod.face_uvs[14:16]), (0.0, 0.0))
        self.assertEqual(lod.check(), [])
        result = reread(P3DModel([lod])).lods[0]
        self.assertEqual(result.face(0).vertices, (0, 1, 2, 3))
        self.assertEqual(result.point(4), (0.5, 2.0, 0.0))

        # Flat points, one normal per corner, then no normals at all.
        lod.set_mesh([0.0] * 9, [(0, 1, 2)], normals=[0.0, 0.0, 1.0] * 3)
        self.assertEqual(list(lod.face_normals), [0, 1, 2, 0])
        lod.set_mesh([0.0] * 9, [(0, 1, 2)])
        self.assertEqual((lod.normal_count, list(lod.face_normals)), (0, [0, 0, 0, 0]))

    def test_bad_input_leaves_lod_unchanged(self):
        lod = triangle_lod()
        before = p3d.write_p3d(P3DModel([lod]))
        bad_calls = [
            dict(points=[0.0] * 9, polygons=[(0, 1)]),
            dict(points=[0.0] * 8, polygons=[(0, 1, 2)]),
            dict(points=[(0.0, 0.0)], polygons=[]),
            dict(points=[0.0] * 9, polygons=[(0, 1, -2)]),
            dict(points=[0.0] * 9, polygons=[(0, 1, 2)], uvs=[0.0]),
            dict(points=[0.0] * 9, polygons=[(0, 1, 2)], normals=[0.0] * 6),
            dict(points=[0.0] * 9, polygons=[(0, 1, 2)], normals=[0.0] * 3, normal_indices=[0]),
            dict(points=[0.0] * 9, polygons=[(0, 1, 2)], textures=[]),
            dict(points=[0.0] * 9, polygons=[(0, 1, 2)], point_flags=[0, -1, 0]),
        ]
        for kwargs in bad_calls:
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
                with self.assertRaises(ValueError):
                    lod.set_mesh(**kwargs)
                self.assertEqual(p3d.write_p3d(P3DModel([lod])), before)

    def test_add_point_speed_with_settle_check(self):
        lod = triangle_lod()
        for index in range(20):
            lod.add_selection("s%d" % index)
        lod.taggs.append(RawTagg("#Unknown#", b""))
        for _ in range(2000):
            lod.add_point((0.0, 0.0, 0.0))
        self.assertEqual(lod.structure_errors(), [])
        self.assertEqual(len(lod.selections()[0].vertex_weights), 2004)


if __name__ == "__main__":
    unittest.main()

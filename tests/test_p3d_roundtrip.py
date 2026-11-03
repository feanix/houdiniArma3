"""Lossless P3D round trips: read -> write reproduces the input byte for byte.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import struct
import tempfile
import time
import unittest
from array import array

from a3ob_core import binary, p3d
from a3ob_core.p3d import LOD, LodType, P3DModel, PropertyTagg, RawTagg, SelectionTagg, SharpEdgesTagg

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")
SAMPLES = ("sample_1_character.p3d", "sample_2_crate.p3d")

# 0x81 is undefined in cp1252: it decodes to U+0081 (not a lone surrogate, which crashes Houdini) and must still write
# back as 0x81.
ODD_NAME = binary.decode_string(b"sel\x81\xe9")
assert ODD_NAME == "sel\x81\xe9"


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as file:
        return file.read()


class TestFixtureRoundTrip(unittest.TestCase):
    def test_byte_identical(self):
        for name in SAMPLES:
            with self.subTest(fixture=name):
                data = fixture_bytes(name)
                start = time.perf_counter()
                model = p3d.read_p3d(data)
                output = p3d.write_p3d(model)
                elapsed = time.perf_counter() - start
                self.assertEqual(model.warnings, [])
                self.assertEqual(model.trailing, b"")
                self.assertEqual(len(output), len(data))
                self.assertTrue(output == data, "round trip of %s is not byte-identical" % name)
                # Generous bound; the 5.3 MB character takes about 0.1 s on a desktop CPU.
                self.assertLess(elapsed, 10.0)

    def test_file_api(self):
        path = os.path.join(FIXTURES, "sample_2_crate.p3d")
        model = P3DModel.read_file(path)
        with tempfile.TemporaryDirectory() as folder:
            out_path = os.path.join(folder, "out.p3d")
            model.write_file(out_path)
            with open(out_path, "rb") as file:
                self.assertEqual(file.read(), fixture_bytes("sample_2_crate.p3d"))
        self.assertEqual(model.to_bytes(), fixture_bytes("sample_2_crate.p3d"))


def _build_odd_model():
    """A model using every raw detail the format allows and Object Builder rarely writes."""
    lod = LOD(LodType.VISUAL, 0.3)
    lod.flags = 0xDEADBEEF
    for position, flags in (((0.0, 0.0, 0.0), 0x02000000), ((1.0, 0.0, 0.0), 0), ((0.0, 1.0, 0.0), 7),
                            ((0.0, 0.0, 1.0), 0xFFFFFFFF)):
        lod.add_point(position, flags)
    lod.add_normal((0.0, -1.0, 0.0))
    lod.add_face((0, 1, 2), (0, 0, 0), ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), 0x10, "tex\\a.paa", "mat\\b.rvmat")
    lod.add_face((0, 1, 2, 3), (0, 0, 0, 0), None, 0x01000000, "", "")
    # Garbage in the unused 4th slot of the triangle.
    lod.face_vertices[3] = 99
    lod.face_normals[3] = 98
    lod.face_uvs[6:8] = array(binary.F32, (12.5, -3.0))
    # Signalling NaN, negative zero and denormal bit patterns in a point.
    words = struct.pack("<3I", 0x7F800001, 0x80000000, 0x00000001)
    lod.points[9:12] = binary.array_from_bytes(binary.F32, words)

    lod.taggs.append(SharpEdgesTagg.from_pairs([(0, 1), (2, 2), (1, 0)]))
    lod.taggs.append(RawTagg("#Weird#", b"\x01\x02\x03\x04opaque", active=1))
    lod.taggs.append(RawTagg("#Lock#", bytes((1, 0, 0, 2, 0, 1)), active=0))  # inactive: A3OB dropped these
    junk = b"key\x00JUNK".ljust(64, b"\x00") + b"value\x00\xff\xfe".ljust(64, b"\x07")
    lod.taggs.append(PropertyTagg.from_payload(junk))
    lod.taggs.append(SelectionTagg(ODD_NAME, bytes((0, 1, 128, 255)), bytes((3, 0)), active=2))
    lod.taggs.append(RawTagg("#Selected#", bytes(6)))
    lod.eof_active = 0

    geometry = LOD(LodType.GEOMETRY)
    geometry.add_point((0.0, 0.0, 0.0))
    geometry.set_masses([12.5])

    model = P3DModel([lod, geometry])
    model.trailing = b"\x00trailing bytes"
    return model, words, junk


class TestOddDetailsRoundTrip(unittest.TestCase):
    def test_everything_survives(self):
        model, point_words, property_payload = _build_odd_model()
        first = p3d.write_p3d(model)
        self.assertIn(b"sel\x81\xe9\x00", first)
        self.assertIn(point_words, first)
        self.assertIn(property_payload, first)

        reread = p3d.read_p3d(first)
        self.assertEqual(p3d.write_p3d(reread), first)

        lod = reread.lods[0]
        self.assertEqual(lod.flags, 0xDEADBEEF)
        self.assertEqual(list(lod.point_flags), [0x02000000, 0, 7, 0xFFFFFFFF])
        self.assertEqual(lod.resolution, 0.3)
        self.assertEqual(lod.resolution_signature, p3d.f32(0.3))
        self.assertEqual((lod.face_vertices[3], lod.face_normals[3]), (99, 98))
        self.assertEqual(tuple(lod.face_uvs[6:8]), (12.5, -3.0))
        self.assertEqual(lod.face(0).vertices, (0, 1, 2))
        self.assertEqual(lod.face(1).vertices, (0, 1, 2, 3))
        self.assertEqual(lod.face(0).texture, "tex\\a.paa")
        self.assertEqual(lod.face_flags[1], 0x01000000)
        self.assertEqual([t.name for t in lod.taggs],
                         ["#SharpEdges#", "#Weird#", "#Lock#", "#Property#", ODD_NAME, "#Selected#"])
        self.assertEqual(lod.taggs[1].data, b"\x01\x02\x03\x04opaque")
        self.assertIsInstance(lod.taggs[2], p3d.MaskTagg)
        self.assertEqual((lod.taggs[2].points(), lod.taggs[2].faces()), ([0, 3], [1]))
        self.assertEqual(lod.taggs[2].active, 0)
        self.assertEqual(lod.taggs[4].active, 2)
        self.assertEqual(lod.eof_active, 0)
        self.assertEqual(lod.sharp_edges(), [(0, 1), (1, 0)])
        self.assertEqual(lod.sharp_edges(include_degenerate=True), [(0, 1), (2, 2), (1, 0)])
        self.assertEqual(lod.properties(), [("key", "value")])
        selection = lod.selection(ODD_NAME)
        self.assertEqual(bytes(selection.vertex_weights), bytes((0, 1, 128, 255)))
        self.assertEqual(bytes(selection.face_weights), bytes((3, 0)))
        self.assertEqual(reread.lods[1].lod_type, LodType.GEOMETRY)
        self.assertEqual(list(reread.lods[1].masses), [12.5])
        self.assertEqual(reread.trailing, b"\x00trailing bytes")
        self.assertEqual(len(reread.warnings), 1)  # the trailing bytes

    def test_edited_property_drops_junk(self):
        model, _, property_payload = _build_odd_model()
        model.lods[0].taggs[3].value = "other"
        data = p3d.write_p3d(model)
        self.assertNotIn(property_payload, data)
        self.assertIn(b"key".ljust(64, b"\x00") + b"other".ljust(64, b"\x00"), data)

    def test_no_sharp_edges_tagg_is_invented(self):
        # A3OB always wrote an empty #SharpEdges# (io/export_p3d.py:493).
        lod = LOD(LodType.MEMORY)
        lod.add_point((1.0, 2.0, 3.0))
        data = p3d.write_p3d(P3DModel([lod]))
        self.assertNotIn(b"#SharpEdges#", data)
        self.assertEqual(p3d.read_p3d(data).lods[0].taggs, [])


if __name__ == "__main__":
    unittest.main()

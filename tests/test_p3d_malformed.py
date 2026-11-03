"""Malformed and truncated P3D input raises clear errors and never loops.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import struct
import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LOD, LodType, P3DEOFError, P3DError, P3DModel, RawTagg

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")


def crate_bytes():
    with open(os.path.join(FIXTURES, "sample_2_crate.p3d"), "rb") as file:
        return file.read()


def tiny_model():
    lod = LOD(LodType.VISUAL, 1.0)
    for position in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        lod.add_point(position)
    lod.add_normal((0.0, 0.0, 1.0))
    lod.add_face((0, 1, 2))
    lod.add_selection("sel", {0: 1.0})
    lod.set_masses([1.0, 2.0, 3.0])
    return P3DModel([lod])


# Offsets in tiny_model()'s bytes.
LOD_START = 12
LOD_HEADER = LOD_START + 4          # version u32 x 2
COUNTS = LOD_START + 12             # points, normals, faces, flags
FACES = LOD_START + 28 + 3 * 16 + 12
TAGG_MAGIC = FACES + 72 + 2


class TestHeaders(unittest.TestCase):
    def test_empty_and_short(self):
        for data in (b"", b"ML", b"MLOD", b"MLOD\x01\x01\x00\x00"):
            with self.subTest(data=data), self.assertRaises(P3DEOFError):
                p3d.read_p3d(data)

    def test_bad_magic(self):
        data = bytearray(crate_bytes())
        data[0:4] = b"ODOL"
        with self.assertRaises(P3DError) as ctx:
            p3d.read_p3d(bytes(data))
        self.assertNotIsInstance(ctx.exception, P3DEOFError)
        self.assertIn("MLOD", str(ctx.exception))

    def test_bad_mlod_version(self):
        data = bytearray(crate_bytes())
        data[4:8] = struct.pack("<I", 256)
        with self.assertRaisesRegex(P3DError, "MLOD version 256"):
            p3d.read_p3d(bytes(data))

    def test_bad_lod_magic_and_version(self):
        data = bytearray(p3d.write_p3d(tiny_model()))
        bad = bytearray(data)
        bad[LOD_START:LOD_START + 4] = b"SP3X"
        with self.assertRaisesRegex(P3DError, "LOD 0.*signature"):
            p3d.read_p3d(bytes(bad))
        bad = bytearray(data)
        bad[LOD_HEADER:LOD_HEADER + 4] = struct.pack("<I", 27)
        with self.assertRaisesRegex(P3DError, "LOD version 27.256"):
            p3d.read_p3d(bytes(bad))

    def test_bad_tagg_magic(self):
        data = bytearray(p3d.write_p3d(tiny_model()))
        self.assertEqual(bytes(data[TAGG_MAGIC:TAGG_MAGIC + 4]), b"TAGG")
        data[TAGG_MAGIC:TAGG_MAGIC + 4] = b"GATT"
        with self.assertRaisesRegex(P3DError, "TAGG section signature"):
            p3d.read_p3d(bytes(data))

    def test_huge_counts_fail_fast(self):
        data = bytearray(p3d.write_p3d(tiny_model()))
        for offset in (8, COUNTS, COUNTS + 4, COUNTS + 8):
            bad = bytearray(data)
            bad[offset:offset + 4] = struct.pack("<I", 0xFFFFFFFF)
            with self.subTest(offset=offset), self.assertRaises(P3DEOFError):
                p3d.read_p3d(bytes(bad))

    def test_too_many_face_sides(self):
        data = bytearray(p3d.write_p3d(tiny_model()))
        data[FACES:FACES + 4] = struct.pack("<I", 5)
        with self.assertRaisesRegex(P3DError, "5 sides"):
            p3d.read_p3d(bytes(data))

    def test_end_of_file_tagg_with_payload(self):
        data = p3d.write_p3d(tiny_model())
        marker = b"#EndOfFile#\x00"
        at = data.index(marker) + len(marker)
        bad = data[:at] + struct.pack("<I", 4) + data[at + 4:]
        with self.assertRaisesRegex(P3DError, "#EndOfFile#"):
            p3d.read_p3d(bad)


class TestTruncation(unittest.TestCase):
    def test_every_cut_of_tiny_model_raises(self):
        data = p3d.write_p3d(tiny_model())
        for cut in range(len(data)):
            with self.subTest(cut=cut), self.assertRaises(P3DEOFError):
                p3d.read_p3d(data[:cut])

    def test_cuts_of_crate_raise(self):
        data = crate_bytes()
        cuts = list(range(0, len(data), 173)) + [len(data) - 1, len(data) - 4]
        for cut in cuts:
            with self.subTest(cut=cut), self.assertRaises(P3DError):
                p3d.read_p3d(data[:cut])

    def test_file_error_names_the_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "broken.p3d")
            with open(path, "wb") as file:
                file.write(crate_bytes()[:1000])
            with self.assertRaises(P3DEOFError) as ctx:
                p3d.read_p3d_file(path)
            self.assertIn("broken.p3d", str(ctx.exception))


def splice_tagg(data, name, payload, active=1):
    """Insert a TAGG record before the first #EndOfFile# record of written P3D bytes. Byte-level, because the writer
    refuses TAGGs that would not read back."""
    marker = b"#EndOfFile#\x00"
    position = data.index(marker) - 1  # the active byte of #EndOfFile#
    record = bytes((active,)) + name.encode("cp1252") + b"\x00" + struct.pack("<I", len(payload)) + payload
    return data[:position] + record + data[position:]


class TestBadTaggSizes(unittest.TestCase):
    def _with_bad_tagg(self, name, payload):
        return splice_tagg(p3d.write_p3d(tiny_model()), name, payload)

    def test_strict_raises_and_lenient_keeps_raw(self):
        cases = [
            ("#Mass#", b"\x00" * 8),                 # 3 points need 12 bytes
            ("#UVSet#", b"\x00" * 10),               # 4 + 8 x 3 corners = 28
            ("#Property#", b"\x00" * 100),           # must be 128
            ("#Property#", b"x" * 128),              # no terminators
            ("#SharpEdges#", b"\x00" * 12),          # not a multiple of 8
            ("another_sel", b"\x01" * 3),            # 3 points + 1 face = 4
        ]
        for name, payload in cases:
            with self.subTest(name=name, size=len(payload)):
                data = self._with_bad_tagg(name, payload)
                with self.assertRaisesRegex(P3DError, "TAGG"):
                    p3d.read_p3d(data)
                model = p3d.read_p3d(data, strict=False)
                self.assertEqual(len(model.warnings), 1)
                kept = model.lods[0].taggs[-1]
                self.assertIsInstance(kept, RawTagg)
                self.assertEqual((kept.name, kept.data), (name, payload))
                self.assertEqual(kept.counts, (3, 1, 3))
                self.assertEqual(p3d.write_p3d(model), data)
                # The same raw TAGG built by hand (no read counts) is refused instead of written.
                hand_made = tiny_model()
                hand_made.lods[0].taggs.append(RawTagg(name, payload))
                with self.assertRaisesRegex(P3DError, "would not read back"):
                    p3d.write_p3d(hand_made)

    def test_unknown_tagg_with_any_size_is_fine(self):
        data = self._with_bad_tagg("#Whatever#", b"\x00" * 3)
        model = p3d.read_p3d(data)
        self.assertEqual(model.warnings, [])
        self.assertEqual(p3d.write_p3d(model), data)


if __name__ == "__main__":
    unittest.main()

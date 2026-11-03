"""Real files (D6, task #29), M1 layer: every Arma 3 Samples .p3d reads in strict mode and writes back the same bytes.

Purpose: pin the advisor's triage (34 files, strict read, byte-identical write) so a change to the reader or writer that
breaks a Bohemia file shows up. Skips when the Samples are not on this machine (see ``tests/samples_support.py``).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import hashlib
import unittest

from a3ob_core import p3d

from tests import samples_support as ss


@unittest.skipUnless(ss.AVAILABLE, ss.SKIP_REASON)
class TestSamplesRoundTrip(unittest.TestCase):
    def test_there_are_files(self):
        self.assertGreaterEqual(len(ss.all_files()), 30)

    def test_every_file_reads_strictly_and_writes_back_identical_bytes(self):
        for rel in ss.all_files():
            with self.subTest(file=rel):
                with open(ss.path(rel), "rb") as file:
                    source = file.read()
                model = p3d.read_p3d(source, strict=True)
                written = p3d.write_p3d(model)
                self.assertEqual(len(written), len(source))
                self.assertEqual(hashlib.sha256(written).hexdigest(), hashlib.sha256(source).hexdigest())


    def test_no_string_is_non_ascii(self):
        """OQ-5: the Samples cannot settle the code page, because they hold no non-ASCII text."""
        checked = 0
        for rel in ss.all_files():
            model = p3d.read_p3d_file(ss.path(rel), strict=True)
            for lod in model.lods:
                texts = [tagg.name for tagg in lod.taggs] + lod.face_textures + lod.face_materials
                for tagg in lod.taggs:
                    if isinstance(tagg, p3d.PropertyTagg):
                        texts += [tagg.key, tagg.value]
                checked += len(texts)
                self.assertTrue(all(text.isascii() for text in texts), rel)
        self.assertGreater(checked, 100000)


if __name__ == "__main__":
    unittest.main()

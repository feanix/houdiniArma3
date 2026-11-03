"""Real files (D6, task #29), Houdini level: Arma 3 Samples through P3D Import, Extract LOD, Assemble LODs and P3D Export.

Purpose: find what Bohemia's own Object Builder files do that the synthetic fixtures never did. The default run uses one
file per Addons folder; ``A3OB_SAMPLES_FULL=1`` runs all 34. Skips without ``hou`` or without the Samples.

A file that does not come back as the same bytes is not a failure by itself. Houdini geometry cannot hold a few things a
real file has (the layout of the normal table, the unused slot of a triangle, the order of the sharp edge pairs, the order
of points and faces when proxies are interleaved). The test fails when a LOD differs in anything else. See
docs/design/evidence/samples/samples_findings.md.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from tests import hou_support as hs
from tests import samples_support as ss

if hs.HAS_HOU:
    from a3ob_houdini import messages

#: Files that must stay byte-identical (they are in the Samples, small, and come back exactly today).
EXACT = ("Addons/TemplateRTM/ManSkeleton_Pivots.p3d", "Addons/Test_Heli_01/Main_Rotor_F.p3d",
         "Addons/Test_Character_01/A3_headgear_template.p3d", "Addons/Test_Character_01/A3_vest_template.p3d")
#: The time limit of ux-tools.md / task #29, seconds, for one import or one export.
SLOW = 10.0


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
@unittest.skipUnless(ss.AVAILABLE, ss.SKIP_REASON)
class TestSamplesInHoudini(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = ss.Rig()

    @classmethod
    def tearDownClass(cls):
        cls.rig.close()

    def clear(self):
        self.rig.container.deleteItems(self.rig.container.children())

    def direct(self, rel):
        """The import then Lossless export of a file, made once per run."""
        if rel not in self.cache:
            self.clear()
            self.cache[rel] = self.rig.roundtrip(rel)
        return self.cache[rel]

    cache = {}

    def test_import_to_lossless_export(self):
        for rel in ss.houdini_files():
            with self.subTest(file=rel):
                r = self.direct(rel)
                self.assertEqual(r["import_errors"], [])
                self.assertEqual(r["report"].texts(messages.ERROR), [])
                self.assertIsNotNone(r["written"], "nothing was written")
                self.assertLess(r["import_seconds"], SLOW)
                self.assertLess(r["export_seconds"], SLOW)
                if r["written"] == r["source"]:
                    continue
                self.assertNotIn(rel, EXACT, "this file used to come back identical")
                interleaved = ss.interleaved_lod_names(r["import_warnings"])
                rows, unexplained = ss.compare_files(r["source"], r["written"], interleaved)
                self.assertTrue(rows)
                self.assertEqual(unexplained, [], "differs where %s" % ss.locate(
                    r["source"], ss.first_difference(r["written"], r["source"])))

    def test_exact_files_are_identical(self):
        for rel in EXACT:
            if rel not in ss.all_files():
                continue
            with self.subTest(file=rel):
                r = self.direct(rel)
                self.assertEqual(ss.sha256(r["written"]), ss.sha256(r["source"]))

    def test_extract_and_assemble_without_edits_change_nothing(self):
        """Import, Extract LOD k for every k, Assemble LODs on the import: the same bytes as import then export."""
        for rel in ss.houdini_files():
            with self.subTest(file=rel):
                direct = self.direct(rel)
                self.clear()
                through = self.rig.roundtrip(rel, via_assemble=True)
                self.assertEqual(through["assemble_errors"], [])
                self.assertEqual(through["report"].texts(messages.ERROR), [])
                self.assertEqual(ss.sha256(through["written"]), ss.sha256(direct["written"]))
                self.assertLess(through["assemble_seconds"], SLOW)

    def test_release_export_runs_without_errors(self):
        for rel in ss.houdini_files():
            with self.subTest(file=rel):
                self.clear()
                r = self.rig.roundtrip(rel, "Arma 3 release")
                self.assertEqual(r["report"].texts(messages.ERROR), [])
                self.assertIsNotNone(r["written"])
                self.assertLess(r["export_seconds"], SLOW)

    def test_release_export_follows_bohemias_files_d24(self):
        """D24 and D25: the rules that fired on Bohemia's own files no longer warn; the unchanged rules still do.
        What it can catch: a rule that comes back, or one of the four kept rules going quiet. The unchanged rules are
        only expected on a full run (the default subset may not hold the files that trigger them)."""
        warnings, notes = [], []
        for rel in ss.houdini_files():
            self.clear()
            r = self.rig.roundtrip(rel, "Arma 3 release")
            warnings.extend(r["report"].texts(messages.WARNING))
            notes.extend(r["report"].texts(messages.INFO))
        for gone in ("T2-SHADOWOFF", "T2-SHADOWRES", "T2-NOMAT", "T2-MASS"):
            self.assertEqual([w for w in warnings if "(rule %s)" % gone in w], [], gone)
        self.assertEqual([w for w in warnings if "T2-TRI" in w and "not convex" not in w and "points" not in w], [])
        self.assertEqual([w for w in warnings if "a3ob_face_flags that Object Builder does not know" in w], [])
        if ss.FULL:
            for kept in ("T2-MASSALL", "T2-CONTIG", "T2-CONVEX", "T2-RW-LIMIT"):
                self.assertTrue([w for w in warnings if "(rule %s)" % kept in w], kept)
            self.assertTrue([n for n in notes if "(rule T2-MASS)" in n])


if __name__ == "__main__":
    unittest.main()

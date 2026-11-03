"""The Validate and P3D Export assets load from houdini/otls and work in a real hython (D13, ux-tools.md 5.8 and 5.9).

Same method as test_hda_lod_nodes.py: a child hython with HOUDINI_PATH including <repo>/houdini presses Save to Disk
with ``pressButton``. Needs ``hou``; skipped under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT

CHILD = os.path.join(REPO_ROOT, "tests", "hda_child_check_export.py")


def run_child(scratch):
    environment = dict(os.environ)
    environment["HOUDINI_PATH"] = os.path.join(REPO_ROOT, "houdini").replace("\\", "/") + ";&"
    environment["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    done = subprocess.run([sys.executable, CHILD, FIXTURES, scratch], env=environment, capture_output=True, text=True,
                          timeout=600)
    lines = [line for line in done.stdout.splitlines() if line.startswith("A3OB_RESULT ")]
    if not lines:
        raise AssertionError("the child hython printed no result (exit %d)\n%s\n%s" % (
            done.returncode, done.stdout[-3000:], done.stderr[-3000:]))
    return json.loads(lines[0][len("A3OB_RESULT "):])


def rule_lines(texts, rule_id=None):
    marker = "(rule %s)" % rule_id if rule_id else "(rule T2-"
    return [text for text in texts if marker in text]


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestExportAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.mkdtemp(prefix="a3ob_export_nodes_")
        try:
            cls.result = run_child(cls.scratch)
        except Exception:
            shutil.rmtree(cls.scratch, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_assets_load_from_the_folders_with_the_table_parameters(self):
        types = self.result["types"]
        validate = types["a3ob_validate"]
        self.assertEqual(validate["label"], "Validate")
        self.assertEqual(validate["inputs"], [1, 1])
        # The tabs are folder templates (Houdini names the later tabs of a row itself): leave them out.
        validate["parms"] = [name for name in validate["parms"] if not name.startswith("tab_")]
        self.assertEqual(validate["parms"][:4], ["errorlevel", "strict", "dupes", "maxmessages"])
        self.assertEqual(len(validate["parms"]), 4 + 1 + 20)
        self.assertEqual(validate["pythoncook"], "import a3ob_houdini.entry as e; e.cook('validate', hou.pwd())")
        self.assertEqual(validate["submenu"], [["A3OB"]])
        export = types["a3ob_p3d_export"]
        self.assertEqual(export["label"], "P3D Export")
        self.assertEqual(export["inputs"], [1, 1])
        export["parms"] = [name for name in export["parms"] if not name.startswith("tab_")]
        self.assertEqual(export["parms"][:4], ["file", "save", "backup", "check"])
        for name in ("errorlevel", "strict", "dupes", "maxmessages", "selmask", "lowercase", "relpaths", "projectroot",
                     "sortsections", "sortlods", "autocomponents", "backup", "typerules", "t2tri"):
            self.assertIn(name, export["parms"])
        self.assertEqual(export["pythoncook"], "import a3ob_houdini.entry as e; e.cook('p3d_export', hou.pwd())")
        self.assertTrue(export["library"].endswith("houdini/otls/a3ob_p3d_export.hda"), export["library"])

    def test_save_to_disk_writes_a_byte_identical_file(self):
        for key in ("crate", "character"):
            with self.subTest(fixture=key):
                self.assertTrue(self.result[key + "_identical"])
                self.assertEqual(self.result[key + "_files"], ["out.p3d"])  # no temporary file is left
                self.assertFalse(self.result[key + "_cook_wrote"])  # cooking never writes
                self.assertEqual(self.result[key + "_save"]["errors"], [])
                self.assertTrue(any(text.startswith("Saved ") for text in self.result[key + "_save"]["messages"]),
                                self.result[key + "_save"]["messages"])
                self.assertEqual(self.result[key + "_cook"]["errors"], [])
                self.assertTrue(any("KB if saved now" in text for text in self.result[key + "_cook"]["messages"]))
                prims, source = self.result[key + "_prims"]
                self.assertEqual(prims, source)  # the output is the input
        self.assertIn("9 LODs", " ".join(self.result["character_save"]["messages"]))
        self.assertEqual(self.result["character_save"]["warnings"], [])  # no false alarm on a real file
        self.assertEqual(self.result["character_cook"]["warnings"], [])

    def test_backup_keeps_the_old_file(self):
        self.assertEqual(self.result["backup_files"], ["out.p3d", "out.p3d.bak"])

    def test_a_five_sided_polygon_writes_nothing_and_shows_the_error(self):
        r = self.result
        self.assertTrue(any("more than 4 points" in text for text in r["bad_cook"]["errors"]), r["bad_cook"])
        self.assertEqual(r["bad_geometry_points"], 0)  # Error (stop here): no geometry
        self.assertTrue(any("more than 4 points" in text for text in r["bad_save"]["errors"]), r["bad_save"])
        self.assertEqual(r["bad_files"], ["bad.p3d"])  # no temporary file
        self.assertTrue(r["bad_old_file_kept"])
        self.assertIn("more than 4 points", r["script_save"])  # the script function raises (ux-tools.md 5.9.1)
        self.assertFalse(r["bad2_exists"])  # nothing was made for a failed check
        self.assertTrue(r["bad2_errors"])

    def test_validate_and_export_give_the_same_messages(self):
        r = self.result
        self.assertEqual(r["validate_bad"]["errors"], r["bad_cook"]["errors"])
        self.assertEqual(r["validate_bad"]["warnings"], r["bad_cook"]["warnings"])
        self.assertEqual(r["validate_bad_soft"]["warnings"], r["export_bad_soft"]["warnings"])
        self.assertEqual(r["validate_crate"]["warnings"], self.result["crate_cook"]["warnings"])
        self.assertTrue(r["validate_crate"]["warnings"])  # the crate has real tier-2 findings
        self.assertEqual(r["validate_crate"]["errors"], [])

    def test_validate_levels(self):
        r = self.result
        self.assertEqual(r["validate_bad_points"], 0)
        self.assertEqual(r["validate_bad_soft"]["errors"], [])
        self.assertTrue(any(text.startswith("Error: ") and "more than 4 points" in text
                            for text in r["validate_bad_soft"]["warnings"]))
        self.assertGreater(r["validate_bad_soft_points"], 0)  # Warning (keep geometry): the geometry flows
        self.assertTrue(any("0 LODs checked" in text or "LODs checked: 1 errors" in text
                            for text in r["validate_bad"]["messages"]), r["validate_bad"]["messages"])
        self.assertTrue(any("7 LODs checked: 0 errors" in text for text in r["validate_crate"]["messages"]),
                        r["validate_crate"]["messages"])
        self.assertEqual(r["validate_crate_prims"][0], r["validate_crate_prims"][1])

    def test_soft_level_export_still_refuses_to_save(self):
        r = self.result
        self.assertFalse(r["soft_exists"])
        self.assertEqual(r["export_bad_soft_after_save"]["errors"], [])
        self.assertTrue(any("more than 4 points" in text for text in r["export_bad_soft_after_save"]["warnings"]))

    def test_each_tier2_rule_has_a_switch_on_the_node(self):
        r = self.result
        self.assertTrue(rule_lines(r["validate_crate"]["warnings"], "T2-RW-SOUND"))
        self.assertEqual(rule_lines(r["validate_crate_rules_off"]["warnings"]), [])
        # D24: T2-MASS is an info note (the node messages), not a warning.
        self.assertTrue(rule_lines(r["validate_no_mass"]["messages"], "T2-MASS"))
        self.assertFalse(rule_lines(r["validate_no_mass"]["warnings"], "T2-MASS"))
        self.assertFalse(rule_lines(r["validate_no_mass_mass_off"]["messages"], "T2-MASS"))
        self.assertTrue(rule_lines(r["validate_no_mass_mass_off"]["warnings"], "T2-MASSALL"))  # only its own switch

    def test_mass_rules_check_the_geometry_lod_only(self):
        """D16: the crate's Buoyancy LOD has no mass and gives no mass warning; a Geometry LOD without mass still does."""
        r = self.result
        self.assertEqual(rule_lines(r["validate_crate"]["warnings"], "T2-MASS"), [])
        self.assertEqual(rule_lines(r["validate_crate"]["warnings"], "T2-MASSALL"), [])
        self.assertEqual(rule_lines(r["crate_cook"]["warnings"], "T2-MASS"), [])
        self.assertEqual(len(rule_lines(r["validate_no_mass"]["messages"], "T2-MASS")), 1)
        self.assertEqual(len(rule_lines(r["validate_no_mass"]["warnings"], "T2-MASSALL")), 1)

    def test_warnings_can_count_as_errors(self):
        r = self.result
        self.assertTrue(r["validate_crate_strict"]["errors"])
        self.assertTrue(all(text.startswith("Warning as error: ") for text in r["validate_crate_strict"]["errors"]))
        self.assertEqual(r["strict_files"], [])
        self.assertTrue(r["strict_errors"])

    def test_max_messages(self):
        shown = self.result["validate_crate_two"]
        self.assertLessEqual(len(shown["errors"]) + len(shown["warnings"]) + len(shown["messages"]), 2)
        self.assertTrue(any("more messages" in text for text in shown["messages"]), shown)

    def test_check_when_saving_does_not_check_on_cook(self):
        r = self.result
        self.assertTrue(any("Not checked while cooking" in text for text in r["late_cook"]["messages"]))
        self.assertTrue(r["late_exists"])
        self.assertEqual(r["late_after"]["errors"], [])

    def test_no_input_is_an_error(self):
        self.assertTrue(self.result["lonely"]["errors"])


if __name__ == "__main__":
    unittest.main()

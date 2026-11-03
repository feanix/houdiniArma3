"""The P3D Import asset loads from houdini/otls and cooks in a real hython (D13).

The test starts a child hython with HOUDINI_PATH including <repo>/houdini, the way a user's package would. The
variable is set for the child only, so nothing leaks into this process or into the user's Houdini preferences.
Needs ``hou`` (the parent must itself be hython); skipped under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT

CHILD = os.path.join(REPO_ROOT, "tests", "hda_child_check.py")


def run_child():
    """Run the child script and return its parsed result dict."""
    environment = dict(os.environ)
    environment["HOUDINI_PATH"] = os.path.join(REPO_ROOT, "houdini").replace("\\", "/") + ";&"
    environment["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    with tempfile.TemporaryDirectory() as scratch:
        done = subprocess.run(
            [sys.executable, CHILD, os.path.join(FIXTURES, "sample_1_character.p3d"),
             os.path.join(FIXTURES, "sample_2_crate.p3d"), scratch],
            env=environment, capture_output=True, text=True, timeout=300)
    lines = [line for line in done.stdout.splitlines() if line.startswith("A3OB_RESULT ")]
    if not lines:
        raise AssertionError("the child hython printed no result (exit %d)\n%s\n%s" % (
            done.returncode, done.stdout[-2000:], done.stderr[-2000:]))
    return json.loads(lines[0][len("A3OB_RESULT "):])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestHdaInHython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_child()

    def test_asset_loads_from_the_folder_with_the_table_parameters(self):
        r = self.result
        self.assertTrue(r["type_found"])
        self.assertTrue(r["library"].replace("\\", "/").endswith("houdini/otls/a3ob_p3d_import.hda"), r["library"])
        self.assertEqual(r["label"], "P3D Import")
        self.assertEqual(r["inputs"], [0, 0])
        self.assertEqual(r["parms"], ["file", "reload", "lods", "facegroups"])
        self.assertEqual(r["submenu"], [["A3OB"]])

    def test_python_cook_is_one_line_into_the_package(self):
        cook = self.result["pythoncook"].strip()
        self.assertEqual(cook.count("\n"), 0)
        self.assertEqual(cook, "import a3ob_houdini.entry as e; e.cook('p3d_import', hou.pwd())")

    def test_character_cooks_without_errors_and_with_nine_lods(self):
        r = self.result
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(len(r["prim_names"]), 9)
        self.assertEqual(r["prim_names"][0], "Resolution 1")
        self.assertTrue(r["messages"][0].startswith("9 LODs, 43 packed proxies, 0 inline proxies"), r["messages"])
        print("\nsample_1_character.p3d HDA cook: %.2f s" % r["cook_seconds"])

    def test_lod_uid_is_stable_across_recooks_and_differs_between_nodes(self):
        r = self.result
        self.assertNotEqual(r["paths"][0], r["paths"][1])
        # Memory (all four faces are packed proxies), Land Contact and Hit-points have no polygons to carry a uid.
        self.assertEqual(r["uids_missing"], [5, 6, 7])
        self.assertTrue(r["uids_stable"])
        self.assertTrue(r["uids_differ_between_nodes"])
        self.assertTrue(r["uids_differ_between_lods"])

    def test_problems_become_node_errors_in_the_ux_form(self):
        r = self.result
        self.assertEqual(len(r["empty_file_errors"]), 1)
        self.assertIn("no file is chosen", r["empty_file_errors"][0])
        self.assertIn("does not exist", r["missing_file_errors"][0])
        self.assertIn("cannot be read", r["garbage_file_errors"][0])
        self.assertTrue(r["garbage_geometry_empty"])
        self.assertIn("runs backwards", r["bad_lods_errors"][0])

    def test_partial_import_warns_and_reload_button_works(self):
        r = self.result
        self.assertEqual(r["partial_errors"], [])
        self.assertEqual(r["partial_prims"], 3)
        self.assertIn("4 of 7 LODs were not imported", r["partial_warnings"][0])
        self.assertEqual(r["reload_errors"], [])
        self.assertEqual(r["reload_prims"], 7)

    def test_python_sop_fallback_uses_the_same_table_and_entry(self):
        self.assertEqual(self.result["fallback_errors"], [])
        self.assertEqual(self.result["fallback_prims"], 7)

    def test_a_kind_the_loaded_code_does_not_know_is_a_node_error_with_the_fix(self):
        # Stale code in a long session (task 13): not a "bug in the A3OB tools", a message that says what to do.
        errors = self.result["bug_errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("no_such_kind", errors[0])
        self.assertIn("not in the A3OB code that is loaded", errors[0])
        self.assertIn("Reload A3OB", errors[0])


if __name__ == "__main__":
    unittest.main()

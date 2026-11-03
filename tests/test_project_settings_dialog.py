"""Pure helpers of the Project root and presets dialog (task 25). Plain Python, no hou.ui.

Purpose: catch a Cancel that still saves (wrong "values from indices" or summary) and a settings path shown with mixed
slashes.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest
from unittest import mock

from a3ob_houdini import project_settings_dialog as dlg, settings as sm


class TestSummary(unittest.TestCase):
    def test_lists_exactly_the_ticked_options(self):
        options = ["lowercase", "sort_lods", "relative_paths"]
        text = dlg.preset_summary("mine", options, (0, 2))
        self.assertIn("\"mine\"", text)
        self.assertIn("lowercase", text)
        self.assertIn("relative_paths", text)
        self.assertNotIn("sort_lods", text)
        self.assertNotIn("all off", text)

    def test_none_ticked_says_all_off(self):
        self.assertIn("all off", dlg.preset_summary("mine", ["lowercase"], ()))


class TestValuesFromIndices(unittest.TestCase):
    def test_every_bool_preset_field_is_mapped(self):
        options = dlg.bool_options({})
        self.assertTrue(options)
        for field in options:
            self.assertIn(field, sm.PRESET_KEYS)
        every = dlg.values_from_indices(options, range(len(options)))
        self.assertEqual(set(every), set(options))
        self.assertTrue(all(value is True for value in every.values()))
        none = dlg.values_from_indices(options, ())
        self.assertEqual(set(none), set(options))
        self.assertTrue(all(value is False for value in none.values()))

    def test_only_chosen_indices_are_on(self):
        options = dlg.bool_options({})
        values = dlg.values_from_indices(options, [1])
        self.assertEqual([f for f, v in values.items() if v], [options[1]])


class TestDisplayPath(unittest.TestCase):
    def test_no_mixed_slashes(self):
        shown = dlg.display_path("C:/Users/me/Documents/houdini22.0\\a3ob_settings.json")
        if os.sep == "\\":  # on posix a backslash is a name character
            self.assertNotIn("/", shown)
        self.assertEqual(shown, os.path.normpath(shown))

    def test_settings_path_display(self):
        with mock.patch.dict(os.environ, {sm.ENV_FILE: "C:/Users/me/houdini22.0\\a3ob_settings.json"}):
            shown = dlg.display_path(sm.settings_path())
        if os.sep == "\\":
            self.assertNotIn("/", shown)

    def test_empty_stays_empty(self):
        self.assertEqual(dlg.display_path(""), "")


if __name__ == "__main__":
    unittest.main()

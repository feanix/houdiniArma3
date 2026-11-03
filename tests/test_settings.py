"""Settings, presets, project root and common data (ux-tools.md 7.2, D14 Q-5 and Q-7). Plain Python, no Houdini.

Purpose: catch a wrong built-in preset (it would change exported files), a crash on a bad settings file (the artist
could not start a tool), and an environment override that is ignored.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from a3ob_houdini import settings as sm
from a3ob_houdini.errors import UserError
from a3ob_houdini.nodes import material_assign
from a3ob_houdini.p3d_export import ExportOptions


class Scratch(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, "a3ob_settings.json")
        clean = {key: value for key, value in os.environ.items() if key not in (sm.ENV_FILE, sm.ENV_ROOT)}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(text)


class TestBuiltInPresets(unittest.TestCase):
    def test_lossless_is_the_export_default_and_all_off(self):
        options = ExportOptions(**sm.BUILTIN_PRESETS[sm.LOSSLESS])
        self.assertEqual(options, ExportOptions())
        for key in ("lowercase", "relative_paths", "sort_sections", "sort_lods", "auto_components"):
            self.assertFalse(getattr(options, key), key)

    def test_release_is_a3ob_defaults_from_ux_5_9(self):
        options = ExportOptions(**sm.BUILTIN_PRESETS[sm.RELEASE])
        self.assertTrue(options.relative_paths and options.sort_sections and options.lowercase and options.auto_components)
        self.assertEqual(options.duplicate_lods, "fail")
        self.assertFalse(options.sort_lods)

    def test_every_preset_key_is_an_export_option(self):
        fields = set(ExportOptions.__dataclass_fields__)
        for preset in sm.BUILTIN_PRESETS.values():
            self.assertLessEqual(set(preset), fields)
        self.assertNotIn("project_root", sm.PRESET_KEYS)


class TestFileAndEnvironment(Scratch):
    def test_missing_file_gives_defaults_without_problems(self):
        settings, problems = sm.load(self.path)
        self.assertEqual((settings.project_root, settings.active_preset, problems), ("P:\\", sm.LOSSLESS, []))

    def test_corrupt_file_gives_defaults_and_a_warning_and_is_not_overwritten(self):
        self.write("{not json")
        settings, problems = sm.load(self.path)
        self.assertEqual(settings.active_preset, sm.LOSSLESS)
        self.assertEqual(len(problems), 1)
        with open(self.path, encoding="utf-8") as file:
            self.assertEqual(file.read(), "{not json")

    def test_json_that_is_not_an_object(self):
        self.write("[1, 2]")
        self.assertEqual(len(sm.load(self.path)[1]), 1)

    def test_file_with_bad_bytes(self):
        with open(self.path, "wb") as file:
            file.write(b"\xff\xfe\x00bad")
        self.assertEqual(len(sm.load(self.path)[1]), 1)

    def test_env_overrides_the_file_location(self):
        os.environ[sm.ENV_FILE] = self.path
        self.assertEqual(sm.settings_path(), os.path.abspath(self.path))

    def test_default_location_uses_the_pref_dir_variable(self):
        os.environ["HOUDINI_USER_PREF_DIR"] = self.folder.name
        self.assertEqual(sm.settings_path(), os.path.join(self.folder.name, sm.FILE_NAME))

    def test_env_overrides_the_project_root(self):
        settings = sm.Settings(project_root="D:\\mod")
        self.assertEqual(sm.effective_root(settings), "D:\\mod")
        os.environ[sm.ENV_ROOT] = "Q:\\x"
        self.assertEqual(sm.effective_root(settings), "Q:\\x")
        self.assertEqual(sm.options_for(settings).project_root, "Q:\\x")

    def test_unknown_keys_are_dropped_from_presets_and_kept_at_top_level(self):
        self.write(json.dumps({"future": {"a": 1}, "project_root": "D:\\p", "presets": {
            "mine": {"lowercase": True, "colour": 3, "sort_lods": "yes"}, "Lossless": {"lowercase": True},
            "broken": 5}}))
        settings, problems = sm.load(self.path)
        self.assertEqual(settings.presets["mine"], {"lowercase": True})
        self.assertEqual(settings.presets["broken"], {})
        self.assertNotIn(sm.LOSSLESS, settings.presets)
        self.assertEqual(len(problems), 4)
        sm.save(settings, self.path)
        with open(self.path, encoding="utf-8") as file:
            saved = json.load(file)
        self.assertEqual(saved["future"], {"a": 1})
        self.assertEqual(saved["project_root"], "D:\\p")

    def test_wrong_types_of_plain_settings_are_ignored(self):
        self.write(json.dumps({"project_root": 5, "active_preset": ["x"]}))
        settings, problems = sm.load(self.path)
        self.assertEqual((settings.project_root, len(problems)), ("P:\\", 2))

    def test_save_and_load_round_trip(self):
        settings = sm.Settings()
        sm.set_project_root(settings, "D:\\mod")
        sm.save_preset(settings, "mine", ExportOptions(lowercase=True, duplicate_lods="fail"))
        sm.set_active_preset(settings, "mine")
        sm.save(settings, os.path.join(self.folder.name, "sub", "s.json"))
        again, problems = sm.load(os.path.join(self.folder.name, "sub", "s.json"))
        self.assertEqual(problems, [])
        self.assertEqual(again.project_root, "D:\\mod")
        self.assertTrue(sm.options_for(again).lowercase)
        self.assertEqual(sm.preset_names(again), [sm.LOSSLESS, sm.RELEASE, "mine"])

    def test_rule_switches_survive_save_and_load(self):
        # rules_off is a tuple in ExportOptions and a list in JSON.
        settings = sm.Settings()
        sm.save_preset(settings, "quiet", ExportOptions(rules_off=("T2-MASS", "T2-UV2")))
        sm.set_active_preset(settings, "quiet")
        path = os.path.join(self.folder.name, "rules.json")
        sm.save(settings, path)
        again, problems = sm.load(path)
        self.assertEqual(problems, [])
        self.assertEqual(sm.options_for(again).rules_off, ("T2-MASS", "T2-UV2"))

    def test_save_into_a_blocked_place_is_a_user_error(self):
        blocker = os.path.join(self.folder.name, "file")
        open(blocker, "w").close()
        with self.assertRaises(UserError):
            sm.save(sm.Settings(), os.path.join(blocker, "s.json"))


class TestPresetEditing(unittest.TestCase):
    def test_builtin_names_are_protected(self):
        settings = sm.Settings()
        with self.assertRaises(UserError):
            sm.save_preset(settings, sm.RELEASE, ExportOptions())
        with self.assertRaises(UserError):
            sm.delete_preset(settings, sm.LOSSLESS)

    def test_unknown_preset_name_lists_the_choices(self):
        with self.assertRaises(UserError) as caught:
            sm.get_preset(sm.Settings(), "nope")
        self.assertIn(sm.RELEASE, str(caught.exception))

    def test_deleting_the_active_preset_falls_back_to_lossless(self):
        settings = sm.Settings()
        sm.save_preset(settings, "mine", {"lowercase": True})
        sm.set_active_preset(settings, "mine")
        sm.delete_preset(settings, "mine")
        self.assertEqual(settings.active_preset, sm.LOSSLESS)

    def test_bad_value_in_a_saved_preset_is_refused(self):
        with self.assertRaises(UserError):
            sm.save_preset(sm.Settings(), "x", {"duplicate_lods": "maybe"})
        with self.assertRaises(UserError):
            sm.save_preset(sm.Settings(), " ", {})

    def test_empty_root_is_refused(self):
        with self.assertRaises(UserError):
            sm.set_project_root(sm.Settings(), "  ")


class TestCommonData(Scratch):
    def test_builtin_lists_without_a_file(self):
        data, problems = sm.common_data(sm.Settings())
        self.assertEqual(problems, [])
        self.assertIn("PIP", data["procedurals"])
        self.assertIn("Glass", data["materials"])

    def test_file_adds_and_overrides_and_bad_entries_are_reported(self):
        self.write(json.dumps({"procedurals": {"Red": "#(argb,8,8,3)color(1,0,0,1,CO)", "PIP": "mine", "Bad": 3},
                               "materials": [1]}))
        data, problems = sm.common_data(sm.Settings(common_data=self.path))
        self.assertEqual(data["procedurals"]["PIP"], "mine")
        self.assertIn("Red", data["procedurals"])
        self.assertEqual(len(problems), 2)

    def test_missing_or_corrupt_file_keeps_the_builtins(self):
        data, problems = sm.common_data(sm.Settings(common_data=self.path))
        self.assertEqual(len(problems), 1)
        self.assertIn("Glass", data["materials"])
        self.write("{")
        self.assertEqual(len(sm.common_data(sm.Settings(common_data=self.path))[1]), 1)


class FakeParm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeNode:
    def __init__(self):
        self.parms = {name: FakeParm(value) for name, value in
                      (("raw", ""), ("texsource", "file"), ("material", ""), ("setmat", 0))}

    def parm(self, name):
        return self.parms[name]


class TestMaterialAssignMenus(Scratch):
    def setUp(self):
        super().setUp()
        os.environ[sm.ENV_FILE] = self.path

    def test_menu_items_start_with_choose_and_include_the_file(self):
        common = os.path.join(self.folder.name, "common.json")
        with open(common, "w", encoding="utf-8") as file:
            json.dump({"procedurals": {"Red": "x"}}, file)
        self.write(json.dumps({"common_data": common}))
        items = material_assign.MENUS["commonproc"]({})
        self.assertEqual(items[0], ("none", "(choose)"))
        self.assertIn(("Red", "Red"), items)
        self.assertIn(("PIP", "PIP"), items)

    def test_choosing_fills_the_parameter_and_resets_the_menu(self):
        node = FakeNode()
        pick = FakeParm("PIP")
        material_assign.BUTTONS["fillproc"]({"node": node, "parm": pick})
        self.assertEqual(node.parm("raw").eval(), "#(argb,512,512,1)r2t(rendertarget0,1.0)")
        self.assertEqual((node.parm("texsource").eval(), pick.eval()), ("custom", "none"))
        pick = FakeParm("Glass")
        material_assign.BUTTONS["fillmat"]({"node": node, "parm": pick})
        self.assertEqual(node.parm("material").eval(), "\\a3\\data_f\\glass_veh.rvmat")
        self.assertEqual((node.parm("setmat").eval(), pick.eval()), (1, "none"))

    def test_choose_changes_nothing(self):
        node = FakeNode()
        material_assign.BUTTONS["fillmat"]({"node": node, "parm": FakeParm("none")})
        self.assertEqual(node.parm("material").eval(), "")

    def test_existing_parameter_names_are_unchanged(self):
        names = [parm.name for parm in material_assign.SPEC.parms]
        for old in ("group", "grouptype", "settex", "texsource", "texture", "color", "colortype", "raw", "setmat",
                    "material"):
            self.assertIn(old, names)


if __name__ == "__main__":
    unittest.main()

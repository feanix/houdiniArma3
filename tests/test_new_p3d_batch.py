"""New P3D, Batch folder convert and the P3D Export presets (ux-tools.md 5.9, 7.4, 7.5). Needs hou; skips under CPython.

Purpose: catch a starter network that does not cook or that Validate/Export reject (the promise of 7.5), a batch
convert that reports "identical" for a file that changed (or the reverse), that writes into the source folder, or that
stops on one bad file, and a preset Apply that renames, misses or mis-sets a parameter.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import ast
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tests import hou_support as hs
from tests.mass_proxy_support import NodeTestCase, cook

if hs.HAS_HOU:
    import hou
    from a3ob_core import p3d
    from a3ob_houdini import batch_convert as bc, new_p3d, settings as sm
    from a3ob_houdini.errors import UserError
    from a3ob_houdini.nodes import p3d_export as export_node


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestNewP3d(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.made = new_p3d.build_starter(self.network)
        self.nodes = self.made.nodes

    def test_every_node_cooks_without_errors(self):
        for key, node in self.nodes.items():
            result = cook(node)
            self.assertEqual(result.errors, (), key)
        self.assertEqual(set(self.nodes), set(new_p3d.NODE_NAMES))

    def test_lods_are_resolution_1_geometry_and_memory(self):
        geo = self.nodes["assemble"].geometry()
        names = [prim.attribValue("name") for prim in geo.prims()]
        self.assertEqual(names, ["Resolution 1", "Geometry", "Memory"])

    def test_validate_and_export_report_no_errors(self):
        validate = self.network.createNode("a3ob_validate")
        validate.setFirstInput(self.nodes["assemble"])
        self.assertEqual(cook(validate).errors, ())
        export = self.nodes["export"]
        self.assertEqual(cook(export).errors, ())
        self.assertEqual(export.parm("file").unexpandedString(), "$HIP/%s.p3d" % self.network.name())

    def test_save_to_disk_works_on_the_first_try(self):
        folder = tempfile.mkdtemp(prefix="a3ob_newp3d_")
        self.addCleanup(shutil.rmtree, folder, True)
        export = self.nodes["export"]
        path = os.path.join(folder, "new.p3d")
        export.parm("file").set(path.replace("\\", "/"))
        export_node.save(export)
        model = p3d.read_p3d_file(path, strict=False)
        self.assertEqual(len(model.lods), 3)

    def test_a_new_object_is_made_when_no_parent_is_given(self):
        made = new_p3d.build_starter(None, "made_by_test")
        self.addCleanup(made.network.destroy)
        self.assertEqual(made.network.path(), "/obj/made_by_test")
        self.assertEqual(cook(made.nodes["export"]).errors, ())

    def test_a_bad_name_or_parent_raises_a_user_error(self):
        with self.assertRaises(UserError):
            new_p3d.build_starter(None, "1 bad name")
        with self.assertRaises(UserError):
            new_p3d.build_starter(hou.node("/obj"))


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestBatchConvert(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.source = os.path.join(self.root, "src")
        shutil.copytree(hs.FIXTURES, self.source)
        self.settings = sm.Settings()

    def source_state(self):
        return {name: os.path.getmtime(os.path.join(self.source, name)) for name in os.listdir(self.source)}

    def test_lossless_fixtures_are_identical_and_the_sources_stay(self):
        before = self.source_state()
        out = os.path.join(self.root, "out")
        result = bc.convert_folder(self.source, out, preset=sm.LOSSLESS, settings=self.settings)
        names = sorted(name for name in os.listdir(hs.FIXTURES) if name.endswith(".p3d"))
        self.assertEqual([(item.name, item.status) for item in result.files],
                         [(name, bc.IDENTICAL) for name in names])
        self.assertEqual(before, self.source_state())
        with open(os.path.join(out, bc.REPORT_NAME), encoding="utf-8") as file:
            text = file.read()
        for name in names:
            self.assertIn("IDENTICAL: " + name, text)
        self.assertIn("%d identical, 0 different, 0 failed" % len(names), text)

    def test_the_default_output_is_beside_the_input(self):
        self.assertEqual(bc.default_output(self.source), self.source + "_converted")
        result = bc.convert_folder(self.source, settings=self.settings)
        self.assertEqual(result.output_folder, self.source + "_converted")
        self.assertTrue(os.path.isfile(result.report_path))

    def test_the_release_preset_gives_different_files(self):
        result = bc.convert_folder(self.source, os.path.join(self.root, "out"), preset=sm.RELEASE, settings=self.settings)
        self.assertTrue(any(item.status == bc.DIFFERENT and "first difference at byte" in item.detail
                            for item in result.files), result.files)

    def test_output_in_or_equal_to_the_source_folder_is_refused(self):
        before = sorted(os.listdir(self.source))
        for output in (self.source, os.path.join(self.source, "sub"), self.source + os.sep):
            with self.assertRaises(UserError):
                bc.convert_folder(self.source, output, settings=self.settings)
        self.assertEqual(before, sorted(os.listdir(self.source)))

    def test_a_bad_file_fails_alone_and_the_batch_goes_on(self):
        with open(os.path.join(self.source, "broken.p3d"), "wb") as file:
            file.write(b"this is not a p3d file")
        result = bc.convert_folder(self.source, os.path.join(self.root, "out"), settings=self.settings)
        by_name = {item.name: item for item in result.files}
        self.assertEqual(by_name["broken.p3d"].status, bc.FAILED)
        self.assertTrue(by_name["broken.p3d"].detail)
        self.assertEqual(by_name["sample_2_crate.p3d"].status, bc.IDENTICAL)
        self.assertIn("FAILED: broken.p3d -- ", result.report_path and open(result.report_path, encoding="utf-8").read())

    def test_subfolders_only_when_asked_and_cancel_stops(self):
        os.makedirs(os.path.join(self.source, "deep"))
        shutil.copy(os.path.join(hs.FIXTURES, "sample_2_crate.p3d"), os.path.join(self.source, "deep", "c.p3d"))
        self.assertNotIn(os.path.join("deep", "c.p3d"), bc.find_files(self.source))
        self.assertIn(os.path.join("deep", "c.p3d"), bc.find_files(self.source, True))
        result = bc.convert_folder(self.source, os.path.join(self.root, "out"), True, settings=self.settings)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "deep", "c.p3d")))
        result = bc.convert_folder(self.source, os.path.join(self.root, "out2"), True, settings=self.settings,
                                   progress=lambda done, total, name: done < 1)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.files), 1)

    def test_unknown_preset_and_missing_folder_raise(self):
        with self.assertRaises(UserError):
            bc.convert_folder(self.source, os.path.join(self.root, "o"), preset="nope", settings=self.settings)
        with self.assertRaises(UserError):
            bc.convert_folder(os.path.join(self.root, "missing"), settings=self.settings)


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestExportPreset(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.node = self.network.createNode("a3ob_p3d_export")
        self.settings = sm.Settings()

    def options(self):
        return {name: self.node.parm(name).eval() for name in ("selmask", "lowercase", "relpaths", "sortsections",
                                                              "sortlods", "autocomponents", "dupes")}

    def test_release_then_lossless_sets_the_option_parameters(self):
        export_node.apply_preset(self.node, sm.RELEASE, self.settings)
        got = self.options()
        self.assertTrue(got["lowercase"] and got["relpaths"] and got["sortsections"] and got["autocomponents"])
        self.assertFalse(got["sortlods"])
        export_node.apply_preset(self.node, sm.LOSSLESS, self.settings)
        self.assertFalse(any(v for k, v in self.options().items() if k not in ("selmask", "dupes")))

    def test_a_user_preset_fills_rules_and_leaves_the_root_and_backup_alone(self):
        self.node.parm("projectroot").set("D:/root")
        self.node.parm("backup").set(True)
        sm.save_preset(self.settings, "mine", {"sort_lods": True, "duplicate_lods": "skip", "rules_off": ["T2-TRI"]})
        self.node.parm("preset").set("mine")
        export_node.apply_preset(self.node, "", self.settings)
        self.assertTrue(self.node.parm("sortlods").eval())
        self.assertEqual(self.node.parm("dupes").eval(), "skip")
        self.assertEqual(self.node.parm("t2tri").eval(), 0)
        self.assertEqual(self.node.parm("t2pcedge").eval(), 1)
        self.assertEqual(self.node.parm("projectroot").eval(), "D:/root")
        self.assertEqual(self.node.parm("backup").eval(), 1)

    def test_the_button_menu_and_unknown_preset(self):
        with self.assertRaises(UserError):
            export_node.apply_preset(self.node, "nope", self.settings)
        self.assertEqual([token for token, _ in export_node.MENUS["presets"]({})][:2], [sm.LOSSLESS, sm.RELEASE])
        self.node.parm("preset").set(sm.RELEASE)
        self.node.parm("apply").pressButton()
        self.assertTrue(self.node.parm("lowercase").eval())

    def test_empty_project_root_uses_the_settings_root_before_the_default(self):
        folder = tempfile.mkdtemp(prefix="a3ob_root_")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "s.json")
        settings = sm.Settings(project_root="D:\\mod")
        sm.save(settings, path)
        old = {key: os.environ.get(key) for key in (sm.ENV_FILE, sm.ENV_ROOT)}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        os.environ.pop(sm.ENV_ROOT, None)
        os.environ[sm.ENV_FILE] = path
        self.assertEqual(export_node._values(self.node)["projectroot"], "D:\\mod")
        self.node.parm("projectroot").set("E:/own")
        self.assertEqual(export_node._values(self.node)["projectroot"], "E:/own")
        os.environ[sm.ENV_FILE] = os.path.join(folder, "missing.json")
        self.node.parm("projectroot").set("")
        self.assertEqual(export_node._values(self.node)["projectroot"], sm.DEFAULT_ROOT)


class TestShelf(unittest.TestCase):
    """Plain Python: the new shelf tools only call into a3ob_houdini."""

    def test_new_tools_only_import_and_call_their_dialog_layer(self):
        path = os.path.join(hs.REPO_ROOT, "houdini", "toolbar", "a3ob_selection_browser.shelf")
        root = ET.parse(path).getroot()
        members = [member.get("name") for member in root.findall("toolshelf/memberTool")]
        for tool, module in (("a3ob_new_p3d", "new_p3d_dialog"), ("a3ob_batch_convert", "batch_convert_dialog")):
            self.assertIn(tool, members)
            script = root.find("tool[@name='%s']/script" % tool).text
            body = ast.parse(script).body
            self.assertEqual(len(body), 2, tool)
            self.assertEqual(body[0].module, "a3ob_houdini")
            self.assertEqual(body[0].names[0].name, module)
            self.assertEqual(body[1].value.func.value.id, module)


if __name__ == "__main__":
    unittest.main()

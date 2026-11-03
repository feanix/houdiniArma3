"""M3 polish (task 13): the dynamic menu, the stale-code message, Reload A3OB and the Assemble LODs base check.

Purpose: catch a menu that is not written into the asset text, a menu that fails when the input is not a P3D
geometry, a stale session that still shows "a bug in the A3OB tools", a reload that leaves old modules behind, and an
Assemble LODs that fails with a raw Houdini error when the base LOD is not a PackedGeometry prim.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import subprocess
import sys
import tempfile
import unittest

from a3ob_houdini import entry, hda_source, nodes
from a3ob_houdini.errors import UserError
from a3ob_houdini.nodes import extract_lod
from a3ob_houdini.params import NodeSpec, Parm

from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT, hou, install_hdas_for_class, uninstall_hdas

OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")


class TestDynamicMenu(unittest.TestCase):
    def test_extract_lod_items_read_position_colon_name(self):
        self.assertEqual(extract_lod.lod_menu_items(["Resolution 1", "Geometry"]),
                         [("0", "0: Resolution 1"), ("1", "1: Geometry")])
        self.assertEqual(extract_lod.lod_menu_items([]), [])

    def test_the_parameter_keeps_its_name_default_and_accepted_values(self):
        parm = extract_lod.SPEC.parm_index["lod"]
        self.assertEqual((parm.name, parm.kind, parm.default), ("lod", "menu", "0"))
        names = ["Resolution 1", "Geometry"]
        self.assertEqual(extract_lod.resolve_lod("1", names), 1)  # a menu token
        self.assertEqual(extract_lod.resolve_lod("Geometry", names), 1)  # a typed name
        self.assertEqual(extract_lod.resolve_lod("1: Geometry", names), 1)  # the label text

    def test_the_dialog_script_has_a_python_menu_script(self):
        text = hda_source.render(extract_lod.SPEC)["Sop_1a3ob__extract__lod/DialogScript"]
        self.assertIn(".menu('extract_lod', 'lods', kwargs)", text)
        self.assertIn("language python", text)

    def test_a_menu_has_items_or_an_action_not_both_and_only_a_menu_has_one(self):
        with self.assertRaises(ValueError):
            Parm("x", "X", "menu", "a", items=(("a", "A"),), menu_action="m")
        with self.assertRaises(ValueError):
            Parm("x", "X", "string", "", menu_action="m")
        Parm("x", "X", "menu", "", menu_action="m")
        self.assertIsInstance(NodeSpec(kind="k", label="K", summary="", parms=()), NodeSpec)

    def test_entry_menu_is_flat_and_never_raises(self):
        class Fake:
            MENUS = {"ok": lambda kwargs: [("0", "0: A"), ("1", "1: B")], "bad": lambda kwargs: 1 / 0}

        original = nodes.load
        nodes.load = lambda kind: Fake
        try:
            self.assertEqual(entry.menu("k", "ok", {}), ["0", "0: A", "1", "1: B"])
            with self.assertLogs("a3ob", level="ERROR"):
                self.assertEqual(entry.menu("k", "bad", {}), [])
        finally:
            nodes.load = original


class TestStaleCode(unittest.TestCase):
    def test_an_unknown_kind_is_a_user_error_that_says_how_to_fix_it(self):
        with self.assertRaises(UserError) as caught:
            entry._load("no_such_kind")
        text = str(caught.exception)
        self.assertIn("'no_such_kind'", text)
        self.assertIn("Restart Houdini", text)
        self.assertIn("Reload A3OB", text)

    def test_reload_forgets_the_a3ob_modules_and_imports_entry_again(self):
        script = (
            "import sys\n"
            "import a3ob_core\n"
            "from a3ob_houdini import entry, reload\n"
            "old_core = sys.modules['a3ob_core']\n"
            "count = reload.reload_all()\n"
            "assert count > 2, count\n"
            "import a3ob_houdini.entry as new_entry\n"
            "assert new_entry is not entry\n"
            "assert sys.modules['a3ob_houdini.entry'] is new_entry\n"
            "assert sys.modules.get('a3ob_core') is not old_core\n"
            "print('reloaded')\n")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "reload_check.py")
            with open(path, "w", encoding="utf-8") as file:
                file.write(script)
            env = dict(os.environ, PYTHONPATH=os.path.join(REPO_ROOT, "python"))
            done = subprocess.run([sys.executable, path], env=env, capture_output=True, text=True, cwd=folder)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("reloaded", done.stdout)

    def test_the_shelf_tool_only_calls_reload_all(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(REPO_ROOT, "houdini", "toolbar", "a3ob_selection_browser.shelf")).getroot()
        tool = root.find("tool[@name='a3ob_reload']")
        self.assertIsNotNone(tool)
        self.assertIn("a3ob_reload", [member.get("name") for member in root.findall("toolshelf/memberTool")])
        self.assertEqual([line for line in tool.find("script").text.splitlines() if line.strip()],
                         ["from a3ob_houdini import reload", "reload.reload_all()"])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestInHython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = [os.path.join(OTLS_DIR, hda_source.hda_folder_name(nodes.load(kind).SPEC))
                 for kind in ("extract_lod", "assemble_lods", "p3d_import")]
        cls.installed = install_hdas_for_class(paths)  # only the files not installed already (#22)
        cls.container = hou.node("/obj").createNode("geo", "m3_polish_tests")

    @classmethod
    def tearDownClass(cls):
        cls.container.destroy()
        uninstall_hdas(cls.installed)

    def cook(self, node):
        try:
            node.cook(force=True)
        except hou.OperationFailed:
            pass
        return list(node.errors())

    def importer(self):
        node = self.container.createNode("a3ob_p3d_import")
        node.parm("file").set(os.path.join(FIXTURES, "sample_2_crate.p3d"))
        self.assertEqual(self.cook(node), [])
        return node

    def test_the_menu_lists_the_input_lods_and_is_empty_without_one(self):
        imported = self.importer()
        extract = self.container.createNode("a3ob_extract_lod")
        self.assertEqual(extract.parm("lod").menuItems(), ())
        extract.setInput(0, imported)
        count = len(imported.geometry().prims())
        self.assertEqual(extract.parm("lod").menuItems(), tuple(str(i) for i in range(count)))
        labels = extract.parm("lod").menuLabels()
        self.assertTrue(labels[0].startswith("0: "), labels)
        self.assertEqual(self.cook(extract), [])
        extract.parm("lod").set("1")  # a token and a typed value both work
        self.assertEqual(self.cook(extract), [])

    def test_a_base_lod_that_is_not_a_packed_geometry_prim_gives_a_user_error(self):
        imported = self.importer()
        extract = self.container.createNode("a3ob_extract_lod")
        extract.setInput(0, imported)
        self.assertEqual(self.cook(extract), [])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "lod.bgeo").replace("\\", "/")
            extract.geometry().saveToFile(path)
            base = self.container.createNode("python")
            base.parm("python").set(
                "geo = hou.pwd().geometry()\n"
                "prim = geo.createPacked('PackedDisk')\n"
                "prim.setIntrinsicValue('unexpandedfilename', %r)\n" % path)
            self.assertEqual(self.cook(base), [])
            assemble = self.container.createNode("a3ob_assemble_lods")
            assemble.setInput(0, base)
            assemble.setInput(1, extract)
            errors = self.cook(assemble)
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("only a PackedGeometry prim can be replaced", errors[0])
            self.assertIn("PackedDisk", errors[0])
            assemble.parm("replace").set(0)  # no replacement, so the disk prim is only passed through
            self.assertEqual(self.cook(assemble), [])
            self.assertEqual(len(assemble.geometry().prims()), 2)


if __name__ == "__main__":
    unittest.main()

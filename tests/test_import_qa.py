"""P3D Import node QA (task #40): the LOD list, LOD selection by name, the LODs menu and the message levels.

The pure Python tests need no Houdini. The rest needs ``hou`` (run under hython).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import time
import unittest

from a3ob_core import p3d
from a3ob_houdini import nodes
from a3ob_houdini.errors import UserError
from a3ob_houdini.p3d_import import lod_filter
from a3ob_houdini.p3d_import.lod_list import read_lod_names
from tests import hou_support
from tests.hou_support import FIXTURE_NAMES, FIXTURES, HAS_HOU, OTLS_DIR

if HAS_HOU:
    import hou
    from a3ob_houdini import entry, hda_source

NAMES = ["Resolution 1", "Resolution 2", "Geometry", "Memory", "Geometry Memory"]


class TestLodList(unittest.TestCase):
    def test_the_list_matches_a_full_read_of_the_fixtures(self):
        for name in FIXTURE_NAMES:
            path = os.path.join(FIXTURES, name)
            full = [lod.name for lod in p3d.read_p3d_file(path, strict=False).lods]
            self.assertEqual(read_lod_names(path), full, name)

    def test_a_binarized_file_is_a_p3d_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "odol.p3d")
            with open(path, "wb") as file:
                file.write(b"ODOL" + bytes(100))
            with self.assertRaises(p3d.P3DError):
                read_lod_names(path)

    def test_the_largest_samples_file_is_listed_quickly(self):
        path = "E:/SteamLibrary/steamapps/common/Arma 3 Samples/Addons/Test_Plane_01/Test_Plane_01.p3d"
        if not os.path.isfile(path):
            self.skipTest("Arma 3 Samples is not installed")
        started = time.perf_counter()
        names = read_lod_names(path)
        seconds = time.perf_counter() - started
        print("\nLOD list of Test_Plane_01.p3d: %d LODs in %.3f s" % (len(names), seconds))
        self.assertGreater(len(names), 1)
        self.assertLess(seconds, 2.0)


class TestNameSelection(unittest.TestCase):
    def pick(self, text, problems=None):
        return lod_filter.parse_lod_filter(text, len(NAMES), NAMES, problems)

    def test_names_patterns_numbers_and_ranges_mix(self):
        self.assertEqual(self.pick("Geometry"), [2])
        self.assertEqual(self.pick("geometry memory"), [4])
        self.assertEqual(self.pick("Resolution*"), [0, 1])
        self.assertEqual(self.pick("Resolution* 3"), [0, 1, 3])
        self.assertEqual(self.pick("0, Geometry Memory"), [0, 4])
        self.assertEqual(self.pick("Geometry* 1-2"), [1, 2, 4])
        self.assertEqual(self.pick("*"), [0, 1, 2, 3, 4])

    def test_a_bare_star_means_all_wherever_it_is(self):
        everything = [0, 1, 2, 3, 4]
        for text in ("* 0", "0 *", "*", "0, *", "Geometry *", " * "):
            self.assertEqual(self.pick(text), everything, text)
        self.assertEqual(self.pick("Resolution*"), [0, 1])  # a pattern is not a bare star
        self.assertEqual(self.pick("Geometry Memory"), [4])  # names with spaces still work

    def test_a_menu_pick_from_the_default_selects_just_that_lod(self):
        self.assertEqual(self.pick(lod_filter.drop_star("* 0")), [0])
        self.assertEqual(self.pick(lod_filter.drop_star("* 4")), [4])
        self.assertEqual(lod_filter.drop_star("* Geometry Memory"), "Geometry Memory")
        self.assertEqual(lod_filter.drop_star("0 *"), "0")
        self.assertEqual(lod_filter.drop_star("*"), "*")
        self.assertEqual(lod_filter.drop_star("0 2-3"), "0 2-3")
        self.assertEqual(lod_filter.drop_star("Resolution* 3"), "Resolution* 3")

    def test_the_longest_name_wins(self):
        self.assertEqual(self.pick("Geometry Memory"), [4])  # not Geometry and then "Memory"
        self.assertEqual(self.pick("Geometry Memory Memory"), [3, 4])

    def test_an_unknown_name_is_a_problem_not_an_error(self):
        problems = []
        self.assertEqual(self.pick("Geometry Shadow", problems), [2])
        self.assertEqual(len(problems), 1)
        self.assertIn("'Shadow'", problems[0])

    def test_without_a_problem_list_it_is_a_user_error(self):
        with self.assertRaises(UserError):
            self.pick("Nothing")

    def test_missing_numbers_still_raise(self):
        with self.assertRaises(UserError):
            self.pick("9")


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestMessageLevels(unittest.TestCase):
    def test_inline_triangles_and_layout_are_info_once_per_lod(self):
        lod = p3d.LOD(p3d.LodType.VISUAL, 1.0)
        quads = [(0, 1, 2, 3), (1, 4, 5, 2)]
        base = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
        points = [(5, 5, 5), (5, 6, 5), (5, 5, 7)] + base  # a packed proxy first: the proxies are not last
        lod.set_mesh(points=points, polygons=[(0, 1, 2)] + [tuple(v + 3 for v in q) for q in quads],
                     normals=[(0, 0, -1)], normal_indices=[0] * 11)
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\x.001", [1, 1, 1] + [0] * 6, [1, 0, 0]))
        # two proxies that do not fit the packed form (they use shared points), and a case-only duplicate
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\y.1", [0] * 3 + [1, 1, 0, 0, 0, 0], [0, 0, 0]))
        lod.taggs.append(p3d.SelectionTagg("proxy:\\a3\\z.2", [0] * 3 + [0, 0, 1, 1, 0, 0], [0, 0, 0]))
        lod.taggs.append(p3d.SelectionTagg("Head", [0] * 3 + [1, 0, 0, 0, 0, 0], [0, 0, 0]))
        lod.taggs.append(p3d.SelectionTagg("head", [0] * 3 + [0, 1, 0, 0, 0, 0], [0, 0, 0]))
        _, report, _ = hou_support.import_model(p3d.P3DModel([lod]))
        warnings, infos = report.texts("warning"), report.texts("info")
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("differ only in case", warnings[0])
        self.assertEqual(len(infos), 2, infos)
        self.assertEqual(len([t for t in infos if "stay inline triangles" in t]), 1)
        self.assertIn("2 proxies", [t for t in infos if "inline" in t][0])
        self.assertEqual(len([t for t in infos if "not the last points and faces" in t]), 1)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestImportNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(OTLS_DIR, hda_source.hda_folder_name(nodes.load("p3d_import").SPEC))
        cls.installed = hou_support.install_hdas_for_class([path])
        cls.container = hou.node("/obj").createNode("geo", "import_qa_tests")
        cls.crate = os.path.join(FIXTURES, "sample_2_crate.p3d")
        cls.names = [lod.name for lod in p3d.read_p3d_file(cls.crate, strict=False).lods]

    @classmethod
    def tearDownClass(cls):
        cls.container.destroy()
        hou_support.uninstall_hdas(cls.installed)

    def importer(self, text):
        node = self.container.createNode("a3ob_p3d_import")
        node.parm("file").set(self.crate)
        node.parm("lods").set(text)
        try:
            node.cook(force=True)
        except hou.OperationFailed:
            pass
        return node

    def test_the_menu_lists_the_lods_with_their_index(self):
        node = self.importer("*")
        items = entry.menu("p3d_import", "lod_choices", {"node": node})
        expected = []
        for index, name in enumerate(self.names):
            expected += [str(index), "%d  %s" % (index, name)]
        self.assertEqual(items, expected)
        self.assertEqual(items[:2], ["0", "0  " + self.names[0]])

    def test_the_menu_is_empty_without_a_readable_file(self):
        node = self.importer("*")
        node.parm("file").set("")
        self.assertEqual(entry.menu("p3d_import", "lod_choices", {"node": node}), [])
        node.parm("file").set(os.path.join(FIXTURES, "does_not_exist.p3d"))
        self.assertEqual(entry.menu("p3d_import", "lod_choices", {"node": node}), [])

    def test_the_lods_parm_is_a_toggle_menu_on_a_string(self):
        template = self.importer("*").parm("lods").parmTemplate()
        self.assertEqual(template.type(), hou.parmTemplateType.String)
        self.assertEqual(template.menuType(), hou.menuType.StringToggle)

    def test_a_menu_pick_from_the_default_imports_just_that_lod(self):
        node = self.importer("* 0")  # what the menu writes when 0 is picked and the field holds the default *
        entry.button("p3d_import", "lods_changed", {"node": node})
        self.assertEqual(node.parm("lods").eval(), "0")
        node.cook(force=True)
        self.assertEqual([p.attribValue("name") for p in node.geometry().prims()], [self.names[0]])

    def test_a_star_set_next_to_entries_still_means_all(self):
        node = self.importer("* 0")
        self.assertEqual(len(node.geometry().prims()), len(self.names))

    def test_selecting_by_name_and_pattern(self):
        geometry = self.importer("Geometry").geometry()
        self.assertEqual([p.attribValue("name") for p in geometry.prims()], ["Geometry"])
        node = self.importer("resolution*")
        self.assertEqual([p.attribValue("name") for p in node.geometry().prims()],
                         [n for n in self.names if n.startswith("Resolution")])

    def test_an_unknown_name_is_a_warning(self):
        node = self.importer("0 NoSuchLod")
        self.assertEqual(list(node.errors()), [])
        self.assertTrue(any("NoSuchLod" in text for text in node.warnings()), node.warnings())
        self.assertEqual(len(node.geometry().prims()), 1)


if __name__ == "__main__":
    unittest.main()

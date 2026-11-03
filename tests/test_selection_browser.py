"""The Selection browser functions (ux-tools.md section 7.3): decoded names and the three SOP node actions.

Purpose: catch a wrong decode (the artist would see ``xn__...``), a wrong group name in a new node, and a node that
is added to the wrong place. The dialog layer is not tested (hython has no ``hou.ui``). Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from tests import hou_support
from tests.hou_support import FIXTURE_NAMES, HAS_HOU

if HAS_HOU:
    import hou
    from a3ob_core import p3d
    from a3ob_houdini import names, selection_browser as browser
    from a3ob_houdini.errors import UserError

NON_ASCII = "café €"
PROXY = "proxy:\\a3\\characters_f\\proxies\\backpack.001"


class TestShelfFile(unittest.TestCase):
    def test_shelf_parses_and_only_calls_the_dialog_layer(self):
        import os
        import xml.etree.ElementTree as ET
        path = os.path.join(hou_support.REPO_ROOT, "houdini", "toolbar", "a3ob_selection_browser.shelf")
        root = ET.parse(path).getroot()
        tool = root.find("tool[@name='a3ob_selection_browser']")
        self.assertIsNotNone(tool)
        self.assertEqual(root.find("toolshelf/memberTool").get("name"), "a3ob_selection_browser")
        script = tool.find("script").text.strip().splitlines()
        self.assertEqual([line for line in script if line.strip()], [
            "from a3ob_houdini import selection_browser_dialog", "selection_browser_dialog.run(kwargs)"])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestListGroups(unittest.TestCase):
    def test_both_fixtures_decode_to_the_names_in_the_file(self):
        for fixture in FIXTURE_NAMES:
            model, geo, _, _ = hou_support.imported_fixture(fixture)
            total = 0
            for lod, prim in zip(model.lods, hou_support.packed_prims(geo)):
                file_names = {t.name for t in lod.taggs if isinstance(t, p3d.SelectionTagg)}
                entries = browser.list_groups(prim.getEmbeddedGeometry())
                for entry in entries:
                    self.assertIn(entry.name, file_names, fixture)
                    self.assertEqual(names.encode_group_name(entry.name), entry.encoded)
                total += len(entries)
            self.assertGreater(total, 0, fixture)

    def test_character_shows_minus_proxies_not_the_encoded_name(self):
        _, geo, _, _ = hou_support.imported_fixture("sample_1_character.p3d")
        entries = {e.name: e for e in browser.list_groups(hou_support.packed_prims(geo)[0].getEmbeddedGeometry())}
        self.assertEqual(entries["-proxies"].encoded, "xn__proxies_cka")
        self.assertIn("-proxies  (xn__proxies_cka)", browser.describe(entries["-proxies"]))

    def test_non_ascii_proxy_weighted_and_face_flags(self):
        geo = hou.Geometry()
        points = [geo.createPoint() for _ in range(3)]
        geo.createPolygon().addVertex(points[0])
        encoded_text = names.encode_group_name(NON_ASCII)
        encoded_proxy = names.encode_group_name(PROXY)
        geo.createPointGroup(encoded_text).add(points[:2])
        geo.createPointGroup(encoded_proxy).add(points[2])
        geo.createPrimGroup(encoded_text)
        geo.addAttrib(hou.attribType.Point, names.point_weight_attrib(encoded_text), 0.0)
        found = {e.name: e for e in browser.list_groups(geo)}
        self.assertEqual(sorted(found), sorted([NON_ASCII, PROXY]))
        text = found[NON_ASCII]
        self.assertEqual((text.count, text.weighted, text.faces, text.proxy), (2, True, True, False))
        proxy = found[PROXY]
        self.assertEqual((proxy.count, proxy.weighted, proxy.faces, proxy.proxy), (1, False, False, True))
        self.assertIn("weighted, faces", browser.describe(text))
        self.assertIn("proxy", browser.describe(proxy))
        self.assertEqual([e.name for e in browser.list_groups(geo, " CAFÉ ")], [NON_ASCII])
        self.assertEqual([e.name for e in browser.list_groups(geo, "xn__")], [NON_ASCII, PROXY])
        self.assertEqual(browser.list_groups(geo, "nothing like this"), [])

    def test_packed_lods_are_recognised(self):
        _, geo, _, _ = hou_support.imported_fixture("sample_2_crate.p3d")
        self.assertTrue(browser.has_packed_lods(geo))
        self.assertEqual(browser.list_groups(geo), [])
        self.assertFalse(browser.has_packed_lods(hou.Geometry()))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestActions(unittest.TestCase):
    def setUp(self):
        self.container = hou.node("/obj").createNode("geo", "selection_browser_test")
        self.box = self.container.createNode("box")

    def tearDown(self):
        self.container.destroy()

    def groups(self, node):
        return {g.name(): [p.number() for p in g.points()] for g in node.geometry().pointGroups()}

    def test_create_adds_a_group_node_with_the_encoded_name(self):
        for name in (NON_ASCII, "-proxies", "plain"):
            new = browser.create_selection(self.box, name, "0-2")
            self.assertEqual(new.type().name(), "group")
            self.assertEqual(new.inputs(), (self.box,))
            new.cook(force=True)
            self.assertEqual(new.errors(), ())
            self.assertEqual(self.groups(new), {names.encode_group_name(name): [0, 1, 2]})

    def test_create_with_an_empty_expression_takes_all_points(self):
        new = browser.create_selection(self.box, "all")
        new.cook(force=True)
        self.assertEqual(len(self.groups(new)["all"]), 8)

    def test_rename_and_delete_follow_the_chain(self):
        made = browser.create_selection(self.box, NON_ASCII, "0-2")
        renamed = browser.rename_selection(made, NON_ASCII, "-proxies")
        self.assertEqual(renamed.type().name(), "grouprename")
        self.assertEqual(renamed.inputs(), (made,))
        renamed.cook(force=True)
        self.assertEqual(renamed.errors(), ())
        self.assertEqual(self.groups(renamed), {"xn__proxies_cka": [0, 1, 2]})
        entry = browser.list_groups(renamed.geometry())[0]
        self.assertEqual((entry.name, entry.encoded), ("-proxies", "xn__proxies_cka"))
        deleted = browser.delete_selection(renamed, "-proxies")
        self.assertEqual(deleted.type().name(), "groupdelete")
        self.assertEqual(deleted.inputs(), (renamed,))
        deleted.cook(force=True)
        self.assertEqual(deleted.errors(), ())
        self.assertEqual(self.groups(deleted), {})

    def test_bad_requests_are_user_errors_in_the_ux_form(self):
        made = browser.create_selection(self.box, "head", "0")
        made.cook(force=True)
        cases = [
            lambda: browser.create_selection(made, ""),
            lambda: browser.create_selection(made, "bad\ud800name"),
            lambda: browser.create_selection(made, "head"),
            lambda: browser.delete_selection(made, "missing"),
            lambda: browser.rename_selection(made, "missing", "x"),
            lambda: browser.rename_selection(made, "head", "head"),
        ]
        for case in cases:
            with self.assertRaises(UserError) as caught:
                case()
            self.assertTrue(str(caught.exception).startswith("Selection browser: "), str(caught.exception))
        self.assertEqual(len(self.container.children()), 2)  # nothing was added by the failed calls
        occupied = browser.create_selection(made, "other", "1")
        occupied.cook(force=True)
        with self.assertRaises(UserError):
            browser.rename_selection(occupied, "head", "other")


if __name__ == "__main__":
    unittest.main()

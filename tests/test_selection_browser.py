"""The Selection browser functions (ux-tools.md section 7.3): decoded names and the three SOP node actions.

Purpose: catch a wrong decode (the artist would see ``xn__...``), a wrong group name in a new node, and a node that
is added to the wrong place. The dialog layer is not tested (hython has no ``hou.ui``). Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from tests import hou_support
from tests.hou_support import FIXTURE_NAMES, HAS_HOU
from tests.mass_proxy_support import NodeTestCase

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
            new.destroy()  # the next one would be spliced in front of this one (D21)

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


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestInlineInsert(NodeTestCase):
    """D21: the new node is spliced in, so the nodes after the chosen one read it and the export sees it.

    Purpose: catch a dead-end branch (the export unchanged), a lost input index on a multi-input node, a lost
    display flag, and overlapping or upward-pointing positions after the shift."""

    def setUp(self):
        super().setUp()
        from a3ob_houdini import new_p3d
        self.made = new_p3d.build_starter(self.network)
        self.nodes = self.made.nodes

    def memory_selections(self, path=None):
        import os
        import shutil
        import tempfile
        from a3ob_houdini.nodes import p3d_export
        folder = tempfile.mkdtemp(prefix="a3ob_inline_")
        self.addCleanup(shutil.rmtree, folder, True)
        file = os.path.join(folder, "x.p3d").replace("\\", "/")
        export = self.nodes["export"]
        export.parm("file").set(file)
        p3d_export.save(export)
        model = p3d.read_p3d_file(file, strict=False)
        lods = [lod for lod in model.lods if lod.lod_type == p3d.LodType.MEMORY]
        self.assertEqual(len(lods), 1)
        return sorted(t.name for t in lods[0].taggs if isinstance(t, p3d.SelectionTagg))

    def pos(self, node):
        p = node.position()
        return p.x(), p.y()

    def test_rename_reaches_the_export(self):
        group, props = self.nodes["memory_group"], self.nodes["memory_props"]
        self.assertEqual(self.memory_selections(), ["new_point"])
        before = self.pos(props)
        new = browser.rename_selection(group, "new_point", "eye")
        self.assertEqual(props.inputs(), (new,))
        self.assertEqual(new.inputs(), (group,))
        self.assertEqual(group.outputs(), (new,))
        self.assertEqual(self.memory_selections(), ["eye"])
        self.assertEqual(self.pos(props)[0], before[0])
        self.assertAlmostEqual(self.pos(props)[1], before[1] - browser.ROW_STEP, places=4)
        self.assertAlmostEqual(self.pos(new)[1], self.pos(group)[1] - browser.ROW_STEP, places=4)
        self.assertEqual(self.pos(new)[0], self.pos(group)[0])

    def test_delete_and_create_reach_the_export(self):
        group = self.nodes["memory_group"]
        created = browser.create_selection(group, "second", "0")
        self.assertEqual(self.nodes["memory_props"].inputs(), (created,))
        self.assertEqual(self.memory_selections(), ["new_point", "second"])
        deleted = browser.delete_selection(created, "new_point")
        self.assertEqual(self.nodes["memory_props"].inputs(), (deleted,))
        self.assertEqual(self.memory_selections(), ["second"])

    def test_flags_move_to_the_new_node(self):
        export = self.nodes["export"]
        new = browser.create_selection(self.nodes["assemble"].inputs()[2].inputs()[0], "late")
        self.assertTrue(export.isDisplayFlagSet())  # export was not the chosen node
        self.assertFalse(new.isDisplayFlagSet())
        head = self.made.network.createNode("box", "head")
        head.setDisplayFlag(True)
        tail = self.made.network.createNode("null", "tail2")
        tail.setFirstInput(head)
        spliced = browser.create_selection(head, "flagged")
        self.assertTrue(spliced.isDisplayFlagSet())
        self.assertFalse(head.isDisplayFlagSet())

    def test_two_downstream_nodes_and_input_indices_are_rewired(self):
        box = self.made.network.createNode("box", "split")
        one = self.made.network.createNode("null", "one")
        two = self.made.network.createNode("null", "two")
        one.setFirstInput(box)
        two.setFirstInput(box)
        merge = self.made.network.createNode("merge", "merge_it")
        merge.setInput(0, one)
        merge.setInput(1, box)
        new = browser.create_selection(box, "both")
        self.assertEqual(one.inputs(), (new,))
        self.assertEqual(two.inputs(), (new,))
        self.assertEqual(merge.inputs(), (one, new))
        assemble = self.nodes["assemble"]
        indices = [c.inputIndex() for c in self.nodes["memory_props"].outputConnections()]
        self.assertEqual(indices, [3])
        browser.create_selection(self.nodes["memory_props"], "tail")
        self.assertEqual(assemble.inputs()[3].type().name(), "group")
        self.assertEqual([n.name() for n in assemble.inputs()[1:3]], ["res1_props", "geometry_props"])

    def test_positions_stay_ordered_and_apart(self):
        browser.create_selection(self.nodes["geometry_triangles"], "mid")
        browser.rename_selection(self.nodes["geometry_components"], "component01", "renamed")
        before = {n.name(): self.pos(n) for n in self.made.network.children()}
        seen = set()
        for node in self.made.network.children():
            position = (round(self.pos(node)[0], 3), round(self.pos(node)[1], 3))
            self.assertNotIn(position, seen, node.name())
            seen.add(position)
            for source in node.inputs():
                if source is not None:
                    self.assertLess(self.pos(node)[1], self.pos(source)[1], node.name())
        self.assertEqual(before["res1_mesh"], (round(before["res1_mesh"][0], 10), before["res1_mesh"][1]))
        # a node that is not downstream does not move
        mesh = self.nodes["geometry_mesh"]
        self.assertEqual(self.pos(mesh), (0.0, 0.0))

    def test_new_p3d_layout_is_not_shifted_by_the_splice(self):
        from a3ob_houdini import new_p3d
        origin = self.pos(self.nodes["geometry_mesh"])
        for key, (dx, dy) in new_p3d.LAYOUT.items():
            x, y = self.pos(self.nodes[key])
            self.assertAlmostEqual(x - origin[0], dx, delta=0.01)
            self.assertAlmostEqual(y - origin[1], dy, delta=0.01)


if __name__ == "__main__":
    unittest.main()

"""Generator support for the node QA pass (#31 part A): visibility conditions, folders, file and folder pickers,
colour with alpha, toggle menus, and the escaping and filler fixes in the Help section.

A small test SPEC (not a real node) goes through the same generators as the nodes. The hython tests install the
asset it makes into the running session only and read the node back with ``parmTemplateGroup``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import shutil
import tempfile
import unittest

from a3ob_houdini import docs_common, hda_source, nodes, params
from a3ob_houdini.params import NodeSpec, Parm

from tests.hou_support import HAS_HOU, hou
from tests.test_user_docs import load_builder

MODE_ITEMS = (("a", "Alpha mode"), ("b", "Beta mode"), ("c", "Gamma mode"))

SPEC = NodeSpec(
    kind="gen_test", label="Generator Test", summary="Only for the tests.",
    inputs=(1, 1), input_labels=("Test geometry",), input_help=("The geometry the test reads.",),
    parms=(
        Parm("mode", "Mode", "menu", "a", "Pick one.", items=MODE_ITEMS,
             item_help=(("a", "does the first thing"),)),
        Parm("usetex", "Use Texture", "toggle", True, "Turn the texture on."),
        Parm("tab_files", "Files", "folder", folder_type="tabs", children=(
            Parm("tex", "Texture File", "file", "", "A texture.", file_pattern="*.paa *.tga",
                 show_when=(("mode", ("a", "b")),), enable_when=(("usetex", ("1",)),)),
            Parm("root", "Root Folder", "dir", "", "A folder."),
        )),
        Parm("tab_look", "Look", "folder", folder_type="tabs", children=(
            Parm("tint", "Tint", "color", (1.0, 0.5, 0.25, 1.0), "A colour.", show_when=(("mode", ("c",)),)),
            Parm("picks", "Picks", "menu", "", "Several entries.", menu_action="picks", menu_toggle=True),
        )),
        Parm("box", "Box", "folder", folder_type="simple", children=(
            Parm("amount", "Amount", "float", 2.0, "A number."),
        )),
        Parm("more", "More", "folder", folder_type="collapsible", children=(
            Parm("rows", "Rows", "multiparm", 0, "Row list.", children=(
                Parm("rowname#", "Name #", "string", "", "A row name."),)),
        )),
    ),
)


class FakeParm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value


class FakeNode:
    def __init__(self, values):
        self.values = values

    def parm(self, name):
        return FakeParm(self.values[name]) if name in self.values and not isinstance(self.values[name], tuple) else None

    def parmTuple(self, name):
        return FakeParm(self.values[name]) if isinstance(self.values.get(name), tuple) else None


class TestParmKinds(unittest.TestCase):
    def test_the_new_kinds_validate(self):
        self.assertEqual(SPEC.parm_index["tint"].size, 4)
        self.assertEqual(Parm("c", "C", "color").default, (1.0, 1.0, 1.0, 1.0))
        for bad in (lambda: Parm("f", "F", "folder"),
                    lambda: Parm("f", "F", "folder", folder_type="round", children=(Parm("x", "X", "toggle"),)),
                    lambda: Parm("f", "F", "folder", children=(Parm("x", "X", "toggle"),),
                                 show_when=(("a", ("1",)),)),
                    lambda: Parm("c", "C", "color", size=3, default=(1, 1, 1)),
                    lambda: Parm("m", "M", "menu", "", menu_toggle=True, items=(("a", "A"),)),
                    lambda: Parm("t", "T", "toggle", item_help=(("a", "A"),)),
                    lambda: Parm("m", "M", "menu", "a", items=(("a", "A"),), item_help=(("z", "Z"),))):
            with self.assertRaises(ValueError):
                bad()

    def test_a_condition_must_name_a_parameter_of_the_node(self):
        with self.assertRaises(ValueError):
            NodeSpec("x", "X", "s", parms=(Parm("a", "A", "toggle", show_when=(("nope", ("1",)),)),))
        with self.assertRaises(ValueError):  # a folder name is not a value
            NodeSpec("x", "X", "s", parms=(
                Parm("f", "F", "folder", children=(Parm("a", "A", "toggle"),)),
                Parm("b", "B", "toggle", show_when=(("f", ("1",)),))))

    def test_duplicate_names_inside_folders_are_found(self):
        with self.assertRaises(ValueError):
            NodeSpec("x", "X", "s", parms=(Parm("a", "A", "toggle"),
                                            Parm("f", "F", "folder", children=(Parm("a", "A2", "toggle"),))))

    def test_read_parms_looks_through_folders_and_gives_no_folder_value(self):
        node = FakeNode({"mode": "b", "usetex": 0, "tex": "t.paa", "root": "P:\\", "tint": (0.1, 0.2, 0.3, 0.4),
                         "picks": "1 2", "amount": 3.5, "rows": 1, "rowname1": "x"})
        values = params.read_parms(node, SPEC)
        self.assertEqual(list(values), ["mode", "usetex", "tex", "root", "tint", "picks", "amount", "rows"])
        self.assertEqual(values["tint"], (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(values["rows"], [{"rowname": "x"}])
        empty = params.read_parms(FakeNode({}), SPEC)  # an old node without the parameters: the defaults
        self.assertEqual(empty["tint"], (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(empty["amount"], 2.0)
        for name in ("tab_files", "tab_look", "box", "more"):
            self.assertNotIn(name, values)

    def test_the_condition_text(self):
        self.assertEqual(params.hide_when_text((("mode", ("a", "b")),)), "{ mode != a mode != b }")
        self.assertEqual(params.hide_when_text((("mode", ("a",)), ("usetex", ("1",)))),
                         "{ mode != a } { usetex != 1 }")
        self.assertEqual(params.hide_when_text((("mode", ("a b",)),)), '{ mode != "a b" }')


class TestDialogScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = hda_source.render(SPEC)[hda_source.section_name(SPEC) + "/DialogScript"]

    def test_visibility(self):
        self.assertIn('\thidewhen\t"{ mode != a mode != b }"', self.text)
        self.assertIn('\tdisablewhen\t"{ usetex != 1 }"', self.text)
        self.assertIn('\thidewhen\t"{ mode != c }"', self.text)

    def test_folders(self):
        for word, name in (("group", "tab_files"), ("group", "tab_look"), ("groupsimple", "box"),
                           ("groupcollapsible", "more")):
            self.assertIn('    %s {\n\tname\t"%s"' % (word, name), self.text)
        # The children are inside their folder.
        self.assertLess(self.text.index('"tab_files"'), self.text.index('"tex"'))
        self.assertLess(self.text.index('"tex"'), self.text.index('"tab_look"'))
        self.assertLess(self.text.index('"more"'), self.text.index('"rows"'))

    def test_the_pickers_colour_and_toggle_menu(self):
        self.assertIn('\ttype\tfile\n', self.text)
        self.assertIn('parmtag\t{ "filechooser_mode" "read" }', self.text)
        self.assertIn('parmtag\t{ "filechooser_pattern" "*.paa *.tga" }', self.text)
        self.assertIn("\ttype\tdirectory\n", self.text)
        text = self.text.replace("    \t", "\t")  # children sit one level deeper
        self.assertIn("\ttype\tcolor4\n\tsize\t4\n\tdefault\t{ \"1\" \"0.5\" \"0.25\" \"1\" }\n\trange\t{ 0 1 }", text)
        self.assertIn("\tmenutoggle {\n\t    [ ", text)
        self.assertEqual(self.text.count("menutoggle"), 1)

    def test_every_file_is_stable(self):
        self.assertEqual(hda_source.render(SPEC), hda_source.render(SPEC))


class TestHelpAndReference(unittest.TestCase):
    def help(self, spec=SPEC):
        return hda_source.render(spec)[hda_source.section_name(spec) + "/Help"]

    def test_the_selection_mask_default_is_shown_literally(self):
        text = self.help(nodes.load("p3d_export").SPEC)
        self.assertIn("Default: `* ^__*`.", text)
        self.assertNotIn("Default: * ^__*", text)

    def test_escape(self):
        esc = docs_common.wiki_escape
        self.assertEqual(esc("* ^__*"), "`* ^__*`")
        self.assertEqual(esc("Use * and ?."), "Use `*` and ?.")
        self.assertEqual(esc("Match *.p3d."), "Match `*.p3d`.")
        self.assertEqual(esc("plain text, a_b and a3ob_tagg_order"), "plain text, a_b and a3ob_tagg_order")
        self.assertEqual(esc("see [x] and `y`"), "see `[x]` and 'y'")
        self.assertEqual(esc(""), "")

    def test_menu_choices_without_help_are_not_given_filler(self):
        for spec in nodes.all_specs().values():
            self.assertNotIn("Sets this to", self.help(spec), spec.type_name)
        text = self.help()
        self.assertIn("Alpha mode:\n        Does the first thing\n", text)  # own help, capitalised
        self.assertIn("Other choices: Beta mode, Gamma mode.", text)
        self.assertIn("Choices: Fail, Skip, Ignore.", self.help(nodes.load("p3d_export").SPEC))

    def test_choice_lines_start_with_a_capital(self):
        text = self.help(nodes.load("p3d_export").SPEC)
        self.assertIn("Every cook:\n        Check whenever the node cooks.", text)
        parm = nodes.load("p3d_export").SPEC.parm_index["check"]
        intro, texts = docs_common.menu_texts(parm)
        self.assertEqual(intro, "")
        self.assertTrue(all(value[0].isupper() for value in texts.values()))

    def test_the_intro_of_a_split_menu_help_is_kept(self):
        parm = Parm("m", "M", "menu", "x", "Pick how to do it. Fast: uses less time; Slow: is careful.",
                    items=(("x", "Fast"), ("y", "Slow")))
        intro, texts = docs_common.menu_texts(parm)
        self.assertEqual(intro, "Pick how to do it.")
        self.assertEqual(texts, {"x": "Uses less time", "y": "Is careful."})

    def test_inputs_are_described(self):
        self.assertIn("Test geometry:\n    The geometry the test reads.", self.help())
        for spec in nodes.all_specs().values():
            self.assertNotRegex(self.help(spec), r"\n    Inputs? \d")
            for first, _, _ in docs_common.collapse_inputs(spec):
                self.assertTrue(spec.input_help[first - 1], "%s: input %d has no description" % (spec.type_name, first))

    def test_the_help_says_when_a_parameter_is_shown(self):
        text = self.help()
        self.assertIn("Shown when: Mode is Alpha mode or Beta mode", text)
        self.assertIn("Editable when: Use Texture is on", text)
        self.assertIn("Shown when: Mode is Gamma mode", text)

    def test_folders_are_headings_in_the_help(self):
        text = self.help()
        for heading in ("== Files == (tab_files)", "== Look == (tab_look)", "== Box == (box)", "== More == (more)"):
            self.assertIn(heading + "\n", text)
        self.assertLess(text.index("== Files =="), text.index("Texture File:"))
        self.assertLess(text.index("Texture File:"), text.index("== Look =="))
        self.assertIn("These parameters are in the Files tab.", text)

    def test_the_html_reference(self):
        builder = load_builder()
        _, page = builder.node_page(SPEC)
        self.assertIn("<dt>Shown when</dt><dd>Mode is Alpha mode or Beta mode</dd>", page)
        self.assertIn("<dt>Editable when</dt><dd>Use Texture is on</dd>", page)
        self.assertIn('<section class="folder" id="tab_files">', page)
        self.assertIn("<h3>Files <code>tab_files</code></h3>", page)
        self.assertIn("These parameters are in the More group box you can fold.", page)
        self.assertIn("<dt>Type</dt><dd>Colour with alpha</dd>", page)
        self.assertIn("<dt>Type</dt><dd>Folder path</dd>", page)
        self.assertIn("1, 0.5, 0.25, 1 (red, green, blue, alpha)", page)
        self.assertIn("Input 1: Test geometry. The geometry the test reads.", page)
        self.assertLess(page.index('id="tab_files"'), page.index('id="tex"'))
        self.assertLess(page.index('id="tex"'), page.index('id="tab_look"'))
        # A row parameter inside a folder keeps its anchor prefix.
        self.assertIn('id="rows-rowname"', page)


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestWikiParsing(unittest.TestCase):
    """The Help text goes through Houdini's own wiki parser (bookish), as the help server does."""

    @staticmethod
    def walk(items):
        for item in items:
            if isinstance(item, dict):
                yield item
                yield from TestWikiParsing.walk(item.get("text") or [])
            elif isinstance(item, list):
                yield from TestWikiParsing.walk(item)

    @staticmethod
    def plain(items):
        out = []
        for item in items or []:
            out.append(item if isinstance(item, str) else TestWikiParsing.plain(item.get("text")))
        return "".join(out)

    def parse(self, spec):
        from bookish.wiki.wikipages import parse_to_root
        text = hda_source.render(spec)[hda_source.section_name(spec) + "/Help"]
        return parse_to_root(text)["body"]

    def test_no_help_text_turns_into_bold_italics_links_or_variables(self):
        for spec in list(nodes.all_specs().values()) + [SPEC]:
            for block in self.parse(spec):
                for item in self.walk([block]):
                    self.assertNotIn(item.get("type"), ("strong", "em", "link", "var", "keys"),
                                     "%s: %s" % (spec.type_name, json.dumps(block)[:200]))

    def test_the_selection_mask_default_renders_as_written(self):
        body = self.parse(nodes.load("p3d_export").SPEC)
        paragraphs = [self.plain(block.get("text")) for block in body if block.get("type") == "para"]
        self.assertIn("Type: Menu (pick several; each pick adds or removes an entry in the text). Default: * ^__*.",
                      paragraphs)

    def test_a_label_with_an_inner_colon_is_one_term(self):
        spec = NodeSpec("x", "X", "s", parms=(Parm("r", "T2-UV2: At most 2 UV sets", "toggle", False, "Rule."),))
        terms = [self.plain(block.get("text")) for block in self.parse(spec) if block.get("type") == "dt"]
        self.assertIn("T2-UV2: At most 2 UV sets", terms)

    def test_folders_parse_as_headings(self):
        headings = [self.plain(block.get("text")) for block in self.parse(SPEC) if block.get("type") == "h"]
        self.assertEqual(headings, ["Files", "Look", "Box", "More"])


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestGeneratedAssetInHoudini(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="a3ob_gen_test_")
        hda_source.write(SPEC, cls.directory)
        cls.path = os.path.join(cls.directory, hda_source.hda_folder_name(SPEC)).replace("\\", "/")
        hou.hda.installFile(cls.path)
        cls.geo = hou.node("/obj").createNode("geo")
        cls.node = cls.geo.createNode(SPEC.type_name)

    @classmethod
    def tearDownClass(cls):
        cls.geo.destroy()
        hou.hda.uninstallFile(cls.path)
        shutil.rmtree(cls.directory, ignore_errors=True)

    def template(self, name):
        found = self.node.parmTemplateGroup().find(name)
        self.assertIsNotNone(found, name)
        return found

    def test_the_folder_tree_matches_the_table(self):
        def tree(templates):
            out = []
            for template in templates:
                if isinstance(template, hou.FolderParmTemplate):
                    # Houdini renames the tabs of one row after the first (tab_files, tab_files_1): the name of
                    # a tab is not compared, its label is.
                    name = "(tab)" if template.folderType() == hou.folderType.Tabs else template.name()
                    out.append((name, template.label(), template.folderType().name(),
                                tree(template.parmTemplates())))
                else:
                    out.append((template.name(),))
            return out

        def expected(parms):
            out = []
            for parm in parms:
                if parm.kind == "folder":
                    word = {"simple": "Simple", "collapsible": "Collapsible", "tabs": "Tabs"}[parm.folder_type]
                    out.append(("(tab)" if word == "Tabs" else parm.name, parm.label, word, expected(parm.children)))
                elif parm.kind == "multiparm":
                    out.append((parm.name, parm.label, "MultiparmBlock", [(c.name,) for c in parm.children]))
                else:
                    out.append((parm.name,))
            return out

        mine = [t for t in self.node.parmTemplateGroup().entries() if not t.name().startswith("label")]
        self.assertEqual(tree(mine), expected(SPEC.parms))

    def test_file_and_directory_pickers(self):
        tex = self.template("tex")
        self.assertEqual(tex.stringType(), hou.stringParmType.FileReference)
        self.assertEqual(tex.tags().get("filechooser_mode"), "read")
        self.assertEqual(tex.tags().get("filechooser_pattern"), "*.paa *.tga")
        root = self.template("root")
        self.assertEqual(root.stringType(), hou.stringParmType.FileReference)
        self.assertEqual(root.fileType(), hou.fileType.Directory)
        self.assertEqual(self.template("usetex").type(), hou.parmTemplateType.Toggle)

    def test_colour_with_alpha(self):
        tint = self.template("tint")
        self.assertEqual(tint.numComponents(), 4)
        self.assertEqual(tint.look(), hou.parmLook.ColorSquare)
        self.assertEqual(tint.namingScheme(), hou.parmNamingScheme.RGBA)
        self.assertEqual(tint.defaultValue(), (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(self.node.parmTuple("tint").eval(), (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(params.read_parms(self.node, SPEC)["tint"], (1.0, 0.5, 0.25, 1.0))
        self.node.parmTuple("tint").set((0.0, 0.25, 0.5, 0.75))
        self.assertEqual(params.read_parms(self.node, SPEC)["tint"], (0.0, 0.25, 0.5, 0.75))
        self.node.parmTuple("tint").revertToDefaults()

    def test_the_toggle_menu(self):
        picks = self.template("picks")
        self.assertEqual(picks.menuType(), hou.menuType.StringToggle)
        self.assertIn("'gen_test', 'picks'", picks.itemGeneratorScript())
        self.assertEqual(picks.itemGeneratorScriptLanguage(), hou.scriptLanguage.Python)

    def test_hide_and_disable_follow_the_parameters(self):
        node = self.node
        states = {}
        for mode, usetex in (("a", 1), ("c", 1), ("b", 0)):
            node.parm("mode").set(mode)
            node.parm("usetex").set(usetex)
            node.updateParmStates()
            states[(mode, usetex)] = (node.parm("tex").isHidden(), node.parm("tex").isDisabled(),
                                      node.parm("tintr").isHidden())
        self.assertEqual(states[("a", 1)], (False, False, True))
        self.assertEqual(states[("c", 1)], (True, False, False))
        self.assertEqual(states[("b", 0)], (False, True, True))
        self.assertEqual(self.template("tex").conditionals()[hou.parmCondType.HideWhen], "{ mode != a mode != b }")
        node.parm("mode").revertToDefaults()
        node.parm("usetex").revertToDefaults()

    def test_values_read_the_same_with_or_without_the_folders(self):
        values = params.read_parms(self.node, SPEC)
        self.assertEqual(values["amount"], 2.0)
        self.assertEqual(values["mode"], "a")
        self.assertNotIn("box", values)

    def test_add_spare_parms_builds_the_same_kinds(self):
        geo = hou.node("/obj").createNode("geo")
        try:
            node = geo.createNode("null")
            params.add_spare_parms(node, SPEC)
            group = node.parmTemplateGroup()
            self.assertEqual(group.find("box").folderType(), hou.folderType.Simple)
            self.assertEqual(group.find("more").folderType(), hou.folderType.Collapsible)
            self.assertEqual(group.findFolder("Look").folderType(), hou.folderType.Tabs)
            self.assertEqual(group.find("tint").look(), hou.parmLook.ColorSquare)
            self.assertEqual(group.find("root").fileType(), hou.fileType.Directory)
            self.assertEqual(group.find("picks").menuType(), hou.menuType.StringToggle)
            self.assertEqual(group.find("tex").tags().get("filechooser_pattern"), "*.paa *.tga")
            self.assertEqual(group.find("tex").conditionals()[hou.parmCondType.DisableWhen], "{ usetex != 1 }")
        finally:
            geo.destroy()


if __name__ == "__main__":
    unittest.main()

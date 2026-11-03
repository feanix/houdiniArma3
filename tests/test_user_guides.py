"""Tests for the hand-written guides in docs/user: every interface text they quote exists in the real tools.

The guides mark quoted text with classes (see docs/user/README.md, "Quoted interface text"). This file finds those marks
and checks them:

- Under plain Python: dialog strings against ``python/a3ob_houdini/*_dialog.py``, shelf labels against the ``.shelf``
  files, node and parameter labels and menu entries against the parameter tables, the preset table against the
  built-in presets, and the starter node names against ``new_p3d.py``.
- Under hython: the two quick start jobs are run headless (New P3D starter, Validate, Save to Disk; Import a sample,
  Extract LOD, Mass, Assemble LODs, Save to Disk), and every node label, parameter label, menu entry and message that the
  guides quote must exist in what Houdini shows.

Purpose: a guide that quotes a label or message that the tools no longer show. This is the only check that catches it.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import ast
import glob
import os
import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser

from a3ob_core import p3d
from a3ob_houdini import nodes, settings

from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT, hou, install_all_assets

DOCS_DIR = os.path.join(REPO_ROOT, "docs", "user")
PAGES = ("index.html", "install.html", "quick-start-arma.html", "quick-start-houdini.html", "concepts.html")
GUIDES = PAGES[1:]
PACKAGE_DIR = os.path.join(REPO_ROOT, "python", "a3ob_houdini")
SHELF_FILE = os.path.join(REPO_ROOT, "houdini", "toolbar", "a3ob_selection_browser.shelf")
#: Built-in Houdini nodes the guides name. Houdini checks them in the hython test; plain Python only knows the names.
BUILTIN_NODES = {"Box", "Divide", "Group", "Add", "Transform"}
KINDS = ("ui-node", "ui-type", "ui-parm", "ui-menu", "ui-shelf", "ui-dialog", "ui-msg")
CRATE = os.path.join(FIXTURES, "sample_2_crate.p3d")


def clean(text):
    return " ".join(text.split())


class Quotes(HTMLParser):
    """Collects the marked quotes of a page: ``(kind, text, data-node)``, plus a few counts."""

    def __init__(self):
        super().__init__()
        self.quotes = []
        self.codes = []
        self.presets = {}  # option -> {preset name: text}
        self.todo = 0
        self.gui = 0
        self._capture = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "todo" in classes:
            self.todo += 1
        if "gui" in classes:
            self.gui += 1
        if tag == "tr":
            self._option = attrs.get("data-option")
        if self._capture is not None:
            return
        if tag in ("kbd", "samp", "code"):
            kinds = [name for name in classes if name in KINDS]
            self._capture = (tag, kinds[0] if kinds else "code", attrs.get("data-node"), [])
        elif tag == "td" and attrs.get("data-preset") and self._option:
            self._capture = (tag, "preset", (self._option, attrs["data-preset"]), [])

    def handle_data(self, data):
        if self._capture is not None:
            self._capture[3].append(data)

    def handle_endtag(self, tag):
        if self._capture is None or self._capture[0] != tag:
            return
        _, kind, extra, buffer = self._capture
        self._capture = None
        text = clean("".join(buffer))
        if kind == "code":
            self.codes.append(text)
        elif kind == "preset":
            self.presets.setdefault(extra[0], {})[extra[1]] = text
        else:
            self.quotes.append((kind, text, extra))


def parse_page(name):
    parser = Quotes()
    with open(os.path.join(DOCS_DIR, name), "r", encoding="utf-8") as file:
        parser.feed(file.read())
    return parser


def all_quotes(kind):
    found = []
    for name in PAGES:
        found += [(name, text, extra) for kind_, text, extra in parse_page(name).quotes if kind_ == kind]
    return found


def parm_rows(parms):
    for parm in parms:
        yield parm
        yield from parm_rows(parm.children)


def spec_labels():
    """Node labels, parameter labels by node type, and menu labels, from the parameter tables."""
    node_labels, parm_labels, menu_labels = set(), {}, set()
    for spec in nodes.all_specs().values():
        node_labels.add(spec.label)
        parm_labels[spec.type_name] = {parm.label for parm in parm_rows(spec.parms)}
        for parm in parm_rows(spec.parms):
            menu_labels.update(label for _, label in parm.items)
    return node_labels, parm_labels, menu_labels


def read_sources(paths):
    text = ""
    for path in paths:
        with open(path, "r", encoding="utf-8") as file:
            text += file.read() + "\n"
    return text


class TestGuidesAreFilled(unittest.TestCase):
    def test_no_placeholder_is_left_and_each_guide_quotes_the_interface(self):
        for name in GUIDES:
            page = parse_page(name)
            self.assertEqual(page.todo, 0, name)
            self.assertGreater(len(page.quotes), 5 if name != "install.html" else 3, name)

    def test_the_quick_starts_mark_the_steps_that_need_the_window(self):
        for name in ("quick-start-arma.html", "quick-start-houdini.html"):
            self.assertGreaterEqual(parse_page(name).gui, 1, name)

    def test_guides_have_one_h1_and_no_screenshots(self):
        for name in GUIDES:
            with open(os.path.join(DOCS_DIR, name), "r", encoding="utf-8") as file:
                text = file.read()
            self.assertEqual(text.count("<h1>"), 1, name)
            self.assertNotIn("<img", text, name)


class TestQuotedTextMatchesTheCode(unittest.TestCase):
    """Everything that does not need Houdini."""

    def test_dialog_strings_are_in_the_dialog_modules(self):
        paths = sorted(glob.glob(os.path.join(PACKAGE_DIR, "*_dialog.py")))
        paths.append(os.path.join(PACKAGE_DIR, "nodes", "p3d_export.py"))  # the "Nothing was written." dialog
        source = read_sources(paths)
        quotes = all_quotes("ui-dialog")
        self.assertGreaterEqual(len(quotes), 5)
        missing = [(page, text) for page, text, _ in quotes if text not in source]
        self.assertEqual(missing, [])

    def test_shelf_labels_are_in_the_shelf_file(self):
        root = ElementTree.parse(SHELF_FILE).getroot()
        labels = {element.get("label") for element in root.iter() if element.get("label")}
        quotes = all_quotes("ui-shelf")
        self.assertGreaterEqual(len(quotes), 5)
        self.assertEqual([(page, text) for page, text, _ in quotes if text not in labels], [])

    def test_the_shelf_tools_the_install_page_lists_are_all_the_tools(self):
        root = ElementTree.parse(SHELF_FILE).getroot()
        tools = {tool.get("label") for tool in root.iter("tool")}
        with open(os.path.join(DOCS_DIR, "install.html"), "r", encoding="utf-8") as file:
            parser = Quotes()
            parser.feed(file.read())
        listed = {text for kind, text, _ in parser.quotes if kind == "ui-shelf"} - {"A3OB"}
        self.assertEqual(listed, tools)

    def test_node_labels_parameter_labels_and_menu_entries_are_in_the_parameter_tables(self):
        node_labels, parm_labels, menu_labels = spec_labels()
        menu_labels |= set(settings.BUILTIN_PRESETS)  # the Preset menu is filled from the settings
        any_parm = set().union(*parm_labels.values())
        problems = []
        for page, text, _ in all_quotes("ui-node"):
            if text not in node_labels and text not in BUILTIN_NODES:
                problems.append("%s: node %s" % (page, text))
        for page, text, node in all_quotes("ui-parm"):
            if node in parm_labels:
                if text not in parm_labels[node]:
                    problems.append("%s: parameter %s of %s" % (page, text, node))
            elif node is None and text not in any_parm:
                problems.append("%s: parameter %s" % (page, text))
            elif node is not None and node.startswith("a3ob_"):
                problems.append("%s: unknown node type %s" % (page, node))
        for page, text, _ in all_quotes("ui-menu"):
            # Menus that are built from the input (Extract LOD) are checked in Houdini.
            if text not in menu_labels and not re.match(r"^\d+: ", text):
                problems.append("%s: menu entry %s" % (page, text))
        for page, text, _ in all_quotes("ui-type"):
            if text not in {spec.type_name for spec in nodes.all_specs().values()}:
                problems.append("%s: node type %s" % (page, text))
        self.assertEqual(problems, [])

    def test_the_preset_table_matches_the_built_in_presets(self):
        rows = parse_page("concepts.html").presets
        self.assertGreaterEqual(len(rows), 8)
        for option, cells in rows.items():
            self.assertIn(option, settings.PRESET_KEYS)
            self.assertEqual(set(cells), {settings.LOSSLESS, settings.RELEASE}, option)
            for name, text in cells.items():
                self.assertEqual(text == "On", bool(settings.BUILTIN_PRESETS[name][option]), "%s in %s" % (option, name))
        # Every on/off switch of a built-in preset is in the table.
        for option in settings.BUILTIN_PRESETS[settings.LOSSLESS]:
            self.assertIn(option, rows)

    def test_starter_node_names_in_the_text_exist(self):
        with open(os.path.join(PACKAGE_DIR, "new_p3d.py"), "r", encoding="utf-8") as file:
            tree = ast.parse(file.read())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "NODE_NAMES" for t in node.targets):
                names = list(ast.literal_eval(node.value))
        self.assertEqual(len(names), 12)
        used = set()
        for page in ("quick-start-arma.html", "quick-start-houdini.html"):
            for text in parse_page(page).codes:
                if re.match(r"^(res1|geometry|memory)_\w+$", text):
                    self.assertIn(text, names, page)
                    used.add(text)
        self.assertEqual(used, {name for name in names if re.match(r"^(res1|geometry|memory)_", name)})


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestQuickStartsRunInHoudini(unittest.TestCase):
    """Runs the two jobs of the quick starts without a window and keeps everything Houdini says."""

    @classmethod
    def setUpClass(cls):
        install_all_assets()
        cls.messages = []
        cls.menu_labels = set()
        cls.temp = tempfile.mkdtemp(prefix="a3ob_guides_")
        cls.networks = []
        cls.nodes_seen = []
        cls.run_job_1()
        cls.run_job_2()
        cls.run_extras()

    @classmethod
    def tearDownClass(cls):
        for network in cls.networks:
            if network.path() and hou.node(network.path()) is not None:
                network.destroy()
        shutil.rmtree(cls.temp, True)

    @classmethod
    def look(cls, node):
        """Cook ``node`` and keep its messages. Returns ``(errors, warnings, messages)``."""
        try:
            node.cook(force=True)
        except hou.OperationFailed:
            pass
        result = tuple(node.errors()), tuple(node.warnings()), tuple(node.messages())
        for group in result:
            cls.messages.extend(group)
        if node not in cls.nodes_seen:
            cls.nodes_seen.append(node)
        for parm in node.parms():
            try:
                cls.menu_labels.update(parm.menuLabels())  # empty for a parameter without a menu
            except hou.OperationFailed:
                pass
        return result

    @classmethod
    def keep(cls, node):
        """The messages a node holds now, without cooking it again (a cook would drop the ones Save to Disk left)."""
        result = tuple(node.errors()), tuple(node.warnings()), tuple(node.messages())
        for group in result:
            cls.messages.extend(group)
        return result

    # Job 1: a new model (quick-start-arma.html, "Job 1").
    @classmethod
    def run_job_1(cls):
        from a3ob_houdini import new_p3d
        from a3ob_houdini.nodes import p3d_export
        made = new_p3d.build_starter(None, "my_crate")
        cls.networks.append(made.network)
        cls.starter = made
        for node in made.nodes.values():
            cls.look(node)
        made.nodes["res1_mesh"].parmTuple("size").set((2.0, 1.0, 4.0))
        validate = made.network.createNode("a3ob_validate")
        validate.setFirstInput(made.nodes["assemble"])
        cls.job1_validate = cls.look(validate)
        # D24: the box's convex quads are fine, so the guide bypasses the Mass node to show a message (an info note).
        mass = made.nodes["geometry_mass"]
        mass.bypass(True)
        cls.job1_no_mass = cls.look(validate)
        mass.bypass(False)
        memory = made.nodes["memory_props"]
        memory.parm("lodtype").set("geometry")
        cls.job1_duplicate = cls.look(validate)
        memory.parm("lodtype").set("memory")
        cls.job1_fixed = cls.look(validate)
        export = hou.node("/obj/my_crate/export")
        cls.job1_file = os.path.join(cls.temp, "my_crate.p3d").replace("\\", "/")
        export.parm("file").set(cls.job1_file[:-len(".p3d")])  # the node adds the ending
        export.parm("save").pressButton()
        cls.job1_saved = cls.keep(export)
        export2 = made.network.createNode("a3ob_p3d_export", "second")
        export2.setFirstInput(made.nodes["assemble"])
        export2.parm("file").set(os.path.join(cls.temp, "script.p3d").replace("\\", "/"))
        p3d_export.save(export2)
        cls.job1_script_file = os.path.join(cls.temp, "script.p3d")

    # Job 2: change an existing model (quick-start-arma.html, "Job 2").
    @classmethod
    def run_job_2(cls):
        geo = hou.node("/obj").createNode("geo", "job_two")
        cls.networks.append(geo)
        imp = geo.createNode("a3ob_p3d_import")
        imp.parm("file").set(CRATE.replace("\\", "/"))
        cls.job2_import = cls.look(imp)
        empty = geo.createNode("a3ob_p3d_import", "no_file")
        cls.job2_no_file = cls.look(empty)
        extract = geo.createNode("a3ob_extract_lod")
        extract.setFirstInput(imp)
        cls.job2_extract_default = cls.look(extract)
        cls.extract_labels = list(extract.parm("lod").menuLabels())
        extract.parm("lod").set(extract.parm("lod").menuItems()[cls.extract_labels.index("3: Geometry")])
        cls.job2_extract = cls.look(extract)
        wrong = geo.createNode("a3ob_extract_lod", "wrong_input")
        wrong.setFirstInput(geo.createNode("box"))
        cls.job2_wrong_input = cls.look(wrong)
        mass = geo.createNode("a3ob_mass")
        mass.setFirstInput(extract)
        mass.parm("mode").set("total")
        mass.parm("mass_total").set(90)
        cls.job2_mass = cls.look(mass)
        assemble = geo.createNode("a3ob_assemble_lods")
        assemble.setInput(0, imp)
        assemble.setInput(1, mass)
        cls.job2_assemble = cls.look(assemble)
        export = geo.createNode("a3ob_p3d_export")
        export.setFirstInput(assemble)
        cls.job2_file = os.path.join(cls.temp, "crate_edited.p3d").replace("\\", "/")
        export.parm("file").set(cls.job2_file)
        export.parm("save").pressButton()
        cls.job2_saved = cls.keep(export)
        cls.job2_nodes = (imp, extract, mass, assemble, export)

    # Things the text says that the two jobs do not show.
    @classmethod
    def run_extras(cls):
        geo = hou.node("/obj").createNode("geo", "extras")
        cls.networks.append(geo)
        for kind in ("a3ob_lod_properties", "a3ob_material_assign", "a3ob_proxy", "box", "divide", "group", "add", "xform"):
            cls.look(geo.createNode(kind))
        # Concepts quotes the start of three rule messages. Bypass the nodes that satisfy them to make them appear.
        from a3ob_houdini import new_p3d
        made = new_p3d.build_starter(None, "rules_probe")
        cls.networks.append(made.network)
        validate = made.network.createNode("a3ob_validate")
        validate.setFirstInput(made.nodes["assemble"])
        for key in ("geometry_mass", "geometry_components"):
            made.nodes[key].bypass(True)
        cls.make_concave_quad(made.nodes["geometry_triangles"])
        cls.rules_probe = cls.look(validate)

    @classmethod
    def make_concave_quad(cls, triangles):
        """D24: convex quads are fine in a Geometry LOD, so the untriangulated case needs a quad with a reflex corner.
        The Divide node is bypassed and fed a wrangle that makes one."""
        wrangle = triangles.parent().createNode("attribwrangle", "concave_quad")
        wrangle.parm("class").set(0)
        wrangle.parm("snippet").set(
            "int a = addpoint(0, {0,0,0}); int b = addpoint(0, {2,0,0}); int c = addpoint(0, {0.5,0,0.5}); "
            "int d = addpoint(0, {0,0,2}); addprim(0, 'poly', a, b, c, d);")
        triangles.setFirstInput(wrangle)
        triangles.bypass(True)

    def all_text(self):
        return " \n ".join(clean(text) for text in self.messages)

    def test_job_1_messages(self):
        errors, warnings, messages = self.job1_validate
        self.assertEqual((errors, warnings), ((), ()))
        self.assertIn("3 LODs checked: 0 errors, 0 warnings.", messages)
        self.assertEqual(self.job1_no_mass[0], ())
        self.assertTrue(any(w.startswith("Geometry: the points have no mass") for w in self.job1_no_mass[1]))
        self.assertTrue(any(m.startswith("Geometry: this Geometry LOD has no mass") for m in self.job1_no_mass[2]),
                        self.job1_no_mass)
        self.assertTrue(any(e.startswith("Geometry: another LOD has the same type and resolution.")
                            for e in self.job1_duplicate[0]))
        self.assertEqual(self.job1_fixed[:2], ((), ()))

    def test_job_1_saves_three_lods_and_the_ending_is_added(self):
        self.assertTrue(any(m.startswith("Saved 3 LODs, ") for m in self.job1_saved[2]), self.job1_saved)
        self.assertEqual(len(p3d.read_p3d_file(self.job1_file, strict=False).lods), 3)
        self.assertEqual(len(p3d.read_p3d_file(self.job1_script_file, strict=False).lods), 3)

    def test_the_starter_has_the_documented_lods(self):
        names = [prim.attribValue("name") for prim in self.starter.nodes["assemble"].geometry().prims()]
        self.assertEqual(names, ["Resolution 1", "Geometry", "Memory"])
        self.assertTrue(self.starter.nodes["export"].isDisplayFlagSet())
        self.assertEqual(self.starter.nodes["assemble"].inputs()[0], None)

    def test_job_2_messages(self):
        self.assertTrue(any(m.startswith("7 LODs, 0 packed proxies, 0 inline proxies") for m in self.job2_import[2]))
        self.assertEqual(self.extract_labels, ["0: Resolution 1", "1: Resolution 2", "2: Shadow Volume 0", "3: Geometry",
                                               "4: Geometry Buoyancy", "5: Memory", "6: Roadway"])
        self.assertIn("Extracted LOD 3: Geometry, 16 points, 24 polygons.", self.job2_extract[2])
        self.assertIn("Total 90.0 kg on 16 points.", self.job2_mass[2][0])
        self.assertTrue(self.job2_assemble[2][0].endswith("1 replaced, 6 passed through, 0 added."))
        self.assertTrue(any(m.startswith("Saved 7 LODs, ") for m in self.job2_saved[2]), self.job2_saved)
        self.assertTrue(any("has no sound texture" in w for w in self.job2_saved[1]), self.job2_saved)
        self.assertEqual(self.job2_saved[0], ())

    def test_job_2_error_cases(self):
        self.assertTrue(any(e.startswith("Input is not a P3D geometry (packed LODs). Connect P3D Import or Assemble LODs.")
                            for e in self.job2_wrong_input[0]))
        self.assertTrue(any(e.startswith("P3D Import: no file is chosen. Pick a .p3d file in the File parameter.")
                            for e in self.job2_no_file[0]))

    def test_the_edited_crate_keeps_the_other_lods(self):
        before = p3d.read_p3d_file(CRATE, strict=False)
        after = p3d.read_p3d_file(self.job2_file, strict=False)
        self.assertEqual(len(after.lods), len(before.lods))

    def test_an_unedited_import_and_export_gives_the_same_bytes(self):
        # The guides say this holds for both sample files with the Lossless preset.
        geo = hou.node("/obj").createNode("geo", "roundtrip")
        self.networks.append(geo)
        for name in ("sample_1_character.p3d", "sample_2_crate.p3d"):
            source = os.path.join(FIXTURES, name)
            imp = geo.createNode("a3ob_p3d_import")
            imp.parm("file").set(source.replace("\\", "/"))
            export = geo.createNode("a3ob_p3d_export")
            export.setFirstInput(imp)
            target = os.path.join(self.temp, "same_" + name)
            export.parm("file").set(target.replace("\\", "/"))
            self.assertEqual(export.parm("preset").eval(), "Lossless")
            export.parm("save").pressButton()
            with open(source, "rb") as old, open(target, "rb") as new:
                self.assertEqual(old.read(), new.read(), name)

    def test_the_install_check_commands_work(self):
        import a3ob_houdini  # noqa: F401
        self.assertIsNotNone(hou.nodeType(hou.sopNodeTypeCategory(), "a3ob_p3d_import"))

    def test_the_presets_in_the_text_exist_in_the_menu(self):
        export = hou.node("/obj/my_crate/export")
        self.assertEqual(list(export.parm("preset").menuLabels()), [settings.LOSSLESS, settings.RELEASE])

    def test_every_quoted_node_label_exists_in_houdini(self):
        descriptions = {t.description() for t in hou.sopNodeTypeCategory().nodeTypes().values()}
        missing = [(page, text) for page, text, _ in all_quotes("ui-node") if text not in descriptions]
        self.assertEqual(missing, [])

    def test_every_quoted_node_type_exists_in_houdini(self):
        quotes = all_quotes("ui-type")
        self.assertGreaterEqual(len(quotes), 1)
        missing = [(page, text) for page, text, _ in quotes
                   if hou.nodeType(hou.sopNodeTypeCategory(), text) is None]
        self.assertEqual(missing, [])

    def test_every_quoted_parameter_label_exists_on_its_node(self):
        by_type = {}
        for node in self.nodes_seen:
            labels = by_type.setdefault(node.type().name(), set())
            labels.update(parm.parmTemplate().label() for parm in node.parms())
        a3ob_labels = set().union(*(labels for name, labels in by_type.items() if name.startswith("a3ob_")))
        problems = []
        for page, text, node in all_quotes("ui-parm"):
            known = by_type.get(node) if node else a3ob_labels
            if known is None or text not in known:
                problems.append((page, node, text))
        self.assertEqual(problems, [])

    def test_every_quoted_menu_entry_exists_in_a_menu(self):
        problems = [(page, text) for page, text, _ in all_quotes("ui-menu") if text not in self.menu_labels]
        self.assertEqual(problems, [])

    def test_every_quoted_message_was_seen(self):
        seen = self.all_text()
        quotes = all_quotes("ui-msg")
        self.assertGreaterEqual(len(quotes), 10)
        problems = [(page, text) for page, text, _ in quotes if text not in seen]
        self.assertEqual(problems, [])

    def test_the_validator_messages_the_concepts_page_quotes_exist(self):
        text = " ".join(self.rules_probe[1] + self.rules_probe[2])  # the mass message is an info note since D24
        for start in ("Geometry: the mesh is not triangulated", "Geometry: the LOD has no component selections",
                      "Geometry: this Geometry LOD has no mass"):
            self.assertIn(start, text)


if __name__ == "__main__":
    unittest.main()

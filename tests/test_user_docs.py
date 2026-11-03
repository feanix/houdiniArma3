"""Tests for the user docs in docs/user: generated pages are up to date, links resolve, each HDA carries its Help section.

The first three classes run under plain Python and under hython. The last one needs ``hou`` and skips without it.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import glob
import importlib.util
import os
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from urllib.parse import urlparse

from a3ob_houdini import hda_source, nodes

from tests.hou_support import HAS_HOU, OTLS_DIR, REPO_ROOT, hou, install_all_assets

DOCS_DIR = os.path.join(REPO_ROOT, "docs", "user")
BUILD_COMMAND = "python scripts/build_user_docs.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_user_docs", os.path.join(REPO_ROOT, "scripts",
                                                                                   "build_user_docs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text(path):
    with open(path, "r", encoding="utf-8", newline=None) as file:
        return file.read()


def html_files():
    found = glob.glob(os.path.join(DOCS_DIR, "*.html")) + glob.glob(os.path.join(DOCS_DIR, "reference", "*.html"))
    return sorted(found)


class LinkParser(HTMLParser):
    """Collects every ``href``, ``src`` and element id of a page, and the tags that must not be there."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()
        self.bad_tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if attrs.get(name) is not None:
                self.links.append(attrs[name])
        if tag in ("script", "iframe", "object", "embed"):
            self.bad_tags.append(tag)


def parse(path):
    parser = LinkParser()
    parser.feed(read_text(path))
    return parser


class TestGeneratedPagesAreCurrent(unittest.TestCase):
    def test_regenerating_into_a_temp_folder_changes_nothing(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as temp:
            copy = os.path.join(temp, "user")
            shutil.copytree(DOCS_DIR, copy)
            builder.build(copy)
            expected = sorted(os.path.relpath(path, copy) for path in glob.glob(os.path.join(copy, "**", "*"),
                                                                                recursive=True) if os.path.isfile(path))
            actual = sorted(os.path.relpath(path, DOCS_DIR) for path in glob.glob(os.path.join(DOCS_DIR, "**", "*"),
                                                                                  recursive=True)
                            if os.path.isfile(path))
            self.assertEqual(actual, expected, "files differ; run: %s" % BUILD_COMMAND)
            stale = [name for name in expected
                     if read_text(os.path.join(copy, name)) != read_text(os.path.join(DOCS_DIR, name))]
            self.assertEqual(stale, [], "docs/user is out of date (%s); run: %s" % (", ".join(stale), BUILD_COMMAND))

    def test_the_build_is_deterministic(self):
        builder = load_builder()
        self.assertEqual(builder.render_all(), builder.render_all())

    def test_check_mode_agrees(self):
        builder = load_builder()
        self.assertEqual(builder.differences(builder.render_all()), [], "run: %s" % BUILD_COMMAND)


class TestSiteLinks(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        pages = html_files()
        self.assertGreater(len(pages), 10)
        ids = {path: parse(path).ids for path in pages}
        problems = []
        for path in pages:
            parser = parse(path)
            if parser.bad_tags:
                problems.append("%s uses %s" % (os.path.relpath(path, DOCS_DIR), parser.bad_tags))
            for link in parser.links:
                parts = urlparse(link)
                if parts.scheme or parts.netloc or link.startswith("/"):
                    problems.append("%s: not a relative link: %s" % (os.path.relpath(path, DOCS_DIR), link))
                    continue
                target = os.path.normpath(os.path.join(os.path.dirname(path), parts.path)) if parts.path else path
                if not os.path.isfile(target):
                    problems.append("%s: missing file: %s" % (os.path.relpath(path, DOCS_DIR), link))
                elif parts.fragment and target in ids and parts.fragment not in ids[target]:
                    problems.append("%s: missing anchor: %s" % (os.path.relpath(path, DOCS_DIR), link))
        self.assertEqual(problems, [])

    def test_every_page_uses_the_stylesheet_and_the_navigation(self):
        for path in html_files():
            text = read_text(path)
            self.assertIn('rel="stylesheet"', text, path)
            self.assertIn("style.css", text, path)
            self.assertIn('<nav class="site"', text, path)
            self.assertIn("<!-- footer:start -->", text, path)
            self.assertIn("GPL-3.0-or-later", text, path)

    def test_the_four_guides_are_in_the_navigation(self):
        text = read_text(os.path.join(DOCS_DIR, "index.html"))
        for name in ("install.html", "quick-start-arma.html", "quick-start-houdini.html", "concepts.html"):
            self.assertIn('href="%s"' % name, text)
            self.assertTrue(os.path.isfile(os.path.join(DOCS_DIR, name)))

    def test_credits_and_licence(self):
        text = read_text(os.path.join(DOCS_DIR, "about.html"))
        for word in ("MrClock", "Alwarren", "ArmAToolbox", "GPL-3.0-or-later"):
            self.assertIn(word, text)

    def test_text_has_no_internal_ids(self):
        import re
        pattern = re.compile(r"(?<![\w&])#\d{2,3}\b|\bD\d{1,2}\b|section \d|ux-tools|houdini-data-model")
        found = []
        for path in html_files():
            for match in pattern.finditer(read_text(path)):
                found.append("%s: %s" % (os.path.relpath(path, DOCS_DIR), match.group(0)))
        self.assertEqual(found, [])


class TestReferenceCoverage(unittest.TestCase):
    def test_every_node_has_a_page(self):
        otls = sorted(name[:-4] for name in os.listdir(OTLS_DIR) if name.endswith(".hda"))
        self.assertGreaterEqual(len(otls), 9)
        for name in otls:
            self.assertTrue(os.path.isfile(os.path.join(DOCS_DIR, "reference", name + ".html")), name)
        for spec in nodes.all_specs().values():
            self.assertIn(spec.type_name, otls)

    def test_every_node_page_lists_every_parameter(self):
        for spec in nodes.all_specs().values():
            text = read_text(os.path.join(DOCS_DIR, "reference", spec.type_name + ".html"))
            ids = parse(os.path.join(DOCS_DIR, "reference", spec.type_name + ".html")).ids
            self.assertIn(spec.label, text)
            for parm in spec.parms:
                self.assertIn(parm.name, ids, "%s: %s" % (spec.type_name, parm.name))

    def test_every_shelf_tool_has_a_page(self):
        builder = load_builder()
        tools = builder.read_tools()
        self.assertGreaterEqual(len(tools), 6)
        for name, label, _ in tools:
            self.assertTrue(os.path.isfile(os.path.join(DOCS_DIR, builder.tool_page_path(name).replace("/", os.sep))),
                            name)

    def test_the_reference_index_links_every_page(self):
        index = parse(os.path.join(DOCS_DIR, "reference", "index.html")).links
        for path in glob.glob(os.path.join(DOCS_DIR, "reference", "a3ob_*.html")) + glob.glob(
                os.path.join(DOCS_DIR, "reference", "tool-*.html")):
            self.assertIn(os.path.basename(path), index)


def all_labels(parms):
    for parm in parms:
        if parm.kind != "folder":  # a folder is a heading in the Help, not a "label:" entry
            yield parm.label
        for label in all_labels(parm.children):
            yield label


class TestHelpSection(unittest.TestCase):
    def test_every_asset_source_carries_a_help_section_and_no_help_url(self):
        for spec in nodes.all_specs().values():
            files = hda_source.render(spec)
            section = hda_source.section_name(spec)
            self.assertNotIn(section + "/HelpUrl", files)
            self.assertIn("Help\tHelp", files[section + "/Sections.list"])
            self.assertNotIn("HelpUrl", files[section + "/Sections.list"])
            text = files[section + "/Help"]
            self.assertTrue(text.startswith("= %s =\n" % spec.label), spec.type_name)
            for line in ("#type: node", "#context: sop", "#internal: " + spec.type_name, "@parameters"):
                self.assertIn(line + "\n", text, spec.type_name)
            self.assertIn('"""%s"""' % spec.summary, text)

    def test_the_help_names_every_parameter_label_and_menu_choice(self):
        for spec in nodes.all_specs().values():
            text = hda_source.render(spec)[hda_source.section_name(spec) + "/Help"]
            for label in all_labels(spec.parms):
                self.assertIn(label + ":\n", text, "%s: %s" % (spec.type_name, label))

    def test_the_last_line_names_the_html_page_and_is_not_a_link(self):
        for spec in nodes.all_specs().values():
            text = hda_source.render(spec)[hda_source.section_name(spec) + "/Help"]
            self.assertNotIn("file://", text)
            last = text.rstrip("\n").split("\n")[-1]
            self.assertIn("docs/user/reference/%s.html" % spec.type_name, last)
            self.assertTrue(os.path.isfile(os.path.join(DOCS_DIR, "reference", spec.type_name + ".html")))

    def test_the_html_reference_and_the_help_share_their_text(self):
        builder = load_builder()
        for spec in nodes.all_specs().values():
            help_text = hda_source.render(spec)[hda_source.section_name(spec) + "/Help"]
            _, page = builder.node_page(spec)
            for parm in spec.parms:
                if parm.kind == "menu" and parm.items:
                    intro, _ = builder.split_menu_help(parm)
                    self.assertIn(intro, help_text)
                    self.assertIn(builder.esc(intro), page)


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestHelpSectionInHoudini(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_all_assets()

    def test_definitions_carry_the_help_section_and_no_help_url(self):
        for spec in nodes.all_specs().values():
            node_type = hou.nodeType(hou.sopNodeTypeCategory(), spec.type_name)
            self.assertIsNotNone(node_type, spec.type_name)
            definition = node_type.definition()
            self.assertFalse(definition.hasSection("HelpUrl"), spec.type_name)
            self.assertTrue(definition.hasSection("Help"), spec.type_name)
            self.assertEqual(node_type.helpUrl(), "", spec.type_name)

    def test_each_help_section_names_every_parameter_label(self):
        for spec in nodes.all_specs().values():
            node_type = hou.nodeType(hou.sopNodeTypeCategory(), spec.type_name)
            text = node_type.definition().sections()["Help"].contents()
            self.assertIn("#internal: " + spec.type_name, text)
            for label in all_labels(spec.parms):
                self.assertIn(label + ":", text, "%s: %s" % (spec.type_name, label))

    def test_the_live_help_server_serves_the_page_when_it_runs(self):
        import urllib.request
        try:
            base = hou.helpServerUrl()
            with urllib.request.urlopen(base + "nodes/sop/a3ob_mass", timeout=10) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as error:  # no help server in this session (for example plain hython): nothing to check
            self.skipTest("no help server: %s" % error)
        if "The full page for this node is" not in body:
            # The server may belong to another Houdini session that has other copies of the assets installed.
            self.skipTest("the help server does not serve this checkout's assets")
        self.assertIn("docs/user/reference/a3ob_mass.html", body)


if __name__ == "__main__":
    unittest.main()

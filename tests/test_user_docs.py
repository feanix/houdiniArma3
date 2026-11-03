"""Tests for the user docs in docs/user: generated pages are up to date, links resolve, F1 reaches a page.

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


class TestHelpLink(unittest.TestCase):
    def test_every_asset_source_carries_the_link(self):
        for spec in nodes.all_specs().values():
            files = hda_source.render(spec)
            section = hda_source.section_name(spec)
            url = files[section + "/HelpUrl"]
            self.assertEqual(url, "file:///$A3OB_HOUDINI/docs/user/reference/%s.html" % spec.type_name)
            self.assertIn("HelpUrl\tHelpUrl", files[section + "/Sections.list"])
            self.assertTrue(os.path.isfile(os.path.join(DOCS_DIR, "reference", spec.type_name + ".html")))


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestHelpLinkInHoudini(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_all_assets()
        cls.saved = os.environ.get("A3OB_HOUDINI")

    def test_definition_carries_the_url_and_it_resolves_from_the_package_folder(self):
        # The package file sets A3OB_HOUDINI to the add-on folder (the repository root here).
        hou.putenv("A3OB_HOUDINI", REPO_ROOT.replace("\\", "/"))
        try:
            for spec in nodes.all_specs().values():
                node_type = hou.nodeType(hou.sopNodeTypeCategory(), spec.type_name)
                self.assertIsNotNone(node_type, spec.type_name)
                definition = node_type.definition()
                self.assertTrue(definition.hasSection("HelpUrl"), spec.type_name)
                url = node_type.helpUrl()
                self.assertEqual(url, "file:///$A3OB_HOUDINI/docs/user/reference/%s.html" % spec.type_name)
                expanded = hou.expandString(url)
                self.assertTrue(expanded.startswith("file:///"), expanded)
                path = expanded[len("file:///"):]
                self.assertTrue(os.path.isfile(path), path)
        finally:
            if self.saved is None:
                hou.unsetenv("A3OB_HOUDINI")
            else:
                hou.putenv("A3OB_HOUDINI", self.saved)


if __name__ == "__main__":
    unittest.main()

"""Tooltips and tool texts (task #44, node QA part Z).

Purpose: stop a parameter, a multiparm row parameter or a shelf tool from shipping with an empty tooltip, or with a
developer note in place of help; and prove that the OnCreated and OnLoaded event scripts do nothing (no Python error)
when the add-on cannot be imported (for example with HOUDINI_PACKAGE_SKIP=1).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import glob
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree

from a3ob_houdini import hda_source, nodes, params
from tests.hou_support import HAS_HOU, REPO_ROOT

OTLS = os.path.join(REPO_ROOT, "houdini", "otls")
SHELF_GLOB = os.path.join(REPO_ROOT, "houdini", "toolbar", "*.shelf")

#: Words that show a tooltip was written for developers: spec or rule ids, task numbers.
_INTERNAL = re.compile(r"\(Rule T2|\bT2-[A-Z]|\bD\d{1,2}\b|\bspec(?:ification)?\b|\bsection \d|#\d+", re.I)


def every_parm(parms):
    """Each parameter of a table, folders opened and multiparm rows included, folders themselves left out."""
    for parm in parms:
        if parm.kind == "folder":
            yield from every_parm(parm.children)
            continue
        yield parm
        yield from every_parm(parm.children)


class TestTooltipsInTheTables(unittest.TestCase):
    def test_every_parameter_has_a_tooltip(self):
        count = 0
        for kind, spec in nodes.all_specs().items():
            for parm in every_parm(spec.parms):
                count += 1
                self.assertTrue(parm.help.strip(), "%s.%s has an empty tooltip" % (kind, parm.name))
        self.assertGreater(count, 80)

    def test_no_tooltip_names_an_internal_id(self):
        for kind, spec in nodes.all_specs().items():
            for parm in every_parm(spec.parms):
                found = _INTERNAL.search(parm.help)
                self.assertIsNone(found, "%s.%s: %r" % (kind, parm.name, found and found.group(0)))

    def test_the_generated_dialog_scripts_give_every_parameter_a_help_line(self):
        for kind, spec in nodes.all_specs().items():
            text = hda_source.render(spec)[hda_source.section_name(spec) + "/DialogScript"]
            blocks = re.split(r"^\s*parm \{$", text, flags=re.M)[1:]
            self.assertTrue(blocks, kind)
            for block in blocks:
                name = re.search(r'^\s*name\s+"([^"]+)"', block, re.M).group(1)
                self.assertRegex(block, r'(?m)^\s*help\s+"[^"]+', "%s.%s has no help line" % (kind, name))

    def test_shelf_tools_have_user_help(self):
        tools = 0
        for path in glob.glob(SHELF_GLOB):
            for tool in ElementTree.parse(path).getroot().findall("tool"):
                tools += 1
                text = (tool.findtext("helpText") or "").strip()
                self.assertTrue(text, tool.get("name"))
                self.assertNotIn("The work is in", text, tool.get("name"))
                self.assertNotRegex(text, r"\w+\.py\b", tool.get("name"))
        self.assertGreaterEqual(tools, 6)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestTooltipsOfTheInstalledAssets(unittest.TestCase):
    def test_every_parameter_template_of_every_node_type_has_help(self):
        from tests import hou_support as hs
        hs.install_all_assets()
        import hou

        def walk(templates):
            for template in templates:
                if template.type() == hou.parmTemplateType.FolderSet:
                    continue
                if template.type() == hou.parmTemplateType.Folder:
                    yield from walk(template.parmTemplates())
                    continue
                yield template

        checked = 0
        for kind in nodes.KINDS:
            node_type = hou.nodeType(hou.sopNodeTypeCategory(), params.type_name(kind))
            self.assertIsNotNone(node_type, kind)
            for template in walk(node_type.parmTemplateGroup().entries()):
                checked += 1
                self.assertTrue(template.help().strip(), "%s.%s has an empty tooltip" % (kind, template.name()))
        self.assertGreater(checked, 60)


# Run in a child process: the add-on cannot be imported (a finder refuses it), then a script runs.
_CHILD = '''
import sys

class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name == "a3ob_houdini" or name.startswith("a3ob_houdini."):
            raise ModuleNotFoundError("No module named 'a3ob_houdini'", name="a3ob_houdini")
        return None

sys.meta_path.insert(0, Refuse())
for name in [n for n in sys.modules if n.split(".")[0] == "a3ob_houdini"]:
    del sys.modules[name]
for text in sys.argv[1:]:
    exec(compile(text, "<event script>", "exec"), {"kwargs": {"node": None}})
print("QUIET")
'''


class TestEventScriptsWithoutTheAddon(unittest.TestCase):
    def run_scripts(self, *scripts):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "child.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_CHILD)
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            return subprocess.run([sys.executable, path, *scripts], capture_output=True, text=True, env=environment,
                                  cwd=folder, timeout=120)

    def test_oncreated_and_onloaded_do_nothing_when_the_addon_is_missing(self):
        done = self.run_scripts(params.CREATED_SCRIPT % "p3d_export", params.LOADED_SCRIPT % "mass")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("QUIET", done.stdout)
        self.assertNotIn("Error", done.stderr)

    def test_the_generated_event_sections_use_the_guarded_scripts(self):
        for kind in nodes.KINDS:
            spec = nodes.load(kind).SPEC
            files = hda_source.render(spec)
            section = hda_source.section_name(spec)
            for event in ("OnCreated", "OnLoaded"):
                if section + "/" + event in files:
                    self.assertIn("except ImportError", files[section + "/" + event], "%s %s" % (kind, event))

    def test_an_unguarded_script_does_fail_in_the_same_child(self):
        # The child really makes the add-on unimportable: the old one-line script raises there.
        done = self.run_scripts("import a3ob_houdini.entry as e; e.created('p3d_export', kwargs)")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ModuleNotFoundError", done.stderr)


if __name__ == "__main__":
    unittest.main()

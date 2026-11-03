"""The LOD Properties layout (node QA, owner walkthrough 2026-11-03): two group boxes, tooltips on every parameter.

Purpose: catch a regression of the owner's approved layout. The expected tree below is the owner's saved dialog
script (folder types, labels, children order); the folder names are our stable ones, not the owner's folder0 and
folder1, so names of folders are not compared. A second test catches a parameter with no tooltip.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from a3ob_houdini import hda_source, nodes

from tests.hou_support import HAS_HOU, hou

SPEC = nodes.load("lod_properties").SPEC

#: The owner's tree: (kind, label, children) for folders and multiparms, (name,) for plain parameters.
OWNER_TREE = [
    ("fromin",), ("settype",), ("lodtype",), ("resolution",), ("propmode",),
    ("Simple", "Properties", [("MultiparmBlock", "Properties", [("prop_name#",), ("prop_value#",)])]),
    ("Simple", "LOD Copies", [("MultiparmBlock", "LOD Copies", [("copy_type#",), ("copy_res#",)])]),
]


def _walk(parms):
    for parm in parms:
        yield parm
        if parm.children:
            yield from _walk(parm.children)


class TestTooltips(unittest.TestCase):
    def test_every_parameter_has_a_tooltip(self):
        for parm in _walk(SPEC.parms):
            if parm.kind != "folder":
                self.assertTrue(parm.help.strip(), parm.name)


@unittest.skipUnless(HAS_HOU, "needs hython")
class TestLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="a3ob_lodprops_test_")
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

    def test_the_tree_matches_the_owner_layout(self):
        def tree(templates):
            out = []
            for template in templates:
                if isinstance(template, hou.FolderParmTemplate):
                    out.append((template.folderType().name(), template.label(), tree(template.parmTemplates())))
                else:
                    out.append((template.name(),))
            return out

        mine = [t for t in self.node.parmTemplateGroup().entries() if not t.name().startswith("label")]
        self.assertEqual(tree(mine), OWNER_TREE)
        self.assertEqual([t.name() for t in mine[5:]], ["props_box", "copies_box"])

    def test_the_parameters_keep_their_names_so_old_files_load(self):
        for name in ("props", "copies", "propmode", "settype", "lodtype", "resolution"):
            self.assertIsNotNone(self.node.parm(name), name)


if __name__ == "__main__":
    unittest.main()

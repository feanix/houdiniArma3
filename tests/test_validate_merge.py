"""D15 in hython: Import, add one proxy, Assemble, Validate gives no merge warning; a merge of two imported LODs warns.

Purpose: the count heuristic fired on an added proxy, although the LOD uid is exact. This catches a return of that
warning, and a uid check that stopped working. Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from tests.hou_support import FIXTURES, HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_node_proxy import PATH, points_geometry


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestMergeCheck(NodeTestCase):
    def importer(self):
        node = self.network.createNode("a3ob_p3d_import")
        node.parm("file").set(os.path.join(FIXTURES, "sample_2_crate.p3d"))
        self.assertEqual(cook(node).errors, ())
        return node

    def extract(self, source, lod):
        node = self.network.createNode("a3ob_extract_lod")
        node.setInput(0, source)
        node.parm("lod").set(str(lod))
        return node

    def merge_warnings(self, node):
        return [text for text in cook(node).warnings if "merged" in text]

    def test_a_proxy_added_to_an_imported_lod_gives_no_merge_warning(self):
        imported = self.importer()
        proxy = self.network.createNode("a3ob_proxy")
        proxy.setInput(0, self.extract(imported, 0))
        proxy.setInput(1, make_source(self.network, points_geometry([(0.0, 1.0, 0.0)])))
        proxy.parm("path").set(PATH)
        self.assertEqual(cook(proxy).errors, ())
        assemble = self.network.createNode("a3ob_assemble_lods")
        assemble.setInput(0, imported)
        assemble.setInput(1, proxy)
        self.assertEqual(cook(assemble).errors, ())
        validate = self.network.createNode("a3ob_validate")
        validate.setInput(0, assemble)
        result = cook(validate)
        self.assertEqual(result.errors, ())
        self.assertEqual(self.merge_warnings(validate), [], result.warnings)

    def test_a_merge_of_two_imported_lods_still_warns(self):
        imported = self.importer()
        merge = self.network.createNode("merge")
        merge.setInput(0, self.extract(imported, 0))
        merge.setInput(1, self.extract(imported, 1))
        validate = self.network.createNode("a3ob_validate")
        validate.setInput(0, merge)
        found = self.merge_warnings(validate)
        self.assertEqual(len(found), 1, found)
        self.assertIn("uids", found[0])


if __name__ == "__main__":
    unittest.main()

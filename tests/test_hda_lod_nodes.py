"""The LOD Properties and Material Assign assets load from houdini/otls and cook in a real hython (D13).

Same method as test_hda_node.py: a child hython with HOUDINI_PATH including <repo>/houdini. The child builds a real
chain (P3D Import, Blast, Unpack, LOD Properties, Material Assign). Needs ``hou``; skipped under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import subprocess
import sys
import unittest

from a3ob_core.p3d import lod_types
from a3ob_houdini import names
from tests.hou_support import FIXTURES, HAS_HOU, REPO_ROOT

CHILD = os.path.join(REPO_ROOT, "tests", "hda_child_check_lod.py")


def run_child():
    environment = dict(os.environ)
    environment["HOUDINI_PATH"] = os.path.join(REPO_ROOT, "houdini").replace("\\", "/") + ";&"
    environment["PYTHONPATH"] = os.path.join(REPO_ROOT, "python")
    done = subprocess.run([sys.executable, CHILD, os.path.join(FIXTURES, "sample_2_crate.p3d")],
                          env=environment, capture_output=True, text=True, timeout=300)
    lines = [line for line in done.stdout.splitlines() if line.startswith("A3OB_RESULT ")]
    if not lines:
        raise AssertionError("the child hython printed no result (exit %d)\n%s\n%s" % (
            done.returncode, done.stdout[-2000:], done.stderr[-2000:]))
    return json.loads(lines[0][len("A3OB_RESULT "):])


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestLodNodeAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_child()

    def test_assets_load_from_the_folders_with_the_table_parameters(self):
        r = self.result["lod_properties"]
        self.assertTrue(r["found"])
        self.assertTrue(r["library"].endswith("houdini/otls/a3ob_lod_properties.hda"), r["library"])
        self.assertEqual(r["label"], "LOD Properties")
        self.assertEqual(r["inputs"], [1, 1])
        self.assertEqual(r["parms"], ["fromin", "settype", "lodtype", "resolution", "propmode",
                                       "props_box", "props", "copies_box", "copies"])
        self.assertEqual(r["submenu"], [["A3OB"]])
        self.assertEqual(r["pythoncook"], "import a3ob_houdini.entry as e; e.cook('lod_properties', hou.pwd())")
        r = self.result["material_assign"]
        self.assertTrue(r["found"])
        self.assertTrue(r["library"].endswith("houdini/otls/a3ob_material_assign.hda"), r["library"])
        self.assertEqual(r["label"], "Material Assign")
        self.assertEqual(r["inputs"], [1, 1])
        self.assertEqual(r["parms"], ["group", "grouptype", "settex", "texsource", "texture", "texcolor", "colortype", "color",
                                      "raw", "commonproc", "setmat", "material", "commonmat"])
        self.assertEqual(r["submenu"], [["A3OB"]])
        self.assertEqual(r["pythoncook"], "import a3ob_houdini.entry as e; e.cook('material_assign', hou.pwd())")

    def test_lod_properties_cooks_a_real_chain_and_writes_the_detail_attributes(self):
        r = self.result
        self.assertEqual(len(r["unconnected_errors"]), 1)  # Houdini's own "Not enough sources" for a required input
        self.assertEqual(r["props_errors"], [])
        self.assertEqual(r["props_type"], int(lod_types.LodType.GEOMETRY))
        self.assertEqual(r["props_resolution"], 0.0)
        self.assertEqual(names.bits_float(r["props_signature"]), lod_types.encode_signature(lod_types.LodType.GEOMETRY))
        self.assertEqual(r["props_names"], ["lodnoshadow", "class", "AUTOCENTER"])
        self.assertEqual(r["props_values"], ["1", "box", "0"])
        self.assertEqual(r["props_copy_types"], [0])
        self.assertEqual(r["props_copy_resolutions"], [4.0])
        self.assertGreater(r["props_points"], 0)
        self.assertTrue(r["props_messages"][-1].startswith("Geometry, signature 1e+13, 3 properties, 1 copy."),
                        r["props_messages"])

    def test_material_assign_cooks_after_it_and_normalises_slashes(self):
        r = self.result
        self.assertEqual(r["material_errors"], [])
        self.assertEqual(r["material_detail_type"], int(lod_types.LodType.GEOMETRY))  # LOD data flows through
        self.assertGreater(r["material_set_count"], 0)
        self.assertIn("a3\\box\\lid_co.paa", r["material_texture_values"])
        self.assertEqual(r["material_material_values"].count("a3\\box\\lid.rvmat"), 1)
        self.assertTrue(r["material_messages"][0].startswith("Set on %d polygons" % r["material_set_count"]))
        self.assertEqual(r["color_errors"], [])
        self.assertEqual(r["color_values"], ["#(argb,8,8,3)color(0.250,0.500,0.750,1.000,CA)"])

    def test_read_from_input_button_fills_the_parameters(self):
        r = self.result
        self.assertEqual(r["fromin_type"], "geometry")
        self.assertEqual(r["fromin_props"], 3)
        self.assertEqual(r["fromin_names"], ["lodnoshadow", "class"])
        self.assertEqual(r["fromin_values"], ["1", "box"])
        self.assertEqual(r["fromin_copies"], 1)
        self.assertEqual(r["fromin_copy_type"], "visual")
        self.assertEqual(r["fromin_copy_res"], 4.0)
        self.assertEqual(r["fromin_errors"], [])
        self.assertTrue(r["fromin_same_signature"])

    def test_python_sop_fallback_builds_menus_and_multiparms(self):
        self.assertEqual(self.result["fallback_parms"], ["lodtype", "propmode", "props_box", "copies_box"])
        self.assertTrue(self.result["fallback_multiparm_row"])
        self.assertEqual(self.result["fallback_read"], "memory")


if __name__ == "__main__":
    unittest.main()

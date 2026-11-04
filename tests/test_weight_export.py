"""Export and the weight rules on the Samples character (design tests 1.7 and 1.8, task #63). Needs ``hou`` with KineFX:
do not run it with HOUDINI_PACKAGE_SKIP=1. The Samples are read from the owner's disk and skip when absent.

Purpose: prove that the export writes cleaned bone weights unchanged (same selections, same weight bytes, same weights
after a re-import) and that the three rules warn on the raw sample and are quiet on the cleaned one.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest

from a3ob_core import p3d
from a3ob_core.rig import read_cfg_file, resolve_skeleton
from a3ob_houdini import messages, nodes, settings
from a3ob_houdini.p3d_export import rig_bones
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"
#: The LODs the sample weights (character-rigging.md 3.3).
LODS = ("Resolution 1", "Resolution 2", "Resolution 3", "Resolution 4", "View - Pilot", "Shadow Volume 0",
        "Shadow Volume 10")
RULES = ("T2-WGT4", "T2-WGTSUM", "T2-WGTNONE")


def bone_bytes(lod, bones):
    """``{selection name: vertex weight bytes}`` of the bone selections of a LOD read from a file."""
    return {s.name: bytes(s.vertex_weights) for s in lod.selections() if s.name.lower() in bones}


@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestBoneList(unittest.TestCase):
    def test_built_in_names_are_the_samples_skeleton(self):
        cfg = read_cfg_file(samples_support.path(SAMPLE_CFG))
        names = [bone for bone, _ in resolve_skeleton(cfg, "OFP2_ManSkeleton").bones]
        self.assertEqual(sorted(n.lower() for n in names), sorted(n.lower() for n in rig_bones.OFP2_MANSKELETON_BONES))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestExportOnSamples(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="a3ob_wgtexp_")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        super().tearDown()

    def node(self, kind, *inputs, **parms):
        node = self.network.createNode(kind)
        for number, source in enumerate(inputs):
            node.setInput(number, source)
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def export(self, source, name):
        out = os.path.join(self.folder, name).replace("\\", "/")
        node = self.node("a3ob_p3d_export", source, file=out)
        nodes.load("p3d_export").apply_preset(node, "Lossless", settings.Settings())
        node.parm("file").set(out)
        node.parm("maxmessages").set(100000)
        report = nodes.load("p3d_export").run_save(node)
        self.assertFalse(report.has_errors, report.texts(messages.ERROR))
        return out, report

    def build(self, clean):
        """The sample, with the weighted LODs cleaned (or not), as one P3D geometry."""
        importer = self.node("a3ob_p3d_import", file=samples_support.path(SAMPLE).replace("\\", "/"))
        assemble = self.node("a3ob_assemble_lods", importer)
        self.lods = {}
        skeleton = self.node("a3ob_rig_skeleton", cfg=samples_support.path(SAMPLE_CFG).replace("\\", "/"),
                             pivots=samples_support.path(SAMPLE_PIVOTS).replace("\\", "/"))
        self.assertEqual(cook(skeleton).errors, ())
        for number, name in enumerate(LODS):
            lod = self.node("a3ob_extract_lod", importer, lod=name)
            if clean:
                lod = self.node("a3ob_weight_cleanup", lod, skeleton)
                self.assertEqual(cook(lod).errors, (), name)
            assemble.setInput(number + 1, lod)
            self.lods[name] = lod
        self.assertEqual(cook(assemble).errors, ())
        return assemble

    def test_cleaned_sample_exports_unchanged_and_the_rules_are_quiet(self):
        bones = rig_bones.bone_name_set()
        cleaned = self.build(True)
        out, report = self.export(cleaned, "cleaned.p3d")
        self.assertEqual([t for t in report.texts(messages.WARNING) if any("rule " + r in t for r in RULES)], [])
        written = p3d.read_p3d_file(out, strict=False)
        source = p3d.read_p3d_file(samples_support.path(SAMPLE), strict=False)
        # the weights on the cleaned geometry, as the weight bytes the export should write: k/254 -> 255 - k (1 for k = 254)
        again_node = self.node("a3ob_p3d_import", file=out)
        self.assertEqual(cook(again_node).errors, ())
        for name in LODS:
            with self.subTest(lod=name):
                after = [lod for lod in written.lods if lod.name == name][0]
                before = [lod for lod in source.lods if lod.name == name][0]
                # the same set of selections
                self.assertEqual(sorted(s.name for s in after.selections()), sorted(s.name for s in before.selections()))
                weights = after.selections()
                for selection in weights:
                    if selection.name.lower() not in bones:
                        continue
                    for point, value in enumerate(selection.vertex_weights):
                        if value:
                            self.assertTrue(value == 1 or 1 <= 255 - value <= 254, (selection.name, point, value))
                # a re-import reads the same weights
                extract = self.node("a3ob_extract_lod", again_node, lod=name)
                self.assertEqual(cook(extract).errors, ())
                geo, base = extract.geometry(), self.lods[name].geometry()
                for selection in weights:
                    if selection.name.lower() not in bones:
                        continue
                    self.assertEqual(self.weights_of(geo, selection.name), self.weights_of(base, selection.name),
                                     selection.name)

    @staticmethod
    def weights_of(geo, name):
        """``{point number: weight}`` of a selection, the weight as the nearest byte of the file (k/254)."""
        group = geo.findPointGroup(hou.text.encodeAttrib(name))
        assert group is not None, name
        attrib = geo.findPointAttrib("a3ob_w_" + group.name())
        values = geo.pointFloatAttribValues(attrib.name()) if attrib is not None else None
        return {p.number(): round((1.0 if values is None else values[p.number()]) * 254) for p in group.points()}

    def test_weights_already_clean_are_written_unchanged(self):
        """Export the cleaned sample, import it, export again: the two files have the same bone weight bytes."""
        bones = rig_bones.bone_name_set()
        first, _ = self.export(self.build(True), "first.p3d")
        again = self.node("a3ob_p3d_import", file=first)
        second, _ = self.export(again, "second.p3d")
        one = p3d.read_p3d_file(first, strict=False)
        two = p3d.read_p3d_file(second, strict=False)
        for a, b in zip(one.lods, two.lods):
            self.assertEqual(bone_bytes(a, bones), bone_bytes(b, bones), a.name)

    def test_raw_sample_trips_the_rules(self):
        """Design test 1.8 on real data: the raw sample has points over the sum limit and with a sum other than 1."""
        _, report = self.export(self.build(False), "raw.p3d")
        warnings = report.texts(messages.WARNING)
        for rule in ("T2-WGTSUM",):
            self.assertTrue([t for t in warnings if "rule " + rule in t], rule)
        self.assertTrue(all("Weight Cleanup" in t for t in warnings if "rule T2-WGT" in t))


if __name__ == "__main__":
    unittest.main()

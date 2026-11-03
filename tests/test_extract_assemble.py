"""Extract LOD and Assemble LODs (M3 step 2): the match rule, the table, and the nodes cooking in hython.

The pure tests run under plain Python. The node tests need ``hou`` (hython). They install the two assets from
houdini/otls into the running session only (``hou.hda.installFile``), so nothing is written to the user's Houdini
preferences.

Purpose of the node tests: catch a pass-through that is not exact (the no-edit case), an edit that touches the wrong
LOD or moves it, and the polygon-less LOD (Memory) case, which only the detail-attribute rule can handle.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from a3ob_houdini import hda_source, lod_match, nodes
from a3ob_houdini.errors import UserError
from a3ob_houdini.nodes import assemble_lods, extract_lod

from tests.hou_support import (FIXTURE_NAMES, FIXTURES, HAS_HOU, REPO_ROOT, hou, install_hdas_for_class,
                               uninstall_hdas)

OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")


class TestMatchRule(unittest.TestCase):
    """Spec section 3.2, "Assemble LODs match rule"."""

    def test_match_keeps_the_base_position_and_no_match_appends_in_input_order(self):
        result = lod_match.match_lods([10, 20, 30], [30, 99, 10, 98], True)
        self.assertEqual(result.replaced, {2: 0, 0: 2})
        self.assertEqual(result.appended, [1, 3])

    def test_a_missing_signature_is_appended_and_a_base_without_one_is_never_replaced(self):
        result = lod_match.match_lods([None, 5], [None, 5], True)
        self.assertEqual(result.replaced, {1: 1})
        self.assertEqual(result.appended, [0])

    def test_replace_off_appends_everything(self):
        result = lod_match.match_lods([1, 2], [1, 2], False)
        self.assertEqual(result.replaced, {})
        self.assertEqual(result.appended, [0, 1])

    def test_duplicate_in_the_base_replaces_the_first_and_warns(self):
        result = lod_match.match_lods([7, 8, 7], [7], True)
        self.assertEqual(result.replaced, {0: 0})
        self.assertEqual(result.appended, [])
        self.assertEqual(result.ambiguous_base, [7])

    def test_duplicates_among_edited_take_the_next_base_duplicate_then_append(self):
        result = lod_match.match_lods([7, 8, 7], [7, 7, 7], True)
        self.assertEqual(result.replaced, {0: 0, 2: 1})
        self.assertEqual(result.appended, [2])
        self.assertEqual(result.repeated_edited, [7])

    def test_no_base_appends_everything(self):
        result = lod_match.match_lods([], [3, 3], True)
        self.assertEqual(result.appended, [0, 1])
        self.assertEqual(result.repeated_edited, [3])

    def test_negative_signatures_compare_as_stored_bits(self):
        result = lod_match.match_lods([-5, 5], [5], True)
        self.assertEqual(result.replaced, {1: 0})


class TestTables(unittest.TestCase):
    def test_kinds_are_found(self):
        self.assertIn("extract_lod", nodes.KINDS)
        self.assertIn("assemble_lods", nodes.KINDS)

    def test_assemble_takes_many_inputs_and_has_a_label_for_each(self):
        spec = assemble_lods.SPEC
        self.assertEqual(spec.inputs, (0, assemble_lods.MAX_INPUTS))
        self.assertGreater(assemble_lods.MAX_INPUTS, 4)
        self.assertEqual(len(spec.input_labels), assemble_lods.MAX_INPUTS)
        text = hda_source.render(spec)["INDEX__SECTION"]
        self.assertIn("Inputs:       0 to %d" % assemble_lods.MAX_INPUTS, text)

    def test_resolve_lod_by_number_by_name_and_by_menu_text(self):
        names = ["Resolution 1", "Resolution 2", "Geometry"]
        self.assertEqual(extract_lod.resolve_lod("1", names), 1)
        self.assertEqual(extract_lod.resolve_lod(" geometry ", names), 2)
        self.assertEqual(extract_lod.resolve_lod("2: Geometry", names), 2)

    def test_resolve_lod_errors(self):
        names = ["Resolution 1", "Resolution 2", "Resolution 2"]
        with self.assertRaises(UserError) as caught:
            extract_lod.resolve_lod("8", names)
        self.assertIn("The input has 3 LODs, LOD 8 does not exist", str(caught.exception))
        for text, expected in (("", "no LOD is chosen"), ("Memory", "no LOD named"),
                               ("Resolution 2", "2 LODs are named")):
            with self.assertRaises(UserError) as caught:
                extract_lod.resolve_lod(text, names)
            self.assertIn(expected, str(caught.exception))


def fingerprint(content):
    """A comparable summary of one LOD geometry: details, counts, positions, topology, attribute names, values."""
    details = {a.name(): content.attribValue(a.name()) for a in content.globalAttribs()}
    points = [tuple(round(x, 5) for x in point.position()) for point in content.points()]
    prims = []
    for prim in content.prims():
        if isinstance(prim, hou.Polygon):
            prims.append(("poly", tuple(v.point().number() for v in prim.vertices())))
        else:
            prims.append((type(prim).__name__, tuple(round(x, 5) for x in prim.fullTransform().asTuple())))
    names = {
        "point": sorted(a.name() for a in content.pointAttribs()),
        "vertex": sorted(a.name() for a in content.vertexAttribs()),
        "prim": sorted(a.name() for a in content.primAttribs()),
        "pointgroups": sorted(g.name() for g in content.pointGroups()),
        "primgroups": sorted(g.name() for g in content.primGroups()),
    }
    values = {a.name(): tuple(round(x, 5) for x in content.vertexFloatAttribValues(a.name()))
              for a in content.vertexAttribs() if a.dataType() == hou.attribData.Float and not a.isArrayType()}
    return details, points, prims, names, values


def p3d_fingerprint(geo):
    """The P3D geometry: per prim, its outer name and the fingerprint of its content."""
    result = []
    for prim in geo.prims():
        outer = {a.name(): prim.attribValue(a.name()) for a in geo.primAttribs()}
        result.append((outer, fingerprint(prim.getEmbeddedGeometry())))
    return result, {a.name(): geo.attribValue(a.name()) for a in geo.globalAttribs()}


# Python SOP body for edits. A Python SOP starts with a copy of its first input, so ``geo`` is already the input.
EDIT_PREFIX = "geo = hou.pwd().geometry()\n"


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestNodesInHython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = [os.path.join(OTLS_DIR, hda_source.hda_folder_name(nodes.load(kind).SPEC))
                 for kind in ("extract_lod", "assemble_lods", "p3d_import")]
        cls.installed = install_hdas_for_class(paths)  # only the files not installed already (#22)
        cls.container = hou.node("/obj").createNode("geo", "extract_assemble_tests")

    @classmethod
    def tearDownClass(cls):
        cls.container.destroy()
        uninstall_hdas(cls.installed)

    # helpers -------------------------------------------------------------------------------------------------
    def cook(self, node):
        """Cook and return the error texts."""
        try:
            node.cook(force=True)
        except hou.OperationFailed:
            pass
        return list(node.errors())

    def importer(self, name):
        node = self.container.createNode("a3ob_p3d_import")
        node.parm("file").set(os.path.join(FIXTURES, name))
        self.assertEqual(self.cook(node), [])
        return node

    def extract(self, source, lod):
        node = self.container.createNode("a3ob_extract_lod")
        node.setInput(0, source)
        node.parm("lod").set(str(lod))
        return node

    def edit(self, source, body):
        node = self.container.createNode("python")
        node.parm("python").set(EDIT_PREFIX + body)
        node.setInput(0, source)
        self.assertEqual(self.cook(node), [])
        return node

    def assemble(self, base, *lods, replace=True):
        node = self.container.createNode("a3ob_assemble_lods")
        if base is not None:
            node.setInput(0, base)
        for number, lod in enumerate(lods, start=1):
            node.setInput(number, lod)
        node.parm("replace").set(1 if replace else 0)
        return node

    def names(self, node):
        return [prim.attribValue("name") for prim in node.geometry().prims()]

    # the assets ----------------------------------------------------------------------------------------------
    def test_assets_load_with_the_table_parameters_and_inputs(self):
        category = hou.sopNodeTypeCategory()
        extract = hou.nodeType(category, "a3ob_extract_lod")
        assemble = hou.nodeType(category, "a3ob_assemble_lods")
        self.assertEqual(extract.description(), "Extract LOD")
        self.assertEqual([t.name() for t in extract.parmTemplates()], ["lod"])
        self.assertEqual((extract.minNumInputs(), extract.maxNumInputs()), (1, 1))
        self.assertEqual(assemble.description(), "Assemble LODs")
        self.assertEqual([t.name() for t in assemble.parmTemplates()], ["replace"])
        self.assertEqual((assemble.minNumInputs(), assemble.maxNumInputs()), (0, assemble_lods.MAX_INPUTS))
        self.assertEqual(assemble.definition().sections()["PythonCook"].contents().strip(),
                         "import a3ob_houdini.entry as e; e.cook('assemble_lods', hou.pwd())")

    def test_more_than_four_inputs_work(self):
        imported = self.importer("sample_2_crate.p3d")
        extract = self.extract(imported, 0)
        assemble = self.assemble(None)
        for number in (5, 20, assemble_lods.MAX_INPUTS):
            assemble.setInput(number - 1, extract)
        self.assertEqual(self.cook(assemble), [])
        self.assertEqual(len(assemble.geometry().prims()), 3)

    # criterion 3: pass-through -------------------------------------------------------------------------------
    def test_import_then_assemble_with_no_edits_is_the_same_packed_geometry(self):
        for name in FIXTURE_NAMES:
            imported = self.importer(name)
            assemble = self.assemble(imported)
            self.assertEqual(self.cook(assemble), [], name)
            self.assertEqual(p3d_fingerprint(assemble.geometry()), p3d_fingerprint(imported.geometry()), name)
            count = len(imported.geometry().prims())
            self.assertIn("0 replaced, %d passed through, 0 added." % count, assemble.messages()[0])

    def test_file_level_detail_survives_the_base_input(self):
        imported = self.importer("sample_2_crate.p3d")
        tagged = self.container.createNode("python")
        tagged.parm("python").set(
            "geo = hou.pwd().geometry()\n"
            "geo.addAttrib(hou.attribType.Global, 'a3ob_file_trailing', ''); "
            "geo.setGlobalAttribValue('a3ob_file_trailing', 'AQID')\n")
        tagged.setInput(0, imported)
        assemble = self.assemble(tagged)
        self.assertEqual(self.cook(assemble), [])
        self.assertEqual(assemble.geometry().attribValue("a3ob_file_trailing"), "AQID")

    # criterion 4: edits --------------------------------------------------------------------------------------
    def test_extract_gives_a_lod_geometry_with_its_detail_attributes(self):
        imported = self.importer("sample_1_character.p3d")
        extract = self.extract(imported, 4)
        self.assertEqual(self.cook(extract), [])
        geo = extract.geometry()
        self.assertEqual(geo.attribValue("a3ob_lod_type"), 6)  # Geometry
        self.assertIsNotNone(geo.findGlobalAttrib("a3ob_lod_signature"))
        self.assertEqual(len(geo.points()), 34)
        self.assertEqual(sum(1 for p in geo.prims() if isinstance(p, hou.Polygon)), 64)
        self.assertEqual(extract.messages()[0], "Extracted LOD 4: Geometry, 34 points, 64 polygons.")
        by_name = self.extract(imported, "geometry")
        self.assertEqual(self.cook(by_name), [])
        self.assertEqual(len(by_name.geometry().points()), 34)

    def test_extract_errors_use_the_ux_text(self):
        imported = self.importer("sample_1_character.p3d")
        self.assertIn("The input has 9 LODs, LOD 12 does not exist", self.cook(self.extract(imported, 12))[0])
        lod = self.extract(imported, 4)
        again = self.extract(lod, 0)
        self.assertIn("Input is not a P3D geometry (packed LODs). Connect P3D Import or Assemble LODs",
                      self.cook(again)[0])
        self.assertIn("no LOD named", self.cook(self.extract(imported, "Nothing"))[0])

    def test_a_transformed_lod_is_baked_with_a_warning(self):
        imported = self.importer("sample_2_crate.p3d")
        moved = self.container.createNode("python")
        moved.parm("python").set(
            "geo = hou.pwd().geometry()\n"
            "geo.prims()[0].setTransform(hou.hmath.buildTranslate((0, 5, 0)))\n")
        moved.setInput(0, imported)
        self.assertEqual(self.cook(moved), [])
        plain = self.extract(imported, 0)
        shifted = self.extract(moved, 0)
        self.assertEqual(self.cook(plain), [])
        self.assertEqual(self.cook(shifted), [])
        self.assertTrue(any("Transform baked in" in text for text in shifted.warnings()), shifted.warnings())
        self.assertEqual(plain.warnings(), ())
        before = plain.geometry().points()[0].position()
        after = shifted.geometry().points()[0].position()
        self.assertAlmostEqual(after[1] - before[1], 5.0, places=5)

    def test_an_edited_geometry_lod_replaces_only_itself_and_keeps_its_position(self):
        imported = self.importer("sample_1_character.p3d")
        base = p3d_fingerprint(imported.geometry())
        extract = self.extract(imported, 4)
        edited = self.edit(extract, "for point in geo.points():\n"
                                    "    point.setPosition(point.position() + hou.Vector3(0, 1, 0))\n"
                                    "geo.addAttrib(hou.attribType.Global, 'edited_marker', 1)\n")
        assemble = self.assemble(imported, edited)
        self.assertEqual(self.cook(assemble), [])
        self.assertTrue(assemble.messages()[0].endswith("1 replaced, 8 passed through, 0 added."))
        result = p3d_fingerprint(assemble.geometry())
        self.assertEqual(len(result[0]), len(base[0]))
        for position, (before, after) in enumerate(zip(base[0], result[0])):
            if position == 4:
                self.assertEqual(after[0], before[0])  # same outer name, same place
                self.assertIn("edited_marker", after[1][0])
                moved = [(x, y - 1, z) for x, y, z in after[1][1]]
                self.assertEqual([tuple(round(c, 4) for c in p) for p in moved],
                                 [tuple(round(c, 4) for c in p) for p in before[1][1]])
            else:
                self.assertEqual(after, before, "LOD %d changed" % position)

    def test_a_lod_with_a_new_signature_is_appended(self):
        imported = self.importer("sample_1_character.p3d")
        extract = self.extract(imported, 4)
        new = self.edit(extract, "geo.setGlobalAttribValue('a3ob_lod_signature', 12345)\n"
                                 "geo.setGlobalAttribValue('a3ob_lod_resolution', 7.0)\n"
                                 "geo.setGlobalAttribValue('a3ob_lod_type', 0)\n")
        bare = self.edit(extract, "geo.findGlobalAttrib('a3ob_lod_signature').destroy()\n")
        assemble = self.assemble(imported, new, bare)
        self.assertEqual(self.cook(assemble), [])
        self.assertTrue(assemble.messages()[0].endswith("0 replaced, 9 passed through, 2 added."))
        self.assertEqual(self.names(assemble)[:9], self.names(imported))
        self.assertEqual(self.names(assemble)[9:], ["Resolution 7", "Geometry"])
        original = p3d_fingerprint(imported.geometry())[0]
        self.assertEqual(p3d_fingerprint(assemble.geometry())[0][:9], original)

    def test_a_polygon_less_lod_round_trips_through_extract_and_assemble(self):
        imported = self.importer("sample_1_character.p3d")
        names = self.names(imported)
        memory = names.index("Memory")
        extract = self.extract(imported, "Memory")
        self.assertEqual(self.cook(extract), [])
        self.assertEqual(sum(1 for p in extract.geometry().prims() if isinstance(p, hou.Polygon)), 0)
        assemble = self.assemble(imported, extract)
        self.assertEqual(self.cook(assemble), [])
        self.assertTrue(assemble.messages()[0].endswith("1 replaced, 8 passed through, 0 added."))
        self.assertEqual(self.names(assemble), names)
        self.assertEqual(p3d_fingerprint(assemble.geometry()), p3d_fingerprint(imported.geometry()))
        # An edit on it lands on the Memory slot and nowhere else.
        moved = self.edit(extract, "for point in geo.points():\n"
                                   "    point.setPosition(point.position() + hou.Vector3(0, 2, 0))\n")
        edited = self.assemble(imported, moved)
        self.assertEqual(self.cook(edited), [])
        before = p3d_fingerprint(imported.geometry())[0]
        after = p3d_fingerprint(edited.geometry())[0]
        for position in range(len(before)):
            if position == memory:
                self.assertNotEqual(after[position], before[position])
            else:
                self.assertEqual(after[position], before[position])

    def test_replace_off_appends_and_a_base_is_optional(self):
        imported = self.importer("sample_2_crate.p3d")
        extract = self.extract(imported, 0)
        off = self.assemble(imported, extract, replace=False)
        self.assertEqual(self.cook(off), [])
        count = len(imported.geometry().prims())
        self.assertEqual(len(off.geometry().prims()), count + 1)
        self.assertTrue(off.messages()[0].endswith("0 replaced, %d passed through, 1 added." % count))
        fresh = self.assemble(None, extract)
        self.assertEqual(self.cook(fresh), [])
        self.assertEqual(self.names(fresh), [self.names(imported)[0]])
        self.assertTrue(fresh.messages()[0].endswith("0 replaced, 0 passed through, 1 added."))

    def test_duplicate_signatures_follow_the_spec_and_warn(self):
        imported = self.importer("sample_2_crate.p3d")
        extract = self.extract(imported, 0)
        marked = self.edit(extract, "geo.addAttrib(hou.attribType.Global, 'edited_marker', 1)\n")
        # Two edited LODs with one signature and no base: both are appended, with a warning.
        twice = self.assemble(None, extract, extract)
        self.assertEqual(self.cook(twice), [])
        self.assertEqual(len(twice.geometry().prims()), 2)
        self.assertTrue(any("2 LOD inputs have the same signature" in text for text in twice.warnings()))
        # A base with the signature twice: the edited LOD replaces the first and the node warns.
        replaced = self.assemble(twice, marked)
        self.assertEqual(self.cook(replaced), [])
        self.assertTrue(replaced.messages()[0].endswith("1 replaced, 1 passed through, 0 added."))
        self.assertTrue(any("the base has 2 LODs with the same signature" in text for text in replaced.warnings()))
        contents = [p.getEmbeddedGeometry() for p in replaced.geometry().prims()]
        self.assertIsNotNone(contents[0].findGlobalAttrib("edited_marker"))
        self.assertIsNone(contents[1].findGlobalAttrib("edited_marker"))
        # Two edited LODs and a base with two: the second one takes the next base duplicate.
        both = self.assemble(twice, marked, marked)
        self.assertEqual(self.cook(both), [])
        self.assertTrue(both.messages()[0].endswith("2 replaced, 0 passed through, 0 added."))
        # Three edited LODs and a base with two: the third is appended.
        three = self.assemble(twice, marked, marked, marked)
        self.assertEqual(self.cook(three), [])
        self.assertTrue(three.messages()[0].endswith("2 replaced, 0 passed through, 1 added."))
        self.assertEqual(len(three.geometry().prims()), 3)

    def test_assemble_input_errors(self):
        imported = self.importer("sample_2_crate.p3d")
        extract = self.extract(imported, 0)
        stripped = self.edit(extract, "geo.findGlobalAttrib('a3ob_lod_type').destroy()\n")
        no_type = self.assemble(imported, extract, stripped)
        self.assertIn("Input 3 has no LOD type. Add a LOD Properties node", self.cook(no_type)[0])
        packed_in_slot = self.assemble(imported, imported)
        self.assertIn("Input 2 is already a P3D geometry. Use it as the base input", self.cook(packed_in_slot)[0])

    def test_the_info_line_names_the_base(self):
        imported = self.importer("sample_1_character.p3d")
        extract = self.extract(imported, 4)
        assemble = self.assemble(imported, extract)
        self.assertEqual(self.cook(assemble), [])
        self.assertEqual(assemble.messages()[0],
                         "Base: %s (9 LODs). 1 replaced, 8 passed through, 0 added." % imported.name())
        self.assertEqual(assemble.warnings(), ())
        fresh = self.assemble(None, extract)
        self.assertEqual(self.cook(fresh), [])
        self.assertEqual(fresh.messages()[0], "No base. 0 replaced, 0 passed through, 1 added.")

    def test_a_single_lod_in_the_base_slot_warns_about_the_input_order(self):
        imported = self.importer("sample_2_crate.p3d")
        extract = self.extract(imported, 0)
        swapped = self.assemble(extract)
        self.assertEqual(self.cook(swapped), [])
        self.assertTrue(any("Input 1 is a single LOD geometry" in text and "input order" in text
                            for text in swapped.warnings()), swapped.warnings())
        self.assertEqual(swapped.messages()[0], "No base. 0 replaced, 0 passed through, 1 added.")
        self.assertEqual(len(swapped.geometry().prims()), 1)
        # The right order gives no warning.
        right = self.assemble(imported, extract)
        self.assertEqual(self.cook(right), [])
        self.assertEqual(right.warnings(), ())


if __name__ == "__main__":
    unittest.main()

"""LOD Properties and Material Assign: every attribute they write, on LODs taken out of imported fixtures.

The nodes are cooked through their ``cook(node, report)`` functions with a small stand-in for the Houdini node (it
supplies the parameter values and the input geometry). The real assets are checked in test_hda_lod_nodes.py. The pure
rules (type menu, signature maths, property merge, path text) also run under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_core.p3d import lod_types
from a3ob_houdini import lod_props, material_text, names, nodes
from a3ob_houdini.errors import UserError
from a3ob_houdini.messages import Report
from a3ob_houdini import params
from a3ob_houdini.params import read_parms
from tests.hou_support import HAS_HOU, imported_fixture

if HAS_HOU:
    import hou
    from a3ob_houdini import geo_edit


class TestPureRules(unittest.TestCase):
    def test_type_menu_has_every_type_by_display_name(self):
        self.assertEqual(len(lod_props.TYPE_ITEMS), len(lod_types.LOD_TYPES))
        self.assertEqual(lod_props.TYPE_ITEMS[0], ("visual", "Resolution"))
        self.assertEqual(lod_props.TYPE_ITEMS[-1], ("unknown", "Unknown"))
        for token, label in lod_props.TYPE_ITEMS:
            self.assertEqual(lod_props.type_token(lod_props.type_from_token(token)), token)
        with self.assertRaises(UserError):
            lod_props.type_from_token("nonsense")

    def test_signature_is_updated_only_when_the_view_changed(self):
        stored = names.float_bits(lod_types.encode_signature(lod_types.LodType.VISUAL, 2))
        self.assertEqual(lod_props.signature_after_edit(stored, lod_types.LodType.VISUAL, 2.0), (stored, False))
        bits, changed = lod_props.signature_after_edit(stored, lod_types.LodType.VISUAL, 3.0)
        self.assertTrue(changed)
        self.assertEqual(names.bits_float(bits), lod_types.encode_signature(lod_types.LodType.VISUAL, 3))
        bits, changed = lod_props.signature_after_edit(stored, lod_types.LodType.GEOMETRY, 7.0)
        self.assertEqual(names.bits_float(bits), lod_types.encode_signature(lod_types.LodType.GEOMETRY, 0))
        self.assertEqual(lod_props.signature_after_edit(None, lod_types.LodType.GEOMETRY, 0.0), (None, False))

    def test_a_signature_that_does_not_reencode_is_kept_while_the_view_matches(self):
        odd = lod_types.f32(1e13 * (1 + 3e-7))  # decodes as Geometry, encodes to something else (spec 3.4)
        stored = names.float_bits(odd)
        self.assertNotEqual(lod_types.encode_signature(lod_types.LodType.GEOMETRY), odd)
        self.assertEqual(lod_props.signature_after_edit(stored, lod_types.LodType.GEOMETRY, 0.0), (stored, False))

    def test_encode_errors_name_the_type_and_the_range(self):
        with self.assertRaises(UserError) as caught:
            lod_props.encode_view(lod_types.LodType.VISUAL, 5000.0)
        self.assertIn("Resolution cannot take", str(caught.exception))
        self.assertIn("0 up to (not including) 1000", str(caught.exception))

    def test_property_merge_modes(self):
        existing = [lod_props.Entry("Class", "man", "AAAA"), lod_props.Entry("buoyancy", "1", "BBBB")]
        added = lod_props.merge_properties(existing, [("class", "car"), ("new", "x")], replace_all=False)
        self.assertEqual([(e.name, e.value, e.raw) for e in added],
                         [("class", "car", ""), ("buoyancy", "1", "BBBB"), ("new", "x", "")])
        replaced = lod_props.merge_properties(existing, [("only", "1")], replace_all=True)
        self.assertEqual([(e.name, e.value, e.raw) for e in replaced], [("only", "1", "")])
        self.assertEqual(lod_props.duplicate_names([("a", "1"), ("A", "2"), ("b", "")]), ["A"])

    def test_a_row_equal_to_an_input_entry_keeps_its_raw_payload(self):
        existing = [lod_props.Entry("Class", "man", "AAAA"), lod_props.Entry("buoyancy", "1", "BBBB"),
                    lod_props.Entry("keep", "x", "CCCC")]
        rows = [("Class", "man"), ("buoyancy", "2"), ("keep", "x")]  # same, changed value, same
        merged = lod_props.merge_properties(existing, rows, replace_all=False)
        self.assertEqual([(e.name, e.value, e.raw) for e in merged],
                         [("Class", "man", "AAAA"), ("buoyancy", "2", ""), ("keep", "x", "CCCC")])
        renamed = lod_props.merge_properties(existing, [("CLASS", "man")], replace_all=False)
        self.assertEqual(renamed[0].raw, "")  # a changed key drops it

    def test_property_text_limits(self):
        lod_props.check_text("x" * 63, "a value")
        with self.assertRaises(UserError) as caught:
            lod_props.check_text("x" * 64, "the value of property row 2")
        self.assertIn("property row 2", str(caught.exception))
        with self.assertRaises(UserError):
            lod_props.check_text("ř", "a name")

    def test_path_and_colour_text(self):
        self.assertEqual(material_text.normalise_path("a3/data_f/tex.paa"), "a3\\data_f\\tex.paa")
        self.assertEqual(material_text.normalise_path("A3\\Keep.PAA"), "A3\\Keep.PAA")
        self.assertEqual(material_text.color_texture((1, 0.5, 0, 1), "CA"), "#(argb,8,8,3)color(1.000,0.500,0.000,1.000,CA)")
        self.assertTrue(material_text.has_known_extension("x.RVMAT"))
        self.assertFalse(material_text.has_known_extension("x.jpg"))

    def test_both_nodes_are_found_and_their_tables_are_valid(self):
        for kind in ("lod_properties", "material_assign"):
            self.assertIn(kind, nodes.KINDS)
            self.assertEqual(nodes.load(kind).SPEC.type_name, "a3ob_" + kind)


class StubParm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value


class StubNode:
    """What a cook function needs from a node: parameter values, the input geometry and the output geometry."""

    def __init__(self, kind, source, **values):
        self.spec = nodes.load(kind).SPEC
        defaults = {}
        for parm in params.flat_parms(self.spec.parms):
            if parm.kind == "button":
                continue
            defaults[parm.name] = [] if parm.kind == "multiparm" else parm.default
        defaults.update(values)
        self.values = defaults
        self.source = source
        self.out = hou.Geometry()

    def path(self):
        return "/obj/geo1/stub"

    def inputs(self):
        return [object()]

    def inputGeometry(self, index):
        return self.source

    def geometry(self):
        return self.out

    def parm(self, name):
        if name in self.values:
            value = self.values[name]
            return StubParm(len(value) if isinstance(value, list) else value)
        for parm in params.flat_parms(self.spec.parms):
            for child in parm.children:
                stem = child.name.replace("#", "")
                if name.startswith(stem) and name[len(stem):].isdigit():
                    return StubParm(self.values[parm.name][int(name[len(stem):]) - 1][stem])
        return None

    def parmTuple(self, name):
        return StubParm(self.values[name])


def cook(kind, source, **values):
    """Cook a node on ``source``. Returns ``(output geometry, report)``."""
    node = StubNode(kind, source, **values)
    report = Report()
    nodes.load(kind).cook(node, report)
    return node.out, report


def extract_lod(fixture, index):
    """Take LOD ``index`` out of an imported fixture the way Extract LOD does: Blast to one prim, then Unpack."""
    _, geo, _, _ = imported_fixture(fixture)
    category = hou.sopNodeTypeCategory()
    blast = category.nodeVerb("blast")
    blast.setParms({"group": str(index), "negate": 1, "grouptype": 4})
    one = hou.Geometry()
    blast.execute(one, [geo])
    lod = hou.Geometry()
    category.nodeVerb("unpack").execute(lod, [one])
    return lod


def detail(geo, name):
    return geo_edit.read_detail(geo, name)


def attribute_names(geo):
    return {
        "detail": sorted(a.name() for a in geo.globalAttribs()),
        "point": sorted(a.name() for a in geo.pointAttribs()),
        "vertex": sorted(a.name() for a in geo.vertexAttribs()),
        "prim": sorted(a.name() for a in geo.primAttribs()),
        "pointgroups": sorted(g.name() for g in geo.pointGroups()),
        "primgroups": sorted(g.name() for g in geo.primGroups()),
    }


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestLodProperties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crate = extract_lod("sample_2_crate.p3d", 0)       # Resolution 1, one property
        cls.geometry = extract_lod("sample_2_crate.p3d", 3)    # Geometry, two properties

    def test_nothing_else_changes_and_the_input_is_not_touched(self):
        before = attribute_names(self.crate)
        out, report = cook("lod_properties", self.crate, lodtype="visual", resolution=1.0)
        self.assertEqual(attribute_names(self.crate), before)
        self.assertEqual(attribute_names(out), before)
        self.assertEqual(len(out.points()), len(self.crate.points()))
        self.assertEqual(len(out.prims()), len(self.crate.prims()))
        self.assertEqual(detail(out, "a3ob_lod_flags"), detail(self.crate, "a3ob_lod_flags"))
        self.assertEqual(detail(out, "a3ob_source_counts"), detail(self.crate, "a3ob_source_counts"))
        self.assertEqual(detail(out, "a3ob_tagg_order"), detail(self.crate, "a3ob_tagg_order"))
        self.assertEqual(report.texts("error"), [])

    def test_unchanged_type_and_resolution_keep_the_stored_signature_bits(self):
        out, report = cook("lod_properties", self.crate, lodtype="visual", resolution=1.0)
        self.assertEqual(detail(out, "a3ob_lod_type"), 0)
        self.assertEqual(detail(out, "a3ob_lod_resolution"), 1.0)
        self.assertEqual(detail(out, "a3ob_spec_version"), 1)
        self.assertEqual(detail(out, "a3ob_lod_signature"), detail(self.crate, "a3ob_lod_signature"))
        self.assertEqual(report.texts("info"), ["Resolution 1, signature 1, 1 property, 0 copies."])

    def test_changing_the_resolution_re_encodes_the_signature_with_the_core(self):
        out, report = cook("lod_properties", self.crate, lodtype="visual", resolution=3.0)
        self.assertEqual(detail(out, "a3ob_lod_type"), 0)
        self.assertEqual(detail(out, "a3ob_lod_resolution"), 3.0)
        self.assertEqual(names.bits_float(detail(out, "a3ob_lod_signature")),
                         lod_types.encode_signature(lod_types.LodType.VISUAL, 3.0))
        self.assertEqual(detail(self.crate, "a3ob_lod_resolution"), 1.0)  # the input keeps its value
        self.assertTrue(any("signature" in text and "updated" in text for text in report.texts("info")))

    def test_changing_the_type_re_encodes_and_resets_the_resolution(self):
        out, report = cook("lod_properties", self.crate, lodtype="geometry", resolution=9.0)
        self.assertEqual(detail(out, "a3ob_lod_type"), int(lod_types.LodType.GEOMETRY))
        self.assertEqual(detail(out, "a3ob_lod_resolution"), 0.0)
        self.assertEqual(names.bits_float(detail(out, "a3ob_lod_signature")),
                         lod_types.encode_signature(lod_types.LodType.GEOMETRY))
        self.assertIn("Geometry, signature 1e+13", report.texts("info")[-1])

    def test_a_ranged_type_takes_its_resolution(self):
        out, _ = cook("lod_properties", self.crate, lodtype="shadow", resolution=0.0)
        self.assertEqual(detail(out, "a3ob_lod_type"), int(lod_types.LodType.SHADOW))
        self.assertEqual(names.bits_float(detail(out, "a3ob_lod_signature")), 10000.0)
        out, _ = cook("lod_properties", self.crate, lodtype="unknown", resolution=5000.5)
        self.assertEqual(detail(out, "a3ob_lod_type"), -1)
        self.assertEqual(detail(out, "a3ob_lod_resolution"), 5000.5)
        self.assertEqual(names.bits_float(detail(out, "a3ob_lod_signature")), 5000.5)
        with self.assertRaises(UserError) as caught:  # 123.5 is the signature of a Resolution LOD
            cook("lod_properties", self.crate, lodtype="unknown", resolution=123.5)
        self.assertIn("Unknown cannot take", str(caught.exception))

    def test_a_lod_without_a_stored_signature_stays_without_one(self):
        bare = hou.Geometry()
        bare.copy(self.crate)
        geo_edit.remove_detail(bare, "a3ob_lod_signature")
        out, _ = cook("lod_properties", bare, lodtype="geometry")
        self.assertIsNone(out.findGlobalAttrib("a3ob_lod_signature"))
        self.assertEqual(detail(out, "a3ob_lod_type"), 6)

    def test_a_geometry_without_lod_data_gets_the_spec_defaults_it_is_given(self):
        empty = hou.Geometry()
        empty.createPoint()
        out, report = cook("lod_properties", empty, lodtype="visual", resolution=2.0)
        self.assertEqual(detail(out, "a3ob_spec_version"), 1)
        self.assertEqual(detail(out, "a3ob_lod_type"), 0)
        self.assertEqual(detail(out, "a3ob_lod_resolution"), 2.0)
        for absent in ("a3ob_lod_signature", "a3ob_lod_flags", "a3ob_lod_copy_types", "a3ob_prop_names"):
            self.assertIsNone(out.findGlobalAttrib(absent), absent)
        self.assertEqual(report.texts("info"), ["Resolution 2, 0 properties, 0 copies."])

    def test_set_type_off_leaves_type_resolution_and_signature(self):
        out, _ = cook("lod_properties", self.geometry, settype=False, lodtype="visual", resolution=5.0,
                      props=[{"prop_name": "extra", "prop_value": "1"}])
        for name in ("a3ob_lod_type", "a3ob_lod_resolution", "a3ob_lod_signature"):
            self.assertEqual(detail(out, name), detail(self.geometry, name), name)
        self.assertEqual(detail(out, "a3ob_prop_names"), ("autocenter", "buoyancy", "extra"))

    def test_add_mode_adds_and_replaces_by_name_in_any_case(self):
        rows = [{"prop_name": "BUOYANCY", "prop_value": "0"}, {"prop_name": "class", "prop_value": "box"}]
        out, report = cook("lod_properties", self.geometry, settype=False, props=rows)
        self.assertEqual(detail(out, "a3ob_prop_names"), ("autocenter", "BUOYANCY", "class"))
        self.assertEqual(detail(out, "a3ob_prop_values"), ("0", "0", "box"))
        self.assertIsNone(out.findGlobalAttrib("a3ob_prop_raw"))
        self.assertEqual(detail(self.geometry, "a3ob_prop_names"), ("autocenter", "buoyancy"))
        self.assertIn("3 properties", report.texts("info")[0])

    def test_replace_mode_keeps_only_the_rows_and_no_rows_removes_the_properties(self):
        out, _ = cook("lod_properties", self.geometry, settype=False, propmode="replace",
                      props=[{"prop_name": "only", "prop_value": "yes"}])
        self.assertEqual(detail(out, "a3ob_prop_names"), ("only",))
        self.assertEqual(detail(out, "a3ob_prop_values"), ("yes",))
        out, _ = cook("lod_properties", self.geometry, settype=False, propmode="replace")
        for name in ("a3ob_prop_names", "a3ob_prop_values", "a3ob_prop_raw"):
            self.assertIsNone(out.findGlobalAttrib(name), name)

    def test_raw_payloads_stay_parallel_and_untouched_entries_keep_theirs(self):
        tailed = hou.Geometry()
        tailed.copy(self.geometry)
        geo_edit.set_detail_array(tailed, "a3ob_prop_raw", "string", ["AAAA", ""])
        out, _ = cook("lod_properties", tailed, settype=False, props=[{"prop_name": "more", "prop_value": "1"}])
        self.assertEqual(detail(out, "a3ob_prop_raw"), ("AAAA", "", ""))
        out, _ = cook("lod_properties", tailed, settype=False, props=[{"prop_name": "AUTOCENTER", "prop_value": "1"}])
        self.assertIsNone(out.findGlobalAttrib("a3ob_prop_raw"))  # the only tail belonged to the replaced entry

    def test_read_from_input_then_an_unedited_cook_keeps_every_raw_payload(self):
        tailed = hou.Geometry()
        tailed.copy(self.geometry)
        geo_edit.set_detail_array(tailed, "a3ob_prop_raw", "string", ["AAAA", "BBBB"])
        rows = [{"prop_name": name, "prop_value": value} for name, value in zip(
            detail(tailed, "a3ob_prop_names"), detail(tailed, "a3ob_prop_values"))]  # what Read from Input fills in
        out, _ = cook("lod_properties", tailed, settype=False, props=rows)
        self.assertEqual(detail(out, "a3ob_prop_raw"), ("AAAA", "BBBB"))
        self.assertEqual(detail(out, "a3ob_prop_names"), detail(tailed, "a3ob_prop_names"))

    def test_copies_are_written_as_two_parallel_arrays(self):
        rows = [{"copy_type": "visual", "copy_res": 2.0}, {"copy_type": "geometry", "copy_res": 7.0},
                {"copy_type": "shadow", "copy_res": 1.0}]
        out, report = cook("lod_properties", self.crate, copies=rows)
        self.assertEqual(detail(out, "a3ob_lod_copy_types"), (0, 6, 4))
        self.assertEqual(detail(out, "a3ob_lod_copy_resolutions"), (2.0, 0.0, 1.0))  # Geometry has no resolution
        self.assertIn("3 copies", report.texts("info")[0])
        again, _ = cook("lod_properties", out, copies=[{"copy_type": "visual", "copy_res": 5.0}])
        self.assertEqual(detail(again, "a3ob_lod_copy_types"), (0, 6, 4, 0))
        self.assertEqual(detail(again, "a3ob_lod_copy_resolutions"), (2.0, 0.0, 1.0, 5.0))
        replaced, _ = cook("lod_properties", out, propmode="replace")
        self.assertIsNone(replaced.findGlobalAttrib("a3ob_lod_copy_types"))
        self.assertIsNone(replaced.findGlobalAttrib("a3ob_lod_copy_resolutions"))

    def test_problems_are_errors_in_the_ux_form_or_warnings(self):
        with self.assertRaises(UserError) as caught:
            cook("lod_properties", self.crate, lodtype="visual", resolution=5000.0)
        self.assertTrue(str(caught.exception).startswith("LOD Properties: Resolution cannot take"))
        with self.assertRaises(UserError) as caught:
            cook("lod_properties", self.crate, props=[{"prop_name": "k", "prop_value": "v" * 64}])
        self.assertIn("the value of property row 1 is longer than 63", str(caught.exception))
        with self.assertRaises(UserError):
            cook("lod_properties", self.crate, props=[{"prop_name": "ř", "prop_value": "v"}])
        with self.assertRaises(UserError) as caught:
            cook("lod_properties", self.crate, copies=[{"copy_type": "visual", "copy_res": -1.0}])
        self.assertIn("Copy 1:", str(caught.exception))
        _, report = cook("lod_properties", self.crate, props=[{"prop_name": "a", "prop_value": "1"},
                                                              {"prop_name": "A", "prop_value": "2"}])
        self.assertIn("appears in more than one row", report.texts("warning")[0])
        _, report = cook("lod_properties", self.crate, copies=[{"copy_type": "visual", "copy_res": 1.0}])
        self.assertIn("same signature as the LOD itself", report.texts("warning")[0])

    def test_unconnected_input_is_an_error(self):
        node = StubNode("lod_properties", None)
        node.inputs = lambda: []
        with self.assertRaises(UserError) as caught:
            nodes.load("lod_properties").cook(node, Report())
        self.assertIn("no geometry is connected", str(caught.exception))

    def test_the_parameter_reader_returns_rows_and_tokens(self):
        node = StubNode("lod_properties", self.crate, props=[{"prop_name": "a", "prop_value": "1"}])
        values = read_parms(node, node.spec)
        self.assertEqual(values["props"], [{"prop_name": "a", "prop_value": "1"}])
        self.assertEqual(values["lodtype"], "visual")


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestMaterialAssign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crate = extract_lod("sample_2_crate.p3d", 0)
        cls.character = extract_lod("sample_1_character.p3d", 0)

    def strings(self, geo, name):
        return list(geo.primStringAttribValues(name))

    def test_all_polygons_get_texture_and_material_with_backslashes(self):
        out, report = cook("material_assign", self.crate, texture="a3/data_f/crate_co.paa",
                           material="a3/data_f/crate.rvmat")
        self.assertEqual(set(self.strings(out, "a3ob_texture")), {"a3\\data_f\\crate_co.paa"})
        self.assertEqual(set(self.strings(out, "a3ob_material")), {"a3\\data_f\\crate.rvmat"})
        self.assertEqual(report.texts("info"), ["Set on 168 polygons."])
        self.assertEqual(report.texts("warning"), [])
        self.assertEqual(set(self.strings(self.crate, "a3ob_texture")),
                         {"#(argb,8,8,3)color(0.040,0.435,0.773,1.000,co)"})  # the input is untouched

    def test_only_the_two_strings_are_written(self):
        before = attribute_names(self.crate)
        out, _ = cook("material_assign", self.crate, texture="x.paa", material="x.rvmat")
        self.assertEqual(attribute_names(out), before)
        for name in ("shop_materialpath", "material"):
            self.assertIsNone(out.findPrimAttrib(name), name)
        self.assertEqual(detail(out, "a3ob_lod_type"), detail(self.crate, "a3ob_lod_type"))
        self.assertEqual(self.strings(out, "a3ob_texture").__len__(), len(out.prims()))

    def test_a_point_group_sets_the_faces_whose_points_are_all_in_it(self):
        out, report = cook("material_assign", self.crate, group="lid", grouptype="points", texture="lid.paa",
                           setmat=False)
        members = {point.number() for point in self.crate.findPointGroup("lid").points()}
        expected = {prim.number() for prim in self.crate.prims()
                    if all(point.number() in members for point in prim.points())}
        self.assertTrue(expected)
        done = {number for number, text in enumerate(self.strings(out, "a3ob_texture")) if text == "lid.paa"}
        self.assertEqual(done, expected)
        self.assertEqual(report.texts("info"), ["Set on %d polygons." % len(expected)])

    def test_a_primitive_group_and_a_number_range(self):
        group = self.character.primGroups()[0]
        wanted = {prim.number() for prim in group.prims() if isinstance(prim, hou.Polygon)}
        out, _ = cook("material_assign", self.character, group=group.name(), texture="g.paa", setmat=False)
        done = {number for number, text in enumerate(self.strings(out, "a3ob_texture")) if text == "g.paa"}
        self.assertEqual(done, wanted)
        out, report = cook("material_assign", self.crate, group="0-4", texture="r.paa", setmat=False)
        self.assertEqual([i for i, text in enumerate(self.strings(out, "a3ob_texture")) if text == "r.paa"],
                         [0, 1, 2, 3, 4])

    def test_packed_proxies_are_never_set(self):
        packed = [prim for prim in self.character.prims() if isinstance(prim, hou.PackedPrim)]
        self.assertTrue(packed)
        out, report = cook("material_assign", self.character, texture="all.paa", material="all.rvmat")
        textures = self.strings(out, "a3ob_texture")
        for prim in packed:
            self.assertNotEqual(textures[prim.number()], "all.paa")
        self.assertEqual(report.texts("info"), ["Set on %d polygons." % (len(out.prims()) - len(packed))])

    def test_set_off_leaves_that_attribute_and_does_not_create_it(self):
        out, _ = cook("material_assign", self.crate, settex=False, material="m.rvmat")
        self.assertEqual(self.strings(out, "a3ob_texture"), self.strings(self.crate, "a3ob_texture"))
        self.assertEqual(set(self.strings(out, "a3ob_material")), {"m.rvmat"})
        bare = hou.Geometry()
        bare.copy(self.crate)
        bare.findPrimAttrib("a3ob_material").destroy()
        out, _ = cook("material_assign", bare, setmat=False, texture="t.paa")
        self.assertIsNone(out.findPrimAttrib("a3ob_material"))

    def test_an_empty_material_clears_it_and_a_missing_attribute_is_made_with_empty_strings(self):
        out, _ = cook("material_assign", self.crate, settex=False, material="")
        self.assertEqual(set(self.strings(out, "a3ob_material")), {""})
        bare = hou.Geometry()
        bare.copy(self.crate)
        bare.findPrimAttrib("a3ob_material").destroy()
        out, _ = cook("material_assign", bare, group="0", settex=False, material="m.rvmat")
        values = self.strings(out, "a3ob_material")
        self.assertEqual(values[0], "m.rvmat")
        self.assertEqual(set(values[1:]), {""})

    def test_color_and_custom_sources(self):
        out, _ = cook("material_assign", self.crate, texsource="color", texcolor=(1.0, 0.5, 0.0, 1.0), colortype="CA",
                      setmat=False)
        self.assertEqual(set(self.strings(out, "a3ob_texture")), {"#(argb,8,8,3)color(1.000,0.500,0.000,1.000,CA)"})
        raw = "#(rgb,8,8,3)color(0.5,0.5,0.5,1,CO)/with/slashes"
        out, report = cook("material_assign", self.crate, texsource="custom", raw=raw, setmat=False)
        self.assertEqual(set(self.strings(out, "a3ob_texture")), {raw})  # copied as typed, never judged
        self.assertEqual(report.texts("warning"), [])
        with self.assertRaises(UserError):
            cook("material_assign", self.crate, texsource="color", texcolor=(2.0, 0.0, 0.0, 1.0))

    def test_the_old_colour_parameter_still_counts_when_the_picker_is_left_white(self):
        want = {"#(argb,8,8,3)color(0.250,0.500,0.750,1.000,CO)"}
        out, _ = cook("material_assign", self.crate, texsource="color", color=(0.25, 0.5, 0.75, 1.0), setmat=False)
        self.assertEqual(set(self.strings(out, "a3ob_texture")), want)
        out, _ = cook("material_assign", self.crate, texsource="color", color=(0.9, 0.9, 0.9, 1.0),
                      texcolor=(0.25, 0.5, 0.75, 1.0), setmat=False)  # the picker wins once it is changed
        self.assertEqual(set(self.strings(out, "a3ob_texture")), want)

    def test_messages(self):
        _, report = cook("material_assign", self.crate, group="9999", texture="x.paa")
        self.assertIn("no polygons match the group '9999'", report.texts("warning")[0])
        with self.assertRaises(UserError) as caught:
            cook("material_assign", self.crate, group="no such group!", texture="x.paa")
        self.assertIn("is not a valid group", str(caught.exception))
        with self.assertRaises(UserError) as caught:
            cook("material_assign", self.crate, texture="ř.paa")
        self.assertIn("The texture has a character", str(caught.exception))
        with self.assertRaises(UserError) as caught:
            cook("material_assign", self.crate, material="ř.rvmat")
        self.assertIn("The material has a character", str(caught.exception))
        _, report = cook("material_assign", self.crate, texture="x.jpg", material="m.txt")
        self.assertEqual(len(report.texts("warning")), 2)


if __name__ == "__main__":
    unittest.main()

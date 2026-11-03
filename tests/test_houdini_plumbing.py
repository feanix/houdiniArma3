"""Tests for the pure parts of a3ob_houdini: messages, names, parameter table, LOD filter, HDA sources, import plan.

These run under plain Python and under hython; none of them needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from a3ob_core import p3d
from a3ob_houdini import hda_source, messages, names, nodes, params
from a3ob_houdini.errors import UserError
from a3ob_houdini.p3d_import import lod_filter, plan as plan_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "p3d")


class TestMessages(unittest.TestCase):
    def test_problem_form(self):
        text = messages.format_problem("Geometry", "3 polygons have more than 4 points", "Add a Divide node")
        self.assertEqual(text, "Geometry: 3 polygons have more than 4 points. Add a Divide node.")
        self.assertEqual(messages.format_problem(None, "no file is chosen."), "no file is chosen.")

    def test_element_lists_are_cut(self):
        self.assertEqual(messages.list_elements("prims", [12, 15, 40]), "prims 12, 15, 40")
        self.assertEqual(messages.list_elements("prims", list(range(17))), "prims 0, 1, 2, 3, 4 and 12 more")

    def test_report_cuts_to_the_limit_and_puts_errors_first(self):
        report = messages.Report()
        report.info("info")
        for number in range(30):
            report.warning("warning %d" % number)
        report.error("error")
        lines = report.lines(25)
        self.assertEqual(len(lines), 25)
        self.assertEqual(lines[0], (messages.ERROR, "error"))
        self.assertEqual(lines[-1][0], messages.INFO)
        self.assertIn("8 more messages", lines[-1][1])
        self.assertTrue(report.has_errors)

    def test_helpers_call_the_node(self):
        class FakeNode:
            def __init__(self):
                self.calls = []

            def addMessage(self, text):
                self.calls.append(("message", text))

            def addWarning(self, text):
                self.calls.append(("warning", text))

            def addError(self, text):
                self.calls.append(("error", text))

        node = FakeNode()
        messages.message(node, "a")
        messages.warning(node, "b")
        messages.error(node, "c")
        self.assertEqual(node.calls, [("message", "a"), ("warning", "b"), ("error", "c")])


class TestNames(unittest.TestCase):
    def test_lod_uid_is_stable_positive_and_distinct(self):
        first = names.lod_uid("/obj/geo1/p3d_import1", 3)
        self.assertEqual(first, names.lod_uid("/obj/geo1/p3d_import1", 3))
        self.assertNotEqual(first, names.lod_uid("/obj/geo1/p3d_import2", 3))
        self.assertNotEqual(first, names.lod_uid("/obj/geo1/p3d_import1", 4))
        draw = [names.lod_uid("/obj/geo%d/p3d_import1" % node, lod) for node in range(200) for lod in range(32)]
        self.assertEqual(len(set(draw)), len(draw))
        self.assertTrue(all(0 < value < 2 ** 31 for value in draw))

    def test_int32_conversions(self):
        self.assertEqual(names.to_int32(0xFE000000), -33554432)
        self.assertEqual(names.to_int32(0x02000000), 33554432)
        self.assertEqual(names.from_int32(-33554432), 0xFE000000)
        for value in (0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
            self.assertEqual(names.from_int32(names.to_int32(value)), value)

    def test_float_bits_round_trip(self):
        for value in (1e13, 1e15, 0.5, -0.0, 1.0):
            self.assertEqual(p3d.f32(names.bits_float(names.float_bits(p3d.f32(value)))), p3d.f32(value))

    def test_weight_attribute_names(self):
        self.assertEqual(names.point_weight_attrib("head"), "a3ob_w_head")
        self.assertEqual(names.prim_weight_attrib("head"), "a3ob_fw_head")
        self.assertEqual(names.split_weight_attrib("a3ob_w_head"), ("point", "head"))
        self.assertEqual(names.split_weight_attrib("a3ob_fw_xn__proxies_cka"), ("prim", "xn__proxies_cka"))
        self.assertIsNone(names.split_weight_attrib("a3ob_mass"))

    def test_hou_string_refuses_what_crashes_houdini(self):
        self.assertEqual(names.hou_string("café \x81"), "café \x81")
        with self.assertRaises(ValueError):
            names.hou_string("bad \udc80")
        with self.assertRaises(ValueError):
            names.hou_string("bad \x00")

    def test_check_encodable(self):
        self.assertIsNone(names.check_encodable("data\\é€"))
        self.assertIn("cannot be encoded", names.check_encodable("ř"))


class TestParameterTable(unittest.TestCase):
    def test_every_node_has_one_unique_parameter_list(self):
        specs = nodes.all_specs()
        self.assertIn("p3d_import", specs)
        for kind, spec in specs.items():
            self.assertEqual(spec.kind, kind)
            self.assertEqual(spec.type_name, "a3ob_" + kind)
            self.assertEqual(len({parm.name for parm in spec.parms}), len(spec.parms))

    def test_import_parameters_follow_ux_doc(self):
        spec = nodes.load("p3d_import").SPEC
        self.assertEqual([parm.name for parm in spec.parms], ["file", "reload", "lods", "facegroups"])
        self.assertEqual(spec.parm_index["lods"].default, "*")
        self.assertFalse(spec.parm_index["facegroups"].default)
        self.assertEqual(spec.inputs, (0, 0))
        self.assertEqual(spec.submenu, "A3OB")

    def test_read_parms_falls_back_to_defaults(self):
        spec = nodes.load("p3d_import").SPEC

        class Found:
            def eval(self):
                return "0 2"

        class FakeNode:
            def parm(self, name):
                return Found() if name == "lods" else None

        values = params.read_parms(FakeNode(), spec)
        self.assertEqual(values["lods"], "0 2")
        self.assertEqual(values["file"], "")
        self.assertNotIn("reload", values)

    def test_bad_tables_are_refused(self):
        with self.assertRaises(ValueError):
            params.Parm("x", "X", "slider")
        with self.assertRaises(ValueError):
            params.Parm("x", "X", "button")
        parm = params.Parm("x", "X", "toggle")
        with self.assertRaises(ValueError):
            params.NodeSpec("k", "K", "s", (parm, parm))


class TestHdaSources(unittest.TestCase):
    def test_checked_in_sources_match_the_generator(self):
        for kind, spec in nodes.all_specs().items():
            self.assertEqual(hda_source.differences(spec, OTLS_DIR), [],
                             "run scripts/build_hdas.py to regenerate houdini/otls/%s" % hda_source.hda_folder_name(spec))

    def test_a_changed_file_is_noticed(self):
        spec = nodes.load("p3d_import").SPEC
        files = hda_source.render(spec)
        self.assertEqual(len(files), 9)
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            hda_source.write(spec, directory)
            self.assertEqual(hda_source.differences(spec, directory), [])
            target = os.path.join(directory, hda_source.hda_folder_name(spec), hda_source.section_name(spec),
                                  "DialogScript")
            with open(target, "a", encoding="utf-8") as file:
                file.write("# edited by hand\n")
            self.assertEqual(hda_source.differences(spec, directory),
                             [hda_source.section_name(spec) + "/DialogScript"])

    def test_node_files_are_thin(self):
        spec = nodes.load("p3d_import").SPEC
        files = hda_source.render(spec)
        cook = files[hda_source.section_name(spec) + "/PythonCook"]
        self.assertEqual(cook.strip().count("\n"), 0, "PythonCook must be one line")
        self.assertIn("a3ob_houdini.entry", cook)
        self.assertTrue(files["INDEX__SECTION"].strip())
        self.assertIn("Python:       true", files["INDEX__SECTION"])
        self.assertIn("<toolSubmenu>A3OB</toolSubmenu>", files[hda_source.section_name(spec) + "/Tools.shelf"])
        dialog = files[hda_source.section_name(spec) + "/DialogScript"]
        for parm in spec.parms:
            self.assertIn('name\t"%s"' % parm.name, dialog)

    def test_section_name_encoding(self):
        spec = nodes.load("p3d_import").SPEC
        self.assertEqual(hda_source.section_name(spec), "Sop_1a3ob__p3d__import")
        self.assertEqual(hda_source.hda_folder_name(spec), "a3ob_p3d_import.hda")


class TestLodFilter(unittest.TestCase):
    def test_all_and_lists(self):
        self.assertEqual(lod_filter.parse_lod_filter("*", 4), [0, 1, 2, 3])
        self.assertEqual(lod_filter.parse_lod_filter("", 3), [0, 1, 2])
        self.assertEqual(lod_filter.parse_lod_filter("0 2-4", 6), [0, 2, 3, 4])
        self.assertEqual(lod_filter.parse_lod_filter("3, 1 1", 6), [1, 3])

    def test_bad_text_is_a_user_error(self):
        for text in ("x", "1-", "-2", "5-3", "0 9"):
            with self.assertRaises(UserError, msg=text):
                lod_filter.parse_lod_filter(text, 6)


class TestPlanOnFixtures(unittest.TestCase):
    def read(self, name):
        return p3d.read_p3d_file(os.path.join(FIXTURES, name), strict=False)

    def test_character_proxies_are_packed_and_the_mesh_drops_their_points(self):
        model = self.read("sample_1_character.p3d")
        plan = plan_mod.make_plan(model.lods[0], 0)
        self.assertEqual(len(plan.proxies), 11)
        self.assertEqual(plan.inline_proxy_count, 0)
        self.assertEqual(len(plan.mesh_points), model.lods[0].point_count - 33)
        self.assertEqual(len(plan.mesh_faces), model.lods[0].face_count - 11)
        self.assertEqual([proxy.face for proxy in plan.proxies], sorted(proxy.face for proxy in plan.proxies))
        self.assertEqual(plan.warnings, [])
        self.assertFalse(plan.blobs)
        self.assertFalse(plan.needs_face_uv)

    def test_no_fixture_lod_needs_blobs_or_active_bytes(self):
        for name in ("sample_1_character.p3d", "sample_2_crate.p3d"):
            for index, lod in enumerate(self.read(name).lods):
                plan = plan_mod.make_plan(lod, index)
                self.assertEqual(plan.blobs, [], (name, index))
                self.assertFalse(plan.needs_order_active, (name, index))
                self.assertEqual(plan.warnings, [], (name, index))
                self.assertEqual(len(plan.order), len(lod.taggs))

    def test_corner_order_map_is_its_own_inverse(self):
        for sides in (0, 1, 2, 3, 4):
            order = plan_mod.houdini_corner_order(sides)
            self.assertEqual([order[k] for k in order], list(range(sides)))
        self.assertEqual(plan_mod.houdini_corner_order(4), [0, 3, 2, 1])
        self.assertEqual(plan_mod.houdini_corner_order(3), [0, 2, 1])

    def test_uv_attribute_names(self):
        self.assertEqual([plan_mod.uv_attrib_name(k) for k in (0, 1, 3)], ["uv", "uv2", "uv4"])


if __name__ == "__main__":
    unittest.main()

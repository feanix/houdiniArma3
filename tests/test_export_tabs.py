"""P3D Export and Validate tabs, the rule labels, the Selection Mask menu and count, and the preset a new node starts
from (node QA parts G and H2, D26).

The first class runs under plain Python (the parameter tables). The second needs ``hou`` and the generated assets.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import shutil
import tempfile
import unittest

from a3ob_core.p3d.model import SelectionTagg
from a3ob_houdini import entry, hda_source, names, nodes, params, selection_browser, settings as sm
from a3ob_houdini.nodes import _export_common as common
from a3ob_houdini.nodes import p3d_export as export_node
from a3ob_houdini.p3d_export.type_rules import RULES
from tests import hou_support as hs
from tests.hou_support import hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

TABS = (("tab_checking", "Error Checking"), ("tab_presets", "Presets"), ("tab_paths", "Paths & Names"),
        ("tab_selections", "Selections"), ("tab_layout", "File Layout"), ("tab_rules", "LOD Rules"))
TAB_PARMS = {
    "tab_checking": ["check", "errorlevel", "strict", "dupes", "maxmessages"],
    "tab_presets": ["preset", "apply"],
    "tab_paths": ["relpaths", "projectroot", "lowercase", "czech", "renumber"],
    "tab_selections": ["selmask", "autocomponents"],
    "tab_layout": ["sortlods", "sortsections", "dropuvs"],
}


class TestExportSpec(unittest.TestCase):
    def setUp(self):
        self.spec = nodes.load("p3d_export").SPEC
        self.validate = nodes.load("validate").SPEC

    def test_top_level_then_the_six_tabs_in_the_owner_order(self):
        self.assertEqual([parm.name for parm in self.spec.parms[:3]], ["file", "save", "backup"])
        folders = self.spec.parms[3:]
        self.assertEqual([(p.name, p.label) for p in folders], list(TABS))
        self.assertTrue(all(p.kind == "folder" and p.folder_type == "tabs" for p in folders))
        for folder in folders[:-1]:
            self.assertEqual([c.name for c in folder.children], TAB_PARMS[folder.name])
        rules = folders[-1].children
        self.assertEqual([c.name for c in rules], ["typerules", "triangulate"] + [common.rule_parm_name(r.id) for r in RULES])

    def test_validate_has_the_same_checking_and_rules_tabs_from_the_same_lists(self):
        self.assertEqual([(p.name, p.label) for p in self.validate.parms],
                         [("tab_checking", "Error Checking"), ("tab_rules", "LOD Rules")])
        self.assertEqual(self.validate.parms[1], self.spec.parms[-1])
        self.assertEqual([c.name for c in self.validate.parms[0].children], TAB_PARMS["tab_checking"][1:])
        self.assertEqual(self.validate.parms[0].children, self.spec.parms[3].children[1:])

    def test_rule_labels_and_tooltips_drop_the_id(self):
        for rule in RULES:
            parm = self.spec.parm_index[common.rule_parm_name(rule.id)]
            self.assertEqual(parm.label, rule.label)
            self.assertNotIn("T2-", parm.label)
            self.assertEqual(parm.help, rule.help)  # Z1: plain words, the id stays in the messages only
            self.assertNotIn("T2-", parm.help)
        self.assertEqual(self.spec.parm_index["t2uv2"].label, "At most 2 UV sets")

    def test_project_root_is_a_folder_picker_and_the_mask_a_toggle_menu(self):
        self.assertEqual(self.spec.parm_index["projectroot"].kind, "dir")
        mask = self.spec.parm_index["selmask"]
        self.assertTrue(mask.menu_toggle)
        self.assertEqual(mask.default, "* ^__*")
        self.assertEqual(
            mask.help, "Which point groups become selections in the file. Leave as is to write all of them except "
                       "Houdini's internal ones (names starting with __).")
        self.assertIn("selmask_groups", export_node.MENUS)

    def test_the_hda_has_an_python_oncreated_and_validate_has_none(self):
        files = hda_source.render(self.spec)
        section = hda_source.section_name(self.spec)
        self.assertEqual(files[section + "/OnCreated"], params.CREATED_SCRIPT % "p3d_export")
        self.assertIn("except ImportError", files[section + "/OnCreated"])  # Z7: quiet when the add-on is missing
        self.assertEqual(json.loads(files[section + "/ExtraFileOptions"]), {"OnCreated/IsPython": {"type": "bool", "value": True}})
        self.assertIn("OnCreated\tOnCreated", files[section + "/Sections.list"])
        self.assertFalse([name for name in hda_source.render(self.validate) if name.endswith("OnCreated")])

    def test_the_default_preset_value_stays_lossless(self):
        self.assertEqual(self.spec.parm_index["preset"].default, sm.LOSSLESS)

    def test_every_parameter_has_a_tooltip(self):
        for spec in (self.spec, self.validate):
            for name, parm in spec.parm_index.items():
                self.assertTrue(parm.help and "spec" not in parm.help.lower().split(), name)

    def test_count_line(self):
        self.assertEqual(export_node.selection_count_text([("Geometry", 1)]), "1 selection will be written.")
        self.assertEqual(export_node.selection_count_text([("Resolution 1", 0), ("Geometry", 4)]),
                         "4 selections will be written (Resolution 1 0, Geometry 4).")
        many = [("LOD %d" % n, 1) for n in range(8)]
        self.assertEqual(export_node.selection_count_text(many), "8 selections will be written in 8 LODs.")


@unittest.skipUnless(hs.HAS_HOU, "needs hou (run under hython)")
class TestExportNodes(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="a3ob_tabs_")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.old = os.environ.get(sm.ENV_FILE)
        self.addCleanup(self.restore_settings_env)

    def restore_settings_env(self):
        if self.old is None:
            os.environ.pop(sm.ENV_FILE, None)
        else:
            os.environ[sm.ENV_FILE] = self.old

    def use_settings(self, active=None):
        path = os.path.join(self.folder, "settings.json")
        if active is not None:
            sm.save(sm.Settings(active_preset=active), path)
        os.environ[sm.ENV_FILE] = path

    def crate_export(self):
        _, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        source = make_source(self.network, geo)
        node = self.network.createNode("a3ob_p3d_export")
        node.setFirstInput(source)
        node.parm("file").set(os.path.join(self.folder, "out.p3d").replace("\\", "/"))
        return node

    def test_a_new_node_starts_from_the_active_preset(self):
        self.use_settings(sm.RELEASE)
        node = self.network.createNode("a3ob_p3d_export")
        self.assertEqual(node.parm("preset").eval(), sm.RELEASE)  # the menu shows which preset was applied
        for name in ("lowercase", "relpaths", "sortsections", "autocomponents", "sortlods"):
            self.assertTrue(node.parm(name).eval(), name)
        self.assertEqual(node.parm("file").eval().endswith(".p3d"), True)

    def test_without_a_settings_file_a_new_node_is_lossless(self):
        self.use_settings(None)
        node = self.network.createNode("a3ob_p3d_export")
        self.assertEqual(node.parm("preset").eval(), sm.LOSSLESS)
        for name in ("lowercase", "relpaths", "sortsections", "autocomponents", "sortlods", "dropuvs"):
            self.assertFalse(node.parm(name).eval(), name)

    def test_the_active_preset_can_be_one_of_the_artists_own(self):
        path = os.path.join(self.folder, "settings.json")
        settings = sm.Settings()
        sm.save_preset(settings, "mine", {"sort_lods": True})
        sm.set_active_preset(settings, "mine")
        sm.save(settings, path)
        os.environ[sm.ENV_FILE] = path
        node = self.network.createNode("a3ob_p3d_export")
        self.assertEqual(node.parm("preset").eval(), "mine")
        self.assertTrue(node.parm("sortlods").eval())

    def test_the_new_p3d_starter_export_does_the_same(self):
        from a3ob_houdini import new_p3d
        self.use_settings(sm.RELEASE)
        made = new_p3d.build_starter(None, "starter_d26")
        self.addCleanup(made.network.destroy)
        export = made.nodes["export"]
        self.assertEqual(export.parm("preset").eval(), sm.RELEASE)
        self.assertTrue(export.parm("lowercase").eval())

    def test_old_parameter_names_are_all_still_there(self):
        node = self.network.createNode("a3ob_p3d_export")
        for name in ("file", "save", "check", "errorlevel", "strict", "dupes", "maxmessages", "preset", "apply",
                     "selmask", "lowercase", "relpaths", "projectroot", "sortsections", "sortlods", "autocomponents",
                     "renumber", "czech", "dropuvs", "backup", "typerules", "t2uv2", "t2pathpos"):
            self.assertIsNotNone(node.parm(name), name)
        validate = self.network.createNode("a3ob_validate")
        for name in ("errorlevel", "strict", "dupes", "maxmessages", "typerules", "t2tri"):
            self.assertIsNotNone(validate.parm(name), name)
        self.assertIsNone(validate.parm("check"))

    def test_the_tabs_exist_on_the_node_in_order(self):
        node = self.network.createNode("a3ob_p3d_export")
        entries = node.parmTemplateGroup().entries()
        self.assertEqual([t.name() for t in entries[:3]], ["file", "save", "backup"])
        tabs = [t for t in entries[3:] if isinstance(t, hou.FolderParmTemplate)]
        # Houdini renames the 2nd and later tab of a row (tab_checking_1, ...): only the labels are compared
        self.assertEqual([t.label() for t in tabs], [label for _, label in TABS])
        self.assertTrue(all(t.folderType() == hou.folderType.Tabs for t in tabs))

    def groups_export(self):
        """box -> three point groups -> LOD Properties (visual) -> P3D Export. The groups are head, left arm and
        __hidden (a Houdini internal name that the default mask leaves out)."""
        node = self.network.createNode("box")
        node.parm("ty").set(0.5)
        for name in ("head", "left arm", "__hidden"):
            node = selection_browser.create_selection(node, name)
        props = self.network.createNode("a3ob_lod_properties")
        props.setFirstInput(node)
        props.parm("lodtype").set("visual")
        props.parm("resolution").set(1.0)
        export = self.network.createNode("a3ob_p3d_export")
        export.setFirstInput(props)
        return export

    def test_the_selection_mask_menu_lists_the_input_groups_by_p3d_name(self):
        node = self.groups_export()
        items = entry.menu("p3d_export", "selmask_groups", {"node": node})
        tokens, labels = items[0::2], items[1::2]
        self.assertEqual(sorted(labels), ["__hidden", "head", "left arm"])
        for token, label in zip(tokens, labels):
            self.assertEqual(names.decode_group_name(token), label)  # the token is the Houdini name the mask matches
        self.assertNotEqual(tokens[labels.index("left arm")], "left arm")  # the space is encoded
        template = node.parm("selmask").parmTemplate()
        self.assertEqual(template.menuType(), hou.menuType.StringToggle)  # picking adds or removes the token
        lonely = self.network.createNode("a3ob_p3d_export")
        self.assertEqual(entry.menu("p3d_export", "selmask_groups", {"node": lonely}), [])

    def test_the_menu_lists_each_group_of_a_p3d_geometry_once(self):
        node = self.crate_export()
        model, _, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        expected = {tagg.name for lod in model.lods for tagg in lod.taggs if isinstance(tagg, SelectionTagg)}
        items = entry.menu("p3d_export", "selmask_groups", {"node": node})
        tokens, labels = items[0::2], items[1::2]
        self.assertTrue(expected <= set(labels), expected - set(labels))
        self.assertEqual(len(set(tokens)), len(tokens))

    def test_the_count_line_follows_the_mask(self):
        node = self.groups_export()
        lines = [m for m in cook(node).messages if "will be written" in m]
        self.assertEqual(lines, ["2 selections will be written."])  # head and left arm; __hidden is left out
        node.parm("selmask").set("head")
        self.assertEqual([m for m in cook(node).messages if "will be written" in m], ["1 selection will be written."])
        node.parm("selmask").set("*")
        self.assertEqual([m for m in cook(node).messages if "will be written" in m], ["3 selections will be written."])

    def test_the_count_line_of_a_whole_p3d_names_the_total(self):
        node = self.crate_export()
        result = cook(node)
        self.assertEqual(result.errors, ())
        model, _, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        total = sum(1 for lod in model.lods for tagg in lod.taggs if isinstance(tagg, SelectionTagg))
        lines = [m for m in result.messages if "will be written" in m]
        self.assertEqual(len(lines), 1, result.messages)
        self.assertTrue(lines[0].startswith("%d selection" % total), lines[0])

    def test_validate_cooks_with_tabs(self):
        _, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        node = self.network.createNode("a3ob_validate")
        node.setFirstInput(make_source(self.network, geo))
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertTrue(any("LODs checked" in m for m in result.messages))

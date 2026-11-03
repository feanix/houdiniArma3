"""D17 export options (spec 18): renumber components, Czech selection names, drop UVs.

The first classes work on the plain exporter data and run under plain Python. The last class needs Houdini and checks the
fixtures (L0 with each option alone where the spec says so) and the P3D Export node.

Purpose: catch a wrong match or numbering rule, a table inverse with the wrong winner, a LOD type on the wrong side of
the UV list, and an option that changes the bytes of a file it should leave alone (L0).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import base64
import os
import unittest

from a3ob_core import p3d
from a3ob_core.p3d.lod_types import LodType
from a3ob_houdini import messages
from a3ob_houdini.p3d_export import ExportOptions, build, czech_names
from a3ob_houdini.p3d_export.data import Collection, Details, GroupData, LodData, MeshData, Poly
from a3ob_houdini.p3d_export.options import UV_LOD_TYPES, keeps_uvs
from a3ob_houdini.p3d_export.renames import renumber_components, translate_names
from a3ob_houdini.p3d_export.taggs import Item
from tests import hou_support as hs

CUBE_POINTS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
CUBE_FACES = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (3, 7, 6, 2), (0, 4, 7, 3), (1, 2, 6, 5)]
UV = [(0.25, 0.5)] * 4


def make_lod(lod_type=LodType.VISUAL, resolution=1.0, names=(), uv=True, **details):
    """A cube LOD. ``names`` are selection names; each one holds point ``i``, so the weights tell the selections apart."""
    polys = [Poly(n, f, uvs={"uv": list(UV)} if uv else {}) for n, f in enumerate(CUBE_FACES)]
    mesh = MeshData(points=[tuple(map(float, p)) for p in CUBE_POINTS], polys=polys)
    if uv:
        mesh.uv_names = ["uv"]
    mesh.groups = [GroupData(name, name, [i]) for i, name in enumerate(names)]
    info = Details(lod_type=int(lod_type), resolution=resolution)
    for key, value in details.items():
        setattr(info, key, value)
    return LodData(0, mesh, info)


def export(lods, **options):
    report = messages.Report()
    model = build(Collection(list(lods)), ExportOptions(**options), report)
    return model, report


def written_names(lod):
    return [tagg.name for tagg in lod.taggs if isinstance(tagg, p3d.SelectionTagg)]


def item(name, blob=False):
    return Item(name, p3d.SelectionTagg(name, b"\x01", b"\x01"), blob=blob)


class TestRenumber(unittest.TestCase):
    def test_match_rule(self):
        """Prefix match without case, as A3OB's re.match; anything else is left alone."""
        for name in ("Component3", "component01_hull", "COMPONENT07", "component12abc"):
            items = [item(name)]
            self.assertEqual(renumber_components(items), 1, name)
            self.assertEqual(items[0].tagg.name, "component01", name)
        for name in ("components", "component", "component_1", "my_component1", "__component1", "proxy:component1.001",
                     "#Mass#"):
            items = [item(name)]
            self.assertEqual(renumber_components(items), 0, name)
            self.assertEqual(items[0].tagg.name, name)

    def test_numbers_follow_the_written_order_not_the_old_number(self):
        items = [item("Component03"), item("other"), item("Component01")]
        renumber_components(items)
        self.assertEqual([i.tagg.name for i in items], ["component01", "other", "component02"])

    def test_goes_past_99_and_blobs_take_no_number(self):
        items = [item("component%d" % n) for n in range(1, 101)]
        items.insert(3, item("component7", blob=True))
        renumber_components(items)
        names = [i.tagg.name for i in items if not i.blob]
        self.assertEqual(names[98:], ["component99", "component100"])
        self.assertEqual(items[3].tagg.name, "component7")

    def test_dense_lowercase_names_are_not_counted_as_changed(self):
        items = [item("component01"), item("component02")]
        self.assertEqual(renumber_components(items), 0)

    def test_counter_restarts_per_lod_and_every_lod_type_is_renamed(self):
        lods = [make_lod(LodType.VISUAL, 1.0, ["Component5", "Component2"]),
                make_lod(LodType.GEOMETRY, 0.0, ["Component9"])]
        model, report = export(lods, renumber_components=True)
        self.assertEqual(written_names(model.lods[0]), ["component01", "component02"])
        self.assertEqual(written_names(model.lods[1]), ["component01"])
        self.assertTrue(any("renamed 2 component selections" in t for t in report.texts(messages.INFO)))

    def test_weights_move_with_the_rename(self):
        model, _ = export([make_lod(names=["Component3", "Component1"])], renumber_components=True)
        first, second = model.lods[0].selection("component01"), model.lods[0].selection("component02")
        self.assertEqual(first.members(), [0])
        self.assertEqual(second.members(), [1])

    def test_hint_order_decides_the_numbers_and_keeps_finding_its_token(self):
        data = make_lod(names=["Component1", "Component2", "Component3"], tagg_order=["Component3", "Component1"])
        model, _ = export([data], renumber_components=True)
        lod = model.lods[0]
        self.assertEqual(written_names(lod), ["component01", "component02", "component03"])
        self.assertEqual(lod.selection("component01").members(), [2])  # the hinted Component3 is written first
        self.assertEqual(lod.selection("component02").members(), [0])

    def test_mask_excluded_group_is_not_renamed_and_takes_no_number(self):
        data = make_lod(names=["Component1", "Component2", "Component3"])
        model, _ = export([data], renumber_components=True, selection_mask="* ^Component2")
        self.assertEqual(written_names(model.lods[0]), ["component01", "component02"])
        self.assertEqual(model.lods[0].selection("component02").members(), [2])

    def test_auto_components_then_renumber(self):
        data = make_lod(LodType.GEOMETRY, 0.0)
        model, _ = export([data], auto_components=True, renumber_components=True)
        self.assertEqual(written_names(model.lods[0]), ["component01"])

    def test_off_changes_nothing(self):
        model, report = export([make_lod(names=["Component3", "component1_x"])])
        self.assertEqual(written_names(model.lods[0]), ["Component3", "component1_x"])
        self.assertEqual(report.texts(messages.INFO), [])


class TestCzech(unittest.TestCase):
    def test_whole_name_case_insensitive(self):
        for before, after in (("Turret", "otocvez"), ("wheel_1_1", "kolol1"), ("BODY", "telo"), ("weapon", "zbran")):
            self.assertEqual(czech_names.translate_name(before), after, before)
        for name in ("wheel_1_11", "kolol1", "wheel_1_1_x", "wheel_1_1 ", "Usti_Hlavne", "turretX", "Head"):
            self.assertEqual(czech_names.translate_name(name), name, name)

    def test_table_size_and_the_three_many_to_one_results(self):
        self.assertEqual(len(czech_names.CZECH_TO_ENGLISH), 144)
        self.assertEqual(len(set(czech_names.CZECH_TO_ENGLISH.values())), 141)
        for axis in ("driver", "commander", "gunner"):
            self.assertEqual(czech_names.translate_name("hatch_%s_axis" % axis), "poklop_%s_axis" % axis)

    def test_aliases_give_the_same_czech_name_as_the_misspelling(self):
        for letter, czech in (("a", "osavrtulea"), ("b", "osavrtuleb")):
            self.assertEqual(czech_names.translate_name("propeller_%s_axis" % letter), czech)
            self.assertEqual(czech_names.translate_name("properller_%s_axis" % letter), czech)
        self.assertNotIn("propeller_a_axis", czech_names.CZECH_TO_ENGLISH.values())

    def test_export_renames_selections_only_and_keeps_a_miss_with_its_case(self):
        model, report = export([make_lod(names=["Turret", "Keep_Me", "wheel_1_1"])], czech_names=True)
        self.assertEqual(written_names(model.lods[0]), ["otocvez", "Keep_Me", "kolol1"])
        self.assertEqual(report.texts(messages.WARNING), [])

    def test_collision_is_one_warning_and_both_are_written(self):
        model, report = export([make_lod(LodType.MEMORY, 0.0, ["turret", "otocvez"], uv=False)], czech_names=True)
        self.assertEqual(written_names(model.lods[0]), ["otocvez", "otocvez"])
        hits = [t for t in report.texts(messages.WARNING) if "Czech translation makes" in t]
        self.assertEqual(len(hits), 1)
        self.assertIn("'turret' and 'otocvez'", hits[0])

    def test_renumber_then_czech_then_lowercase(self):
        names = ["Component1", "Body"]
        model_on, _ = export([make_lod(names=names)], renumber_components=True, czech_names=True, lowercase=True)
        model_off, _ = export([make_lod(names=names)], renumber_components=True, czech_names=True)
        self.assertEqual(written_names(model_on.lods[0]), ["component01", "telo"])
        self.assertEqual(written_names(model_on.lods[0]), written_names(model_off.lods[0]))


class TestDropUvs(unittest.TestCase):
    def test_uv_type_list_matches_the_spec(self):
        self.assertEqual(UV_LOD_TYPES, frozenset({0, 1, 2, 3, 18, -1, 4, 26, 27, 28, 31}))

    def test_every_lod_type_is_on_the_right_side(self):
        types = list(LodType)
        self.assertGreaterEqual(len(types), 33)
        checked = 0
        self.assertTrue(keeps_uvs(-1))  # Unknown cannot be written (no signature), so only the list is checked
        for lod_type in types:
            if int(lod_type) == -1:
                continue
            for resolution in (1.0, 0.0, 1000.0):
                model, report = export([make_lod(lod_type, resolution)], drop_uvs=True)
                if model is not None:
                    break
            self.assertIsNotNone(model, "%s: %s" % (lod_type.name, report.texts(messages.ERROR)))
            checked += 1
            written = model.lods[0]
            has_uv_tagg = bool(written.uv_set_ids()) and any(isinstance(t, p3d.UVSetTagg) for t in written.taggs)
            has_face_uv = any(v != 0.0 for v in written.face_uvs)
            self.assertEqual((has_uv_tagg, has_face_uv), (keeps_uvs(int(lod_type)),) * 2, lod_type.name)

    def test_drop_is_all_or_nothing_for_the_lod_and_not_for_others(self):
        model, report = export([make_lod(LodType.VISUAL, 1.0), make_lod(LodType.GEOMETRY, 0.0)], drop_uvs=True)
        keep, drop = model.lods
        self.assertTrue(any(isinstance(t, p3d.UVSetTagg) for t in keep.taggs))
        self.assertTrue(any(v != 0.0 for v in keep.face_uvs))
        self.assertFalse(any(isinstance(t, p3d.UVSetTagg) for t in drop.taggs))
        self.assertFalse(any(v != 0.0 for v in drop.face_uvs))
        infos = report.texts(messages.INFO)
        self.assertIn("Dropped the UVs of 1 LODs (Geometry)", infos)

    def test_off_keeps_uvs_everywhere(self):
        model, report = export([make_lod(LodType.GEOMETRY, 0.0)])
        self.assertTrue(any(isinstance(t, p3d.UVSetTagg) for t in model.lods[0].taggs))
        # No "Dropped the UVs" note. The only info is the D24 note that a Geometry LOD has no mass.
        self.assertEqual([t for t in report.texts(messages.INFO) if "T2-MASS" not in t], [])

    def test_copy_is_judged_by_its_own_type(self):
        data = make_lod(LodType.VISUAL, 1.0, copy_types=[int(LodType.GEOMETRY)], copy_resolutions=[0.0])
        model, _ = export([data], drop_uvs=True)
        original, copy = model.lods
        self.assertEqual(original.name, "Resolution 1")
        self.assertTrue(any(v != 0.0 for v in original.face_uvs))
        self.assertTrue(any(isinstance(t, p3d.UVSetTagg) for t in original.taggs))
        self.assertFalse(any(v != 0.0 for v in copy.face_uvs))
        self.assertFalse(any(isinstance(t, p3d.UVSetTagg) for t in copy.taggs))

    def test_uvset_blob_is_dropped_without_the_mesh_changed_warning(self):
        blob = base64.b64encode(b"\x05\x00\x00\x00" + bytes(8)).decode("ascii")
        data = make_lod(LodType.GEOMETRY, 0.0, uv=False, unknown_names=["#UVSet#"], unknown_active=[1],
                        unknown_data=[blob], source_counts=(0, 0, 0))
        model, report = export([data], drop_uvs=True)
        self.assertFalse([t for t in report.texts(messages.WARNING) if "opaque" in t])
        self.assertFalse(any(t.name == p3d.UVSET for t in model.lods[0].taggs))
        _, control = export([make_lod(LodType.GEOMETRY, 0.0, uv=False, unknown_names=["#UVSet#"], unknown_active=[1],
                                      unknown_data=[blob], source_counts=(0, 0, 0))])
        self.assertTrue([t for t in control.texts(messages.WARNING) if "opaque" in t])

    def test_validator_sees_the_uvs_before_they_are_dropped(self):
        data = make_lod(LodType.VISUAL, 1.0)
        data.mesh.uv_names = ["uv", "uv2", "uv3"]
        data.details.lod_type = int(LodType.VISUAL)
        model, report = export([data], drop_uvs=True)
        self.assertTrue(any("T2-UV2" in t for t in report.texts(messages.WARNING)))


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestFixtures(unittest.TestCase):
    """Spec 18.2 to 18.4 round trips on the two fixtures."""

    def export(self, name, **options):
        from a3ob_houdini import p3d_export
        model, geo, _, _ = hs.imported_fixture(name)
        report = messages.Report()
        data = p3d_export.build_bytes([geo], ExportOptions(**options), report)
        self.assertEqual(report.texts(messages.ERROR), [])
        with open(os.path.join(hs.FIXTURES, name), "rb") as file:
            source = file.read()
        return source, data, model, p3d.read_p3d(data, strict=False), report

    def test_lossless_still_byte_identical(self):
        for name in ("sample_1_character.p3d", "sample_2_crate.p3d"):
            source, data, _, _, _ = self.export(name)
            self.assertTrue(data == source, name)

    def test_renumber_alone_is_l0_on_both(self):
        for name in ("sample_1_character.p3d", "sample_2_crate.p3d"):
            source, data, _, _, _ = self.export(name, renumber_components=True)
            self.assertTrue(data == source, name)

    def test_czech_alone_crate_l0_character_three_renames(self):
        source, data, _, _, _ = self.export("sample_2_crate.p3d", czech_names=True)
        self.assertTrue(data == source)
        source, data, model, written, _ = self.export("sample_1_character.p3d", czech_names=True)
        renames = {"aimpoint": "zamerny", "body": "telo", "weapon": "zbran"}
        changed = set()
        for before, after in zip(model.lods, written.lods):
            self.assertEqual(len(before.taggs), len(after.taggs))
            for a, b in zip(before.taggs, after.taggs):
                if a.name != b.name:
                    self.assertEqual(renames.get(a.name), b.name)
                    changed.add(a.name)
                self.assertEqual(a.payload(), b.payload())
        self.assertEqual(changed, set(renames))

    def test_drop_uvs_character_l0_crate_drops_lods_3_to_6(self):
        source, data, _, _, _ = self.export("sample_1_character.p3d", drop_uvs=True)
        self.assertTrue(data == source)
        source, data, model, written, report = self.export("sample_2_crate.p3d", drop_uvs=True)
        for index in range(3):
            self.assertEqual(p3d.write_p3d(p3d.P3DModel([written.lods[index]])),
                             p3d.write_p3d(p3d.P3DModel([model.lods[index]])), index)
        for index in range(3, 7):
            lod = written.lods[index]
            self.assertFalse(any(t.name == p3d.UVSET for t in lod.taggs), index)
            self.assertFalse(any(v != 0.0 for v in lod.face_uvs), index)
        self.assertTrue(any(t.startswith("Dropped the UVs of 4 LODs") for t in report.texts(messages.INFO)))


if hs.HAS_HOU:
    from tests.mass_proxy_support import NodeTestCase
else:
    NodeTestCase = unittest.TestCase


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestNode(NodeTestCase):
    def test_parms_exist_apply_fills_them_and_lossless_clears_them(self):
        from a3ob_houdini import nodes, settings
        from a3ob_houdini.nodes import _export_common as common
        module = nodes.load("p3d_export")
        for name in ("renumber", "czech", "dropuvs"):
            self.assertIn(name, module.OPTION_PARMS)
        node = self.network.createNode("a3ob_p3d_export")
        names = ("renumber", "czech", "dropuvs", "sortlods")
        self.assertEqual([node.parm(n).eval() for n in names[:3]], [0, 0, 0])
        module.apply_preset(node, settings.RELEASE, settings.Settings())
        self.assertEqual([node.parm(n).eval() for n in names], [0, 0, 1, 1])
        module.apply_preset(node, settings.LOSSLESS, settings.Settings())
        self.assertEqual([node.parm(n).eval() for n in names], [0, 0, 0, 0])
        node.parm("czech").set(1)
        options = common.options_from(module.read_parms(node, module.SPEC))
        self.assertEqual((options.renumber_components, options.czech_names, options.drop_uvs), (False, True, False))


if __name__ == "__main__":
    unittest.main()

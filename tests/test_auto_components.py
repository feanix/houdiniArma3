"""Auto-components on all 14 A3OB geometry LOD types (D19 item 4, task #22).

Purpose: catch the set being narrower than A3OB's ``lod_geometries``, a non-geometry type getting components, a validator
or mass set widened by mistake, and the fixtures' validator warnings or Lossless bytes changing. The first classes run
under plain Python; the last one needs Houdini.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from a3ob_core import p3d
from a3ob_core.p3d.lod_types import LOD_TYPES, LodType
from a3ob_houdini import messages
from a3ob_houdini.p3d_export import ExportOptions, selections, type_rules
from tests import hou_support as hs
from tests.test_d17_options import export, make_lod, written_names

#: A3OB utilities/data.py lines 132-147, written out by hand so a wrong edit of the port's constant shows up.
LOD_GEOMETRIES = (6, 7, 8, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 30)

if hs.HAS_HOU:
    import hou
    from a3ob_houdini import p3d_export
    from tests.test_export_roundtrip import lod_geometry


class TestAutoComponentTypes(unittest.TestCase):
    def test_the_set_is_the_14_lod_geometries(self):
        self.assertEqual(sorted(selections.AUTO_COMPONENT_TYPES), sorted(LOD_GEOMETRIES))
        self.assertEqual(len(LOD_GEOMETRIES), 14)

    def test_every_geometry_type_gets_a_component_and_no_other_type_does(self):
        for lod_type in LodType:
            if lod_type == LodType.UNKNOWN:
                continue
            model, _ = export([make_lod(lod_type, 0.0)], auto_components=True)
            names = [n for n in written_names(model.lods[0]) if n.startswith("Component")]
            expected = ["Component01"] if int(lod_type) in LOD_GEOMETRIES else []
            self.assertEqual(names, expected, LOD_TYPES[lod_type].display_name)

    def test_resolution_and_memory_get_none(self):
        for lod_type, resolution in ((LodType.VISUAL, 1.0), (LodType.MEMORY, 0.0)):
            model, _ = export([make_lod(lod_type, resolution)], auto_components=True)
            self.assertEqual(written_names(model.lods[0]), [])

    def test_off_makes_none_on_any_type(self):
        for number in LOD_GEOMETRIES:
            model, _ = export([make_lod(LodType(number), 0.0)])
            self.assertEqual(written_names(model.lods[0]), [], number)

    def test_existing_components_stop_the_generation(self):
        model, _ = export([make_lod(LodType.FIRE_GEOMETRY, 0.0, ["component07"])], auto_components=True)
        self.assertEqual(written_names(model.lods[0]), ["component07"])


class TestOtherSetsStay(unittest.TestCase):
    """Only the auto-components set widens (criterion 2). Each other set keeps its A3OB-correct types."""

    def test_selections_geometry_types_is_still_the_three(self):
        # The mass warning (spec 5, D14 Q-3). A3OB exports mass for Geometry only (io/export_p3d.py:579).
        self.assertEqual(selections.GEOMETRY_TYPES, (6, 7, 8))

    def test_type_rules_geometry_types_is_still_the_three(self):
        # D14 names these three for the per-LOD-type rules (spec 17.1), not lod_geometries.
        self.assertEqual(sorted(int(t) for t in type_rules.GEOMETRY_TYPES), [6, 7, 8])

    def test_the_component_rule_is_not_widened(self):
        rule = type_rules.rule_by_id("T2-COMP")
        self.assertEqual(sorted(int(t) for t in rule.types), [6, 7, 8, 30])

    def test_the_mass_warning_does_not_cover_fire_geometry(self):
        from a3ob_houdini.nodes import mass
        self.assertEqual(sorted(int(t) for t in mass.GEOMETRY_TYPES), [6, 7, 8])


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestFixtures(unittest.TestCase):
    def test_fire_geometry_of_the_character_gets_components(self):
        model, geo, _, _ = hs.imported_fixture("sample_1_character.p3d")
        source = model.lods[8]
        self.assertEqual(source.name, "Fire Geometry")
        edited = lod_geometry(geo, 8)
        for name in [n for n in source.selection_names() if n.startswith("component")]:
            edited.findPointGroup(name).destroy()
        report = messages.Report()
        out = p3d.read_p3d(p3d_export.build_bytes([edited], ExportOptions(auto_components=True), report), strict=False)
        written = out.lods[0]
        made = [n for n in written.selection_names() if n.startswith("Component")]
        self.assertTrue(made)
        self.assertEqual(made, ["Component%02d" % n for n in range(1, len(made) + 1)])
        covered = sorted(point for name in made for point in written.selection(name).members())
        self.assertEqual(covered, sorted(set(covered)))  # every point is in one piece only
        # With the components still there, nothing is made.
        out = p3d.read_p3d(p3d_export.build_bytes([lod_geometry(geo, 8)], ExportOptions(auto_components=True),
                                                  messages.Report()), strict=False)
        self.assertFalse([n for n in out.lods[0].selection_names() if n.startswith("Component")])

    def test_validator_warnings_on_both_fixtures_are_unchanged(self):
        # Recorded on main before #22. Auto-components on or off, the messages are the same.
        expected = {"sample_1_character.p3d": [],
                    "sample_2_crate.p3d": ["Roadway: polygon 0 has no sound texture (a3ob_texture is empty) "
                                           "(rule T2-RW-SOUND). Set a texture on every polygon."]}
        for name in hs.FIXTURE_NAMES:
            _, geo, _, _ = hs.imported_fixture(name)
            for auto in (False, True):
                report = messages.Report()
                p3d_export.run([geo], ExportOptions(auto_components=auto), report)
                # Task #63: the character fixture has raw bone weights, so the new weight rules speak; they have their own tests.
                texts = [t for t in report.texts(messages.WARNING) if "(rule T2-WGT" not in t]
                self.assertEqual(texts, expected[name], (name, auto))

    def test_lossless_still_exports_both_fixtures_byte_identically(self):
        for name in hs.FIXTURE_NAMES:
            _, geo, _, _ = hs.imported_fixture(name)
            with open(os.path.join(hs.FIXTURES, name), "rb") as file:
                source = file.read()
            data = p3d_export.build_bytes([geo], ExportOptions(), messages.Report())
            self.assertEqual(data, source, name)


if __name__ == "__main__":
    unittest.main()

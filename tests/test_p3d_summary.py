"""Parse summaries of the P3D fixtures: pins facts read from the files.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LodType

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "p3d")

_MODELS = {}


def load(name):
    if name not in _MODELS:
        _MODELS[name] = p3d.read_p3d_file(os.path.join(FIXTURES, name))
    return _MODELS[name]


class TestCharacterSummary(unittest.TestCase):
    """sample_1_character.p3d: 9 LODs, 11 proxies, mass, UV sets."""

    def setUp(self):
        self.model = load("sample_1_character.p3d")

    def test_lods(self):
        summary = [(lod.lod_type, lod.resolution, lod.point_count, lod.normal_count, lod.face_count)
                   for lod in self.model.lods]
        self.assertEqual(summary, [
            (LodType.VISUAL, 1.0, 5379, 5351, 10699),
            (LodType.VISUAL, 2.0, 1371, 1343, 2683),
            (LodType.VIEW_PILOT, 0.0, 5376, 5351, 10698),
            (LodType.SHADOW, 0.0, 691, 1342, 1343),
            (LodType.GEOMETRY, 0.0, 34, 34, 64),
            (LodType.MEMORY, 0.0, 36, 4, 4),
            (LodType.LANDCONTACT, 0.0, 0, 0, 0),
            (LodType.HITPOINTS, 0.0, 31, 0, 0),
            (LodType.FIRE_GEOMETRY, 0.0, 226, 226, 380),
        ])
        self.assertEqual([lod.name for lod in self.model.lods][:4],
                         ["Resolution 1", "Resolution 2", "View - Pilot", "Shadow Volume 0"])
        self.assertEqual(self.model.duplicate_lods(), [])
        self.assertIs(self.model.find_lod(LodType.GEOMETRY), self.model.lods[4])
        for lod in self.model.lods:
            self.assertEqual(lod.flags, 0)
            self.assertEqual(lod.check(), [])

    def test_proxies(self):
        lod = self.model.lods[0]
        proxies = lod.proxies()
        self.assertEqual(len(proxies), 11)
        self.assertEqual([p.path.rsplit("\\", 1)[-1] for p in proxies], [
            "backpack", "binoculars", "bysta", "equipment", "flag", "glasses", "headgear", "hmd", "launcher",
            "pistol", "weapon"])
        self.assertTrue(all(p.index == 1 for p in proxies))
        self.assertEqual(proxies[0].path, "\\a3\\characters_f\\proxies\\backpack")
        self.assertEqual(proxies[0].selection.name, "proxy:\\a3\\characters_f\\proxies\\backpack.001")
        self.assertEqual([len(lod.proxies()) for lod in self.model.lods], [11, 11, 10, 7, 0, 4, 0, 0, 0])

        # The backpack proxy is the canonical triangle at the origin.
        self.assertEqual(lod.proxy_points(proxies[0].selection), ((0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 1.0)))
        frame = lod.proxy_frame(proxies[0].selection)
        self.assertEqual(frame, p3d.ProxyFrame((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))

        # The launcher proxy in the Memory LOD: long leg along -X, short leg along -Y.
        memory = self.model.lods[5]
        launcher = [p for p in memory.proxies() if p.path.endswith("launcher")][0]
        frame = memory.proxy_frame(launcher.selection)
        for got, expected in zip(frame.origin + frame.x_axis + frame.y_axis + frame.z_axis,
                                 (1.0061293, 0.0029958, 0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, 0.0, -1.0, 0.0)):
            self.assertAlmostEqual(got, expected, places=5)

    def test_selections(self):
        lod = self.model.lods[0]
        self.assertEqual(len(lod.selections()), 35)
        self.assertEqual(lod.selection_names()[:4], ["head", "hip", "spine1", "spine2"])
        self.assertIn("camo1", lod.selection_names())
        # Bone weights use the whole byte range; byte 255 is a member with weight 0.0.
        weights = set()
        for selection in lod.selections():
            weights.update(selection.vertex_weights)
        self.assertIn(255, weights)
        self.assertIn(128, weights)
        self.assertGreater(len(weights), 200)
        # Face selection bytes are present (A3OB dropped them).
        self.assertEqual(sum(1 for s in lod.selections() for b in s.face_weights if b), 29808)
        head = lod.selection("HEAD")  # case-insensitive lookup
        self.assertEqual(head.name, "head")

    def test_mass_uv_sharp_properties(self):
        geometry = self.model.lods[4]
        self.assertEqual(len(geometry.masses), 34)
        self.assertAlmostEqual(geometry.total_mass, 87.5, places=4)
        self.assertEqual(geometry.properties(), [("class", "man")])
        self.assertEqual(sum(1 for lod in self.model.lods if lod.masses is not None), 1)

        visual = self.model.lods[0]
        self.assertEqual(visual.uv_set_ids(), [0])
        self.assertEqual(visual.corner_count, 32097)
        self.assertEqual(len(visual.uv_set(0)), 2 * 32097)
        self.assertEqual(visual.get_property("LODNoShadow"), "1")

        self.assertEqual(len(self.model.lods[3].sharp_edges()), 2004)
        self.assertEqual(visual.sharp_edges(), [])
        self.assertEqual([t.name for t in visual.taggs[:3]], ["#SharpEdges#", "#UVSet#", "#Property#"])

    def test_raw_flags(self):
        visual = self.model.lods[0]
        self.assertEqual(sorted(set(visual.point_flags)), [0, 0x02000000])
        self.assertEqual(sum(1 for f in visual.point_flags if f == 0x02000000), 336)
        self.assertEqual(set(visual.face_flags), {0})
        self.assertEqual(set(visual.face_sides), {3})
        self.assertEqual(self.model.lods[8].face_materials[0], "a3\\data_f\\penetration\\body.rvmat")


class TestCrateSummary(unittest.TestCase):
    """sample_2_crate.p3d: 7 LODs including Roadway."""

    def setUp(self):
        self.model = load("sample_2_crate.p3d")

    def test_lods(self):
        summary = [(lod.lod_type, lod.resolution, lod.point_count, lod.face_count) for lod in self.model.lods]
        self.assertEqual(summary, [
            (LodType.VISUAL, 1.0, 96, 168),
            (LodType.VISUAL, 2.0, 48, 72),
            (LodType.SHADOW, 0.0, 16, 24),
            (LodType.GEOMETRY, 0.0, 16, 24),
            (LodType.GEOMETRY_BUOY, 0.0, 8, 12),
            (LodType.MEMORY, 0.0, 4, 0),
            (LodType.ROADWAY, 0.0, 8, 5),
        ])

    def test_roadway(self):
        roadway = self.model.find_lod(LodType.ROADWAY)
        self.assertIs(roadway, self.model.lods[6])
        self.assertEqual(roadway.name, "Roadway")
        self.assertEqual(set(roadway.face_sides), {4})  # quads
        self.assertEqual(roadway.corner_count, 20)
        self.assertEqual(roadway.uv_set_ids(), [0])
        self.assertEqual(len(roadway.face(0).vertices), 4)

    def test_geometry(self):
        geometry = self.model.lods[3]
        self.assertEqual(geometry.properties(), [("autocenter", "0"), ("buoyancy", "1")])
        self.assertEqual(list(geometry.masses), [1.25] * 16)
        self.assertEqual(geometry.total_mass, 20.0)
        self.assertEqual(geometry.selection_names(), ["component01", "component02", "lid"])
        self.assertEqual(set(geometry.face_materials), {"a3\\data_f\\penetration\\plastic.rvmat"})

    def test_other_lods(self):
        self.assertEqual(len(self.model.lods[0].sharp_edges()), 112)
        self.assertEqual(self.model.lods[0].selection_names(), ["lid", "camo1"])
        self.assertEqual(self.model.lods[5].selection_names(), ["lid_axis", "interaction"])
        self.assertEqual(self.model.lods[0].face(0).texture, "#(argb,8,8,3)color(0.040,0.435,0.773,1.000,co)")
        self.assertEqual(sum(len(lod.proxies()) for lod in self.model.lods), 0)
        # The Memory LOD has no faces but still carries an (empty) UV set.
        self.assertEqual(len(self.model.lods[5].uv_set(0)), 0)


if __name__ == "__main__":
    unittest.main()

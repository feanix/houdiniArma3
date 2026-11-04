"""Task #48: Shadow Volume and Underground LODs are always written with hard edges. The plain-data tests run under
CPython; the round trip through Houdini needs hython and skips otherwise.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_core import p3d
from a3ob_core.p3d.lod_types import LodType
from a3ob_houdini import messages
from a3ob_houdini.p3d_export import ExportOptions, build, harden, type_rules
from a3ob_houdini.p3d_export.data import Collection
from tests import hou_support as hs
from tests.test_type_rules import CUBE_FACES, CUBE_POINTS, lod

if hs.HAS_HOU:
    from a3ob_houdini import p3d_export


def edge_set(pairs):
    return {(min(a, b), max(a, b)) for a, b in pairs}


CUBE_EDGES = edge_set((a, b) for face in CUBE_FACES for a, b in zip(face, face[1:] + face[:1]))


class TestHardenPlainData(unittest.TestCase):
    def smooth_shadow(self, lod_type=LodType.SHADOW):
        return lod(lod_type, resolution=1000.0, flat=False)

    def test_smooth_shadow_gets_all_edges_and_face_normals(self):
        data = self.smooth_shadow()
        report = messages.Report()
        out = harden.harden_lod(data, report)
        self.assertEqual(len(CUBE_EDGES), 12)
        self.assertEqual(edge_set(out.mesh.sharp), CUBE_EDGES)
        self.assertEqual(len(out.mesh.sharp), 12)
        for poly in out.mesh.polys:
            self.assertEqual(len(set(poly.normals)), 1)
        self.assertEqual(report.texts(messages.INFO),
                         ["Shadow Volume 1000: all edges written as sharp (shadow LODs need hard edges)."])
        self.assertIsNone(data.mesh.sharp)  # the input is not changed
        self.assertIsNone(data.mesh.polys[0].normals)

    def test_underground_and_shadow_view_types_are_hardened(self):
        for lod_type in (LodType.UNDERGROUND, LodType.SHADOW_VIEW_CARGO):
            out = harden.harden_lod(self.smooth_shadow(lod_type), messages.Report())
            self.assertEqual(edge_set(out.mesh.sharp), CUBE_EDGES, lod_type)

    def test_other_types_are_left_alone(self):
        data = lod(LodType.VISUAL, resolution=1.0, flat=False)
        report = messages.Report()
        self.assertIs(harden.harden_lod(data, report), data)
        self.assertEqual(report.texts(messages.INFO), [])

    def test_already_sharp_is_the_same_object_and_silent(self):
        data = lod(LodType.SHADOW, resolution=1000.0)
        data.mesh.sharp = [(a, b) for face in CUBE_FACES for a, b in zip(face, face[1:] + face[:1])][::-1]
        report = messages.Report()
        self.assertIs(harden.harden_lod(data, report), data)
        self.assertEqual(report.texts(messages.INFO), [])

    def test_existing_pairs_keep_their_order_and_missing_ones_follow(self):
        data = lod(LodType.SHADOW, resolution=1000.0)
        first = [(3, 0), (0, 1)]
        data.mesh.sharp = list(first)
        out = harden.harden_lod(data, messages.Report())
        self.assertEqual(out.mesh.sharp[:2], first)
        self.assertEqual(edge_set(out.mesh.sharp), CUBE_EDGES)
        self.assertEqual(len(out.mesh.sharp), 12)

    def test_the_rule_t2_sharp_is_gone_and_validate_has_nothing_to_warn_about(self):
        self.assertNotIn("T2-SHARP", type_rules.RULE_IDS)
        report = messages.Report()
        model = build(Collection([self.smooth_shadow()]), ExportOptions(rules_off=()), report)
        self.assertIsNotNone(model)
        self.assertFalse([text for text in report.texts(messages.WARNING) if "T2-SHARP" in text])
        self.assertEqual(len(report.texts(messages.INFO)), 1)

    def test_build_writes_sharp_tagg_and_one_normal_per_face(self):
        report = messages.Report()
        model = build(Collection([self.smooth_shadow()]), ExportOptions(), report)
        written = model.lods[0]
        pairs = [tagg for tagg in written.taggs if isinstance(tagg, p3d.SharpEdgesTagg)]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(len(pairs[0].pairs()), 12)


@unittest.skipUnless(hs.HAS_HOU, "needs Houdini")
class TestHardenRoundTrip(unittest.TestCase):
    def smooth_box(self):
        """A shadow volume box with smooth vertex normals and no sharp edges."""
        points = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
        polygons = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        normals, indices = [], []
        for face in polygons:
            for k in face:
                length = 3 ** 0.5
                normals.append(tuple(c / length for c in points[k]))
                indices.append(len(normals) - 1)
        lod = p3d.LOD(p3d.LodType.SHADOW, 1000.0)
        lod.set_mesh(points=points, polygons=polygons, normals=[c for n in normals for c in n], normal_indices=indices,
                     uvs=[0.0] * 48, point_flags=[0] * 8, face_flags=[0] * 6, textures=[""] * 6, materials=[""] * 6)
        return p3d.P3DModel([lod])

    def export(self, geo):
        report = messages.Report()
        data = p3d_export.build_bytes([geo], None, report)
        self.assertIsNotNone(data)
        return data, report

    def test_smooth_shadow_box_is_written_sharp_and_reimports_sharp(self):
        geo, _, _ = hs.import_model(self.smooth_box())
        before = [prim.number() for prim in geo.prims()]
        data, report = self.export(geo)
        self.assertEqual(len([t for t in report.texts(messages.INFO) if harden.HARDEN_NOTE in t]), 1)
        lod = p3d.read_p3d(data, strict=False).lods[0]
        sharp = [tagg for tagg in lod.taggs if isinstance(tagg, p3d.SharpEdgesTagg)][0]
        self.assertEqual(len(edge_set(sharp.pairs())), 12)
        self.assertEqual(len(sharp.pairs()), 12)
        again, _, _ = hs.import_model(p3d.read_p3d(data, strict=False))
        inner = again.prims()[0].getEmbeddedGeometry()
        self.assertEqual(len(list(inner.findEdgeGroup("a3ob_sharp").edges())), 12)
        # the Houdini geometry of the node is not changed
        inner_in = geo.prims()[0].getEmbeddedGeometry()
        self.assertIsNone(inner_in.findEdgeGroup("a3ob_sharp"))
        self.assertEqual([prim.number() for prim in geo.prims()], before)

    def test_already_sharp_is_byte_identical(self):
        geo, _, _ = hs.import_model(self.smooth_box())
        first, _ = self.export(geo)
        again, _, _ = hs.import_model(p3d.read_p3d(first, strict=False))
        second, report = self.export(again)
        self.assertEqual(second, first)
        self.assertEqual([t for t in report.texts(messages.INFO) if harden.HARDEN_NOTE in t], [])


if __name__ == "__main__":
    unittest.main()

"""The import permutation and the remap of index-bound records (task #36, finding F-4 of #29, spec 13.6).

Purpose: with interleaved proxies the importer moves points and faces. Every record that keeps point, face or corner
numbers has to move with them. These tests pin the pure part (no Houdini): the permutation itself, the payload remap
for each record kind, and the plan (which blob is rewritten, which is dropped with a warning). The Houdini round trip is
in test_real_file_layout.py.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import struct
import unittest

from a3ob_core import p3d
from a3ob_core.p3d import LodType

from a3ob_houdini.p3d_import import plan as plan_mod
from a3ob_houdini.permutation import Permutation, remap_payload

UP = (0.0, 0.0, 1.0)
SQUARE = [(0.0, 0.0, 0.5), (1.0, 0.0, 0.5), (1.0, 1.0, 0.5), (0.0, 1.0, 0.5)]
PROXY_A = [(5.0, 0.0, 0.0), (5.0, 1.0, 0.0), (5.0, 0.0, 1.0)]
PROXY_B = [(6.0, 0.0, 0.0), (6.0, 1.0, 0.0), (6.0, 0.0, 1.0)]
NAME_A = "proxy:\\a3\\test\\thing.001"
NAME_B = "proxy:\\a3\\test\\other.002"


def interleaved_lod():
    """10 points and 4 faces. Proxy A is points 0-2 and face 0, the square is points 3-6 and faces 1 and 3, proxy B is
    points 7-9 and face 2: both proxies sit among mesh points and faces, so the import moves them.

    File order: A0 A1 A2 s0 s1 s2 s3 B0 B1 B2. Export order: s0 s1 s2 s3 A0 A1 A2 B0 B1 B2 (faces: f1 f3 A B).
    The mask bytes are all different, so a wrong move shows. Sharp pairs touch proxy and mesh points, are real edges
    or not, and one is degenerate."""
    lod = p3d.LOD(LodType.VISUAL, 1.0)
    points = PROXY_A + SQUARE + PROXY_B
    lod.set_mesh(points, [(0, 1, 2), (3, 4, 5), (7, 8, 9), (3, 5, 6)], [UP], [0] * 12)
    lod.add_selection(NAME_A, {0: 1.0, 1: 1.0, 2: 1.0})
    lod.add_selection(NAME_B, {7: 1.0, 8: 1.0, 9: 1.0})
    lod.taggs.append(p3d.MaskTagg("#Selected#", bytes([1, 0, 0, 0, 2, 0, 0, 0, 3, 0]), bytes([4, 0, 5, 6])))
    lod.set_sharp_edges([(0, 1), (3, 4), (4, 8), (4, 6), (7, 9), (3, 3)])
    return lod


def position_of(lod, index):
    return tuple(lod.point(index))


def bytes_by_position(lod, tagg):
    """``{position: byte}`` of the points a mask marks, and ``{sorted positions of the corners: byte}`` of its faces."""
    points = {position_of(lod, i): b for i, b in enumerate(tagg.vertex_bytes) if b}
    faces = {tuple(sorted(position_of(lod, v) for v in lod.face(i).vertices)): b
             for i, b in enumerate(tagg.face_bytes) if b}
    return points, faces


def pairs_by_position(lod, tagg):
    return sorted(tuple(sorted((position_of(lod, a), position_of(lod, b)))) for a, b in tagg.pairs())


class TestPermutation(unittest.TestCase):
    def test_import_order_is_mesh_first_then_proxies(self):
        plan = plan_mod.make_plan(interleaved_lod(), 0)
        perm = plan.permutation
        self.assertEqual(perm.point_old, [3, 4, 5, 6, 0, 1, 2, 7, 8, 9])
        self.assertEqual(perm.face_old, [1, 3, 0, 2])
        self.assertEqual([perm.point_new[old] for old in range(10)], [4, 5, 6, 0, 1, 2, 3, 7, 8, 9])
        self.assertEqual(perm.face_new, [2, 0, 3, 1])
        self.assertFalse(perm.is_identity)

    def test_proxies_at_the_end_are_the_identity(self):
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        lod.set_mesh(SQUARE + PROXY_A, [(0, 1, 2), (0, 2, 3), (4, 5, 6)], [UP], [0] * 9)
        lod.add_selection(NAME_A, {4: 1.0, 5: 1.0, 6: 1.0})
        self.assertTrue(plan_mod.make_plan(lod, 0).permutation.is_identity)

    def test_corners_move_with_their_face(self):
        perm = Permutation([0], [1, 0], [3, 4])  # face 0 has corners 0-2, face 1 has corners 3-6
        self.assertEqual(perm.corner_old, [3, 4, 5, 6, 0, 1, 2])
        self.assertEqual(list(perm.corners(bytes(range(7)))), [3, 4, 5, 6, 0, 1, 2])

    def test_a_number_outside_the_lod_stays(self):
        perm = Permutation([1, 0], [0], [3])
        self.assertEqual((perm.new_point(0), perm.new_point(1), perm.new_point(7)), (1, 0, 7))


class TestRemapPayload(unittest.TestCase):
    def setUp(self):
        self.perm = Permutation([2, 0, 1], [1, 0], [3, 3])  # 3 points, 2 faces, 6 corners

    def test_mask_and_selection_and_hide(self):
        payload = bytes([10, 11, 12, 20, 21])
        expected = bytes([12, 10, 11, 21, 20])
        self.assertEqual(remap_payload(p3d.MaskTagg("#Selected#", payload[:3], payload[3:]), self.perm), expected)
        self.assertEqual(remap_payload(p3d.SelectionTagg("a", payload[:3], payload[3:]), self.perm), expected)
        self.assertEqual(remap_payload(p3d.RawTagg("#Hide#", payload), self.perm), expected)

    def test_mass_animation_and_uvset(self):
        mass = remap_payload(p3d.MassTagg([1.0, 2.0, 3.0]), self.perm)
        self.assertEqual(struct.unpack("<3f", mass), (3.0, 1.0, 2.0))
        anim = p3d.AnimationTagg(0.5, [0, 0, 0, 1, 1, 1, 2, 2, 2])
        out = remap_payload(anim, self.perm)
        self.assertEqual(struct.unpack("<f9f", out), (0.5, 2, 2, 2, 0, 0, 0, 1, 1, 1))
        uvs = [float(k) for k in range(12)]  # 6 corners, (u, v) each
        out = remap_payload(p3d.UVSetTagg(3, uvs), self.perm)
        self.assertEqual(struct.unpack("<I12f", out), (3, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0))

    def test_sharp_pairs_and_property(self):
        out = remap_payload(p3d.SharpEdgesTagg.from_pairs([(0, 1), (2, 2)]), self.perm)
        self.assertEqual(struct.unpack("<4I", out), (1, 2, 0, 0))
        self.assertIsNone(remap_payload(p3d.SharpEdgesTagg.from_pairs([(0, 3)]), self.perm))
        prop = p3d.PropertyTagg("a", "b")
        self.assertEqual(remap_payload(prop, self.perm), prop.payload())

    def test_what_cannot_move_gives_none(self):
        self.assertIsNone(remap_payload(p3d.RawTagg("#Unknown#", b"12345"), self.perm))
        self.assertIsNone(remap_payload(p3d.RawTagg("#Hide#", b"1234"), self.perm))  # not points + faces
        self.assertIsNone(remap_payload(p3d.MaskTagg("#Lock#", b"1", b"2"), self.perm))  # wrong size


class TestPlanRemapsBlobs(unittest.TestCase):
    def test_editor_mask_follows_the_points_and_faces(self):
        lod = interleaved_lod()
        plan = plan_mod.make_plan(lod, 0)
        self.assertEqual(plan.warnings[-1].count("proxies are not the last"), 1)
        blob = plan.blobs[0]
        self.assertEqual(blob.name, "#Selected#")
        # File points A0, s1, B1 carry 1, 2, 3: they are now points 4, 1 and 8.
        self.assertEqual(list(blob.payload[:10]), [0, 2, 0, 0, 1, 0, 0, 0, 3, 0])
        # File faces A, B, f3 carry 4, 5, 6: the export order of the faces is f1, f3, A, B.
        self.assertEqual(list(blob.payload[10:]), [0, 6, 4, 5])

    def test_unmovable_blob_is_dropped_with_a_warning_and_the_tokens_follow(self):
        lod = interleaved_lod()
        lod.taggs.insert(0, p3d.RawTagg("#Odd#", b"abc"))
        lod.taggs.append(p3d.RawTagg("#Hide#", bytes(14)))
        plan = plan_mod.make_plan(lod, 0)
        self.assertEqual([blob.name for blob in plan.blobs], ["#Selected#", "#Hide#"])
        self.assertEqual([token for token in plan.order if token[:2] == "#?"], ["#?0", "#?1"])
        self.assertEqual(len(plan.order), len(plan.order_active))
        dropped = [w for w in plan.warnings if "#Odd#" in w]
        self.assertEqual(len(dropped), 1)
        self.assertIn(lod.name, dropped[0])

    def test_nothing_changes_without_interleaving(self):
        lod = p3d.LOD(LodType.VISUAL, 1.0)
        lod.set_mesh(SQUARE, [(0, 1, 2), (0, 2, 3)], [UP], [0] * 6)
        lod.taggs.append(p3d.RawTagg("#Odd#", b"abc"))
        plan = plan_mod.make_plan(lod, 0)
        self.assertEqual(plan.blobs[0].payload, b"abc")
        self.assertEqual(plan.warnings, [])


if __name__ == "__main__":
    unittest.main()

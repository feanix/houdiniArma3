"""The layout record a3ob_layout (task #37, D23, spec 19), pure Python part.

Purpose: the record puts Object Builder's file layout back into a LOD the exporter built in its own layout. These tests
build that "exporter layout" by hand (``content_lossless``) from a file-order LOD and check that ``apply_record`` brings
back the exact bytes, and that it leaves the LOD alone when the positions, the topology or the counts changed. The
Houdini round trip with the real importer and exporter is in test_real_file_layout.py.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import copy
import struct
import unittest
from array import array

from a3ob_core import p3d
from a3ob_core.p3d import LodType
from a3ob_houdini import layout_record
from a3ob_houdini.p3d_import import plan as plan_mod
from a3ob_houdini.permutation import remap_payload

from tests.test_permutation import interleaved_lod

UP = (0.0, 0.0, 1.0)
LEFT = (1.0, 0.0, 0.0)
SQUARE = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.5), (1.0, 1.0, 0.5), (0.0, 1.0, 0.5)]


def layout_lod():
    """Two triangles with every kind of layout: repeated normals, old values in the unused slots, sharp pairs in their
    own order (one degenerate), a unused normal entry, and a point with z = +0.0 and x = -0.0."""
    points = [(-0.0,) + SQUARE[0][1:]] + SQUARE[1:]
    lod = p3d.LOD(LodType.VISUAL, 1.0)
    lod.set_mesh(points, [(0, 1, 2), (0, 2, 3)], [UP, UP, LEFT, UP, (0.0, 1.0, 0.0)], [0, 1, 2, 3, 1, 0])
    lod.face_vertices[3] = 7
    lod.face_normals[7] = 3
    lod.face_uvs[6] = 0.25
    lod.set_sharp_edges([(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (2, 3)])
    return lod


def file_bytes(lod):
    return p3d.write_p3d(p3d.P3DModel([lod]))


def content_lossless(lod, perm):
    """The LOD as the exporter builds it from the import of ``lod``: Houdini order, first-appearance normal table,
    zero pads, sharp pairs with the degenerate pair last, and +0.0 where the unpack lost a -0.0 (x here)."""
    sides = list(lod.face_sides)
    _, vertices = lod.face_corner_indices()
    start = [sum(sides[:f]) for f in range(len(sides))]
    polygons = [[perm.point_new[v] for v in vertices[start[old]:start[old] + sides[old]]] for old in perm.face_old]
    normal_of = lod.face_corner_normal_indices()
    table, flat, indices = {}, [], []
    for corner in perm.corner_old:
        key = tuple(lod.normals[3 * normal_of[corner]:3 * normal_of[corner] + 3])
        if key not in table:
            table[key] = len(table)
            flat.append(key)
        indices.append(table[key])
    corner_uvs = lod.corner_uvs()
    uvs = []
    for corner in perm.corner_old:
        uvs.extend(corner_uvs[2 * corner:2 * corner + 2])
    points = [tuple(value + 0.0 for value in lod.point(old)) for old in perm.point_old]
    built = p3d.LOD()
    built.resolution_signature = lod.resolution_signature
    built.set_mesh(points, polygons, flat, indices, uvs, [lod.point_flags[o] for o in perm.point_old],
                   [lod.face_flags[o] for o in perm.face_old], [lod.face_textures[o] for o in perm.face_old],
                   [lod.face_materials[o] for o in perm.face_old])
    for tagg in lod.taggs:
        payload = remap_payload(tagg, perm)
        new = p3d.parse_tagg(tagg.name, tagg.active, payload, built.point_count, built.face_count, built.corner_count)
        if isinstance(new, p3d.SharpEdgesTagg):
            pairs = new.pairs()
            new = p3d.SharpEdgesTagg.from_pairs([p for p in pairs if p[0] != p[1]] + [p for p in pairs if p[0] == p[1]])
        built.taggs.append(new)
    return built


def record_for(lod):
    plan = plan_mod.make_plan(lod, 0)
    return plan, layout_record.build_record(lod, plan.permutation, plan.sharp)


class TestApplyRecord(unittest.TestCase):
    def test_every_kind_of_layout_comes_back_byte_for_byte(self):
        lod = layout_lod()
        plan, text = record_for(lod)
        built = content_lossless(lod, plan.permutation)
        self.assertNotEqual(file_bytes(built), file_bytes(lod))
        self.assertIsNone(layout_record.apply_record(built, text))
        self.assertEqual(file_bytes(built), file_bytes(lod))

    def test_interleaved_proxies_move_back_to_the_file_order(self):
        lod = interleaved_lod()
        plan, text = record_for(lod)
        self.assertFalse(plan.permutation.is_identity)
        built = content_lossless(lod, plan.permutation)
        self.assertNotEqual(file_bytes(built), file_bytes(lod))
        self.assertIsNone(layout_record.apply_record(built, text))
        self.assertEqual(file_bytes(built), file_bytes(lod))

    def test_a_moved_point_gives_a_reason_and_leaves_the_lod_alone(self):
        lod = layout_lod()
        plan, text = record_for(lod)
        built = content_lossless(lod, plan.permutation)
        built.set_point(1, (1.5, 0.0, 0.5))
        before = file_bytes(built)
        self.assertIn("positions", layout_record.apply_record(built, text))
        self.assertEqual(file_bytes(built), before)

    def test_a_deleted_face_gives_a_reason_and_leaves_the_lod_alone(self):
        lod = layout_lod()
        plan, text = record_for(lod)
        built = content_lossless(lod, plan.permutation)
        smaller = p3d.LOD()
        smaller.set_mesh(built.points, [tuple(built.face_vertices[:3])], list(built.normals), [0, 1, 2])
        before = file_bytes(smaller)
        self.assertIn("count", layout_record.apply_record(smaller, text))
        self.assertEqual(file_bytes(smaller), before)

    def test_a_different_topology_with_the_same_counts_is_caught_by_the_digest(self):
        lod = layout_lod()
        plan, text = record_for(lod)
        built = content_lossless(lod, plan.permutation)
        built.face_vertices[0], built.face_vertices[1] = built.face_vertices[1], built.face_vertices[0]
        self.assertIn("topology", layout_record.apply_record(built, text))

    def test_a_record_that_cannot_be_read_is_ignored(self):
        built = layout_lod()
        for text in ("", "not base64!", "AAAA", "eJwDAAAAAAE="):
            self.assertIn("cannot be read", layout_record.apply_record(built, text))

    def test_an_edited_normal_keeps_the_other_parts(self):
        """Two corners that shared a table entry now have different vectors: the table stays first-appearance, the
        pads, the sharp order and the signs of zero still come back."""
        lod = layout_lod()
        plan, text = record_for(lod)
        built = content_lossless(lod, plan.permutation)
        # The file shares table entry 1 between corners 1 and 4 (both UP). Corner 4 is now LEFT.
        self.assertEqual(built.normal_count, 2)
        built.face_normals[5] = 1
        self.assertIsNone(layout_record.apply_record(built, text))
        self.assertEqual(built.normal_count, 2)  # the first-appearance table of the built LOD stays
        self.assertEqual(built.sharp_edges(True), lod.sharp_edges(True))
        self.assertEqual(list(built.face_vertices[3::4]), list(lod.face_vertices[3::4]))
        self.assertEqual(built.points.tobytes(), lod.points.tobytes())

    def test_the_record_is_small(self):
        lod = layout_lod()
        _, text = record_for(lod)
        self.assertLess(len(text), 400)

    def test_a_short_face_gets_no_record(self):
        lod = layout_lod()
        lod.face_sides[0] = 2
        self.assertIsNone(layout_record.build_record(lod, plan_mod.make_plan(layout_lod(), 0).permutation))

    def test_a_record_for_a_lod_without_faces_still_fixes_signs_of_zero(self):
        lod = p3d.LOD(LodType.MEMORY, 0.0)
        lod.set_mesh([(0.0, 0.0, 0.0), (-0.0, 1.0, 2.0)], [])
        plan, text = record_for(lod)
        built = copy.deepcopy(lod)
        built.points[0] = -0.0
        built.points[3] = 0.0
        self.assertIsNone(layout_record.apply_record(built, text))
        self.assertEqual(built.points.tobytes(), lod.points.tobytes())


class TestRecordFormat(unittest.TestCase):
    def test_a_record_of_another_version_is_ignored(self):
        import base64
        import zlib
        _, text = record_for(layout_lod())
        body = bytearray(zlib.decompress(base64.b64decode(text)))
        body[4] = 2
        other = base64.b64encode(zlib.compress(bytes(body))).decode("ascii")
        self.assertIn("cannot be read", layout_record.apply_record(layout_lod(), other))

    def test_the_digest_ignores_the_sign_of_a_zero(self):
        a = array("f", [0.0, 1.0, -0.0])
        b = array("f", [-0.0, 1.0, 0.0])
        sides, vertices = array("I", [3]), array("I", [0, 1, 2])
        self.assertEqual(layout_record.digest_of(a, sides, vertices, (3, 1, 3)),
                         layout_record.digest_of(b, sides, vertices, (3, 1, 3)))
        c = array("f", [0.0, 1.5, 0.0])
        self.assertNotEqual(layout_record.digest_of(a, sides, vertices, (3, 1, 3)),
                            layout_record.digest_of(c, sides, vertices, (3, 1, 3)))


if __name__ == "__main__":
    unittest.main()

"""The weight transfer maths of ``a3ob_core.rig.transfer`` (design tests 2.1 and 2.2, task #65). Plain numpy; runs under
CPython (numpy 1.26) and hython (numpy 2.x). The test on the real Samples character skips when the Samples are absent.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import time
import unittest

import numpy as np

from a3ob_core.rig import (ORIGIN_COPIED, ORIGIN_FILLED, ORIGIN_NONRIGID, ORIGIN_RIGID, Settings, is_twist,
                           transfer_layers, twist_targets)
from a3ob_core.rig import transfer
from tests import samples_support

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"


def tube(rings, around, radius=0.1, height=2.0, y0=0.0, first_point=0):
    """A closed-around tube along +Y: points (rings x around), triangles with outward normals."""
    angle = np.linspace(0, 2 * np.pi, around, endpoint=False)
    ys = np.linspace(y0, y0 + height, rings)
    P = np.array([[radius * np.cos(a), y, radius * np.sin(a)] for y in ys for a in angle])
    tris = []
    for r in range(rings - 1):
        for a in range(around):
            a1 = (a + 1) % around
            p00, p01 = r * around + a, r * around + a1
            p10, p11 = (r + 1) * around + a, (r + 1) * around + a1
            tris += [(p00, p10, p11), (p00, p11, p01)]
    return P, np.array(tris, dtype=np.int64) + first_point


def two_bone_weights(P, low=0.8, high=1.2, width=0.4):
    """Bone 0 below ``low``, bone 1 above ``high`` (a straight blend in between); bone 2 is never used."""
    t = np.clip((P[:, 1] - low) / (high - low), 0.0, 1.0)
    return np.column_stack([1.0 - t, t, np.zeros(len(P))])


def with_cuff(P, tris, rings=4, around=12, y0=0.9, height=0.3, offset=0.03):
    """The tube plus a loose cuff piece around it. Returns (P, tris, cuff point mask)."""
    cuff_P, cuff_tris = tube(rings, around, 0.1 + offset, height, y0, first_point=len(P))
    mask = np.zeros(len(P) + len(cuff_P), dtype=bool)
    mask[len(P):] = True
    return np.concatenate([P, cuff_P]), np.concatenate([tris, cuff_tris]), mask


def run(src, dst_P, dst_tris, first, rigid, nonrigid, twist=None, override=None, **settings):
    src_P, src_tris, src_W = src
    return transfer_layers(src_P, src_tris, src_W, dst_P, dst_tris, first, rigid, nonrigid, twist, override,
                           Settings(**settings))


class TestFirstPass(unittest.TestCase):
    def setUp(self):
        self.P, self.tris = tube(20, 16)
        self.W = two_bone_weights(self.P)
        self.src = (self.P, self.tris, self.W)

    def test_same_mesh_copies_every_point(self):
        n = len(self.P)
        result = run(self.src, self.P, self.tris, np.ones(n, bool), np.zeros(n, bool), np.zeros(n, bool))
        np.testing.assert_allclose(result.weights, self.W, atol=1e-9)
        self.assertTrue((result.origin == ORIGIN_COPIED).all())
        self.assertEqual(result.info["matched"], n)

    def test_a_denser_target_gets_smooth_weights(self):
        P, tris = tube(41, 32)
        n = len(P)
        result = run(self.src, P, tris, np.ones(n, bool), np.zeros(n, bool), np.zeros(n, bool))
        self.assertLess(float(np.abs(result.weights - two_bone_weights(P)).sum(1).mean()), 0.02)
        np.testing.assert_allclose(result.weights.sum(1), 1.0, atol=1e-9)

    def test_rejected_points_are_inpainted_from_their_neighbours(self):
        # Move a band of the target far away from the source: nothing matches there, the fill must give the harmonic mean.
        P, tris = tube(20, 16)
        band = (P[:, 1] > 0.9) & (P[:, 1] < 1.1)
        P = P.copy()
        P[band, 0] *= 3.0
        P[band, 2] *= 3.0
        n = len(P)
        result = run(self.src, P, tris, np.ones(n, bool), np.zeros(n, bool), np.zeros(n, bool), distance=0.02)
        self.assertTrue((result.origin[band] == ORIGIN_FILLED).all())
        far = (P[:, 1] < 0.7) | (P[:, 1] > 1.3)
        self.assertTrue((result.origin[far] == ORIGIN_COPIED).all())
        filled = result.weights[band]
        np.testing.assert_allclose(filled.sum(1), 1.0, atol=1e-9)
        self.assertTrue((filled >= 0).all())
        order = np.argsort(P[band][:, 1])
        self.assertTrue((np.diff(filled[order][:, 1]) >= -1e-3).all())  # bone 1 grows with height

    def test_a_piece_with_no_match_takes_its_closest_weights(self):
        P, tris = tube(20, 16)
        extra, extra_tris = tube(3, 8, radius=0.1, height=0.2, y0=1.5, first_point=len(P))
        extra = extra + np.array([5.0, 0.0, 0.0])  # far from the source: no match at all
        both = np.concatenate([P, extra])
        n = len(both)
        result = run(self.src, both, np.concatenate([tris, extra_tris]), np.ones(n, bool), np.zeros(n, bool),
                     np.zeros(n, bool))
        self.assertEqual(result.info["fallback"], len(extra))
        self.assertTrue((result.weights[len(P):].sum(1) > 0.99).all())

    def test_points_outside_the_groups_are_untouched(self):
        n = len(self.P)
        first = self.P[:, 1] < 1.0
        result = run(self.src, self.P, self.tris, first, np.zeros(n, bool), np.zeros(n, bool))
        self.assertTrue((result.weights[~first] == 0).all())
        self.assertTrue((result.origin[~first] == 0).all())

    def test_needs_polygons_in_the_source(self):
        n = len(self.P)
        with self.assertRaises(ValueError):
            run((self.P, np.zeros((0, 3), dtype=np.int64), self.W), self.P, self.tris, np.ones(n, bool),
                np.zeros(n, bool), np.zeros(n, bool))

    def test_loose_source_triangles_are_not_skin(self):
        tri = np.array([[len(self.P), len(self.P) + 1, len(self.P) + 2]])
        P = np.concatenate([self.P, self.P[:3] * 0.0])  # a 3-point piece (a proxy triangle) at the origin
        W = np.concatenate([self.W, np.tile([0.0, 0.0, 1.0], (3, 1))])
        n = len(self.P)
        result = run((P, np.concatenate([self.tris, tri]), W), self.P, self.tris, np.ones(n, bool), np.zeros(n, bool),
                     np.zeros(n, bool))
        np.testing.assert_allclose(result.weights, self.W, atol=1e-9)


class TestLayers(unittest.TestCase):
    """Design test 2.2."""

    def setUp(self):
        self.P, self.tris = tube(24, 16)
        self.W = two_bone_weights(self.P)
        self.src = (self.P, self.tris, self.W)
        self.all_P, self.all_tris, self.cuff = with_cuff(self.P, self.tris)
        self.n = len(self.all_P)
        self.body = ~self.cuff
        self.none = np.zeros(self.n, bool)

    def test_rigid_dominant_gives_one_bone_to_every_point_of_the_piece(self):
        result = run(self.src, self.all_P, self.all_tris, self.body, self.cuff, self.none, rigid_mode="dominant")
        rows = result.weights[self.cuff]
        self.assertTrue((rows == rows[0]).all())
        self.assertEqual(sorted(rows[0].tolist()), [0.0, 0.0, 1.0])
        self.assertTrue((result.origin[self.cuff] == ORIGIN_RIGID).all())
        self.assertEqual(len(result.pieces), 1)
        self.assertEqual(result.pieces[0].layer, "rigid")
        self.assertEqual(result.pieces[0].points, int(self.cuff.sum()))
        self.assertTrue((result.piece[self.cuff] == 0).all())
        self.assertTrue((result.piece[self.body] == -1).all())

    def test_rigid_blend_gives_the_same_row_that_is_the_area_average(self):
        result = run(self.src, self.all_P, self.all_tris, self.body, self.cuff, self.none, rigid_mode="blend")
        rows = result.weights[self.cuff]
        self.assertTrue((np.abs(rows - rows[0]) < 1e-12).all())
        self.assertAlmostEqual(float(rows[0].sum()), 1.0)
        self.assertTrue(0.3 < rows[0][1] < 0.7)  # the cuff sits in the middle of the blend
        self.assertEqual(rows[0][2], 0.0)

    def test_first_pass_does_not_depend_on_the_other_groups(self):
        plain = run(self.src, self.all_P, self.all_tris, self.body, self.none, self.none)
        layered = run(self.src, self.all_P, self.all_tris, self.body, self.cuff, self.none)
        np.testing.assert_array_equal(plain.weights[self.body], layered.weights[self.body])

    def test_override_wins_and_is_counted(self):
        override = -np.ones(self.n, dtype=int)
        override[np.nonzero(self.cuff)[0][0]] = 2  # one point names bone 2
        result = run(self.src, self.all_P, self.all_tris, self.body, self.cuff, self.none, override=override,
                     rigid_mode="blend")
        self.assertTrue((result.weights[self.cuff] == [0.0, 0.0, 1.0]).all())
        self.assertEqual(result.pieces[0].how, "override")
        self.assertEqual(result.info["overrides"], 1)

    def test_twist_weight_goes_to_the_next_bone_down(self):
        # bones: 0 Arm, 1 ArmRoll, 2 ForeArm. The first pass is nearly all ArmRoll under the cuff.
        P, tris = self.P, self.tris
        W = np.zeros((len(P), 3))
        W[:, 1] = 0.7
        W[:, 0] = 0.1
        W[:, 2] = 0.2
        twist = twist_targets(["Arm", "ArmRoll", "ForeArm"], [-1, 0, 1])
        self.assertEqual(twist, [0, 2, 2])
        merged = run((P, tris, W), self.all_P, self.all_tris, self.body, self.cuff, self.none, twist=twist)
        self.assertEqual(merged.pieces[0].bone, 2)
        plain = run((P, tris, W), self.all_P, self.all_tris, self.body, self.cuff, self.none, twist=twist,
                    merge_twist=False)
        self.assertEqual(plain.pieces[0].bone, 1)

    def test_non_rigid_varies_along_the_piece_and_smoothing_reduces_the_jumps(self):
        rough = run(self.src, self.all_P, self.all_tris, self.body, self.none, self.cuff, smooth=0)
        smooth = run(self.src, self.all_P, self.all_tris, self.body, self.none, self.cuff, smooth=8)
        self.assertTrue((rough.origin[self.cuff] == ORIGIN_NONRIGID).all())

        def jumps(result):
            edges = self.all_tris[self.cuff[self.all_tris].all(axis=1)]
            a, b = np.concatenate([edges[:, 0], edges[:, 1]]), np.concatenate([edges[:, 1], edges[:, 2]])
            return float(np.abs(result.weights[a] - result.weights[b]).sum(1).mean())

        w = rough.weights[self.cuff]
        self.assertGreater(float(w[:, 1].max() - w[:, 1].min()), 0.3)  # it varies along the piece
        self.assertLess(jumps(smooth), jumps(rough))
        np.testing.assert_allclose(smooth.weights[self.cuff].sum(1), 1.0, atol=1e-9)
        np.testing.assert_array_equal(rough.weights[self.body], smooth.weights[self.body])  # first pass unchanged

    def test_small_pieces_are_ignored(self):
        result = run(self.src, self.all_P, self.all_tris, self.body, self.cuff, self.none, minpiece=100)
        self.assertEqual(result.info["ignored_pieces"], 1)
        self.assertTrue((result.weights[self.cuff] == 0).all())
        self.assertEqual(len(result.pieces), 0)

    def test_layers_without_a_first_pass_are_an_error(self):
        with self.assertRaises(ValueError):
            run(self.src, self.all_P, self.all_tris, self.none, self.cuff, self.none)


class TestTwist(unittest.TestCase):
    def test_names(self):
        self.assertTrue(is_twist("LeftArmRoll"))
        self.assertTrue(is_twist("leftforearmroll"))
        self.assertFalse(is_twist("LeftForeArm"))

    def test_chain_goes_through_two_twists_and_a_lone_twist_stays(self):
        names = ["a", "aRoll", "bRoll", "c", "dRoll"]
        parents = [-1, 0, 1, 2, -1]
        self.assertEqual(twist_targets(names, parents), [0, 3, 3, 3, 4])


@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestOnSamples(unittest.TestCase):
    """Design test 2.1: the spike's target A (same topology, no scale, 2 mm noise) on the Samples character."""

    @classmethod
    def setUpClass(cls):
        from a3ob_core.p3d.model import decode_weight
        from a3ob_core.p3d.reader import read_p3d_file
        from a3ob_core.rig import read_cfg_file, resolve_skeleton
        cfg = read_cfg_file(samples_support.path(SAMPLE_CFG))
        bones = {name.lower(): name for name, _ in resolve_skeleton(cfg, "OFP2_ManSkeleton").bones}
        model = read_p3d_file(samples_support.path(SAMPLE), strict=False)
        lod = model.lods[0]
        cls.P = np.array(lod.points, dtype=np.float64).reshape(-1, 3)
        P = cls.P
        P[:, 2] *= -1.0  # the importer's axis map; the maths does not care, but the geometry should look like Houdini's
        tris = []
        for face in lod.faces():
            v = face.vertices
            for k in range(1, len(v) - 1):
                tris.append((v[0], v[k], v[k + 1]))
        cls.tris = np.array(tris, dtype=np.int64)
        table = np.array([decode_weight(b) for b in range(256)])
        columns = [table[np.frombuffer(bytes(lod.selection(name).vertex_weights), dtype=np.uint8)]
                   for name in lod.selection_names() if name.lower() in bones]
        cls.W = np.stack(columns, axis=1)

    def test_same_topology_target_a_and_twelve_thousand_points(self):
        keep = np.zeros(len(self.P), bool)
        tris = transfer.drop_small_pieces(len(self.P), self.tris, 10)
        keep[np.unique(tris)] = True
        index = -np.ones(len(self.P), dtype=np.int64)
        index[keep] = np.arange(int(keep.sum()))
        P, W, tris = self.P[keep], self.W[keep], index[tris]
        rng = np.random.default_rng(1)
        target = P + rng.normal(0.0, 0.002, P.shape)
        n = len(P)
        start = time.perf_counter()
        result = run((P, tris, W), target, tris, np.ones(n, bool), np.zeros(n, bool), np.zeros(n, bool))
        elapsed = time.perf_counter() - start
        ok = W.sum(1) > 0.5
        l1 = np.abs(result.weights - W).sum(1)[ok]
        changed = (result.weights.argmax(1) != W.argmax(1))[ok]
        self.assertLessEqual(float(l1.mean()), 0.07)
        self.assertLessEqual(float(changed.mean()), 0.06)
        self.assertLess(elapsed, 10.0)
        print("   design test 2.1: %d points, L1 mean %.4f, heaviest bone changed %.2f%%, %.2f s, matched %d, filled %d"
              % (n, l1.mean(), 100 * changed.mean(), elapsed, result.info["matched"], result.info["filled"]))

    def test_twelve_thousand_points_under_ten_seconds(self):
        # The spike's target C: the body subdivided once (about 12.5k points).
        keep_tris = transfer.drop_small_pieces(len(self.P), self.tris, 10)
        used = np.unique(keep_tris)
        index = -np.ones(len(self.P), dtype=np.int64)
        index[used] = np.arange(len(used))
        P, W, tris = self.P[used], self.W[used], index[keep_tris]
        big_P, big_tris = subdivide(P, tris)
        n = len(big_P)
        self.assertGreater(n, 11000)
        start = time.perf_counter()
        result = run((P, tris, W), big_P, big_tris, np.ones(n, bool), np.zeros(n, bool), np.zeros(n, bool))
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 10.0)
        self.assertEqual(result.info["matched"] + result.info["filled"] + result.info["fallback"], n)
        print("   design test 2.1 timing: %d points in %.2f s (matched %d, CG %d iterations)"
              % (n, elapsed, result.info["matched"], result.info["iterations"]))


def subdivide(P, tris):
    """One level of midpoint subdivision (every triangle into four)."""
    edge_id, new_P, count, out = {}, [P], len(P), []
    for t in tris:
        mids = []
        for k in range(3):
            a, b = int(t[k]), int(t[(k + 1) % 3])
            key = (a, b) if a < b else (b, a)
            if key not in edge_id:
                edge_id[key] = count
                count += 1
                new_P.append(((P[a] + P[b]) * 0.5)[None, :])
            mids.append(edge_id[key])
        a, b, c = (int(x) for x in t)
        ab, bc, ca = mids
        out += [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
    return np.concatenate(new_P), np.array(out, dtype=np.int64)


if __name__ == "__main__":
    unittest.main()

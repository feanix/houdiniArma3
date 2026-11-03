"""Tests for LOD types and signature encode/decode (a3ob_core.p3d.lod_types).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import math
import unittest

from a3ob_core.p3d import LOD_TYPES, LodType, decode_signature, encode_signature, f32, format_lod_name
from a3ob_core.p3d.lod_types import has_resolution

# A3OB io/data_p3d.py:294-330, the fixed signatures it matched as "%.3e" strings.
A3OB_SIGNATURE_MAP = {
    "1.000e+03": LodType.VIEW_GUNNER,
    "1.100e+03": LodType.VIEW_PILOT,
    "1.300e+04": LodType.GROUNDLAYER,
    "1.000e+13": LodType.GEOMETRY,
    "2.000e+13": LodType.GEOMETRY_BUOY,
    "3.000e+13": LodType.UNDERGROUND,
    "4.000e+13": LodType.GEOMETRY_PHYSX,
    "5.000e+13": LodType.NAVIGATION,
    "1.000e+15": LodType.MEMORY,
    "2.000e+15": LodType.LANDCONTACT,
    "3.000e+15": LodType.ROADWAY,
    "4.000e+15": LodType.PATHS,
    "5.000e+15": LodType.HITPOINTS,
    "6.000e+15": LodType.VIEW_GEOMETRY,
    "7.000e+15": LodType.FIRE_GEOMETRY,
    "9.000e+15": LodType.VIEW_CARGO_FIRE_GEOMETRY,
    "1.000e+16": LodType.VIEW_COMMANDER,
    "1.100e+16": LodType.VIEW_COMMANDER_GEOMETRY,
    "1.200e+16": LodType.VIEW_COMMANDER_FIRE_GEOMETRY,
    "1.300e+16": LodType.VIEW_PILOT_GEOMETRY,
    "1.400e+16": LodType.VIEW_PILOT_FIRE_GEOMETRY,
    "1.500e+16": LodType.VIEW_GUNNER_GEOMETRY,
    "1.600e+16": LodType.VIEW_GUNNER_FIRE_GEOMETRY,
    "1.700e+16": LodType.SUBPARTS,
    "1.900e+16": LodType.SHADOW_VIEW_PILOT,
    "2.000e+16": LodType.SHADOW_VIEW_GUNNER,
    "2.100e+16": LodType.WRECKAGE,
}


class TestTable(unittest.TestCase):
    def test_one_entry_per_type(self):
        self.assertEqual(len(LodType), 34)  # 33 types + Unknown
        self.assertEqual(set(LOD_TYPES), set(LodType))
        self.assertEqual(sorted(t.value for t in LodType), list(range(-1, 33)))

    def test_resolution_types(self):
        with_resolution = {t for t in LodType if has_resolution(t)}
        self.assertEqual(with_resolution, {
            LodType.VISUAL, LodType.VIEW_CARGO, LodType.SHADOW, LodType.EDIT, LodType.VIEW_CARGO_GEOMETRY,
            LodType.SHADOW_VIEW_CARGO, LodType.UNKNOWN})


class TestDecode(unittest.TestCase):
    def test_a3ob_fixed_signatures(self):
        for text, lod_type in A3OB_SIGNATURE_MAP.items():
            with self.subTest(signature=text):
                self.assertEqual(decode_signature(f32(float(text))), (lod_type, 0.0))
                self.assertEqual(encode_signature(lod_type), f32(float(text)))

    def test_fixture_signatures(self):
        # Raw float32 values read from the fixtures.
        self.assertEqual(decode_signature(9999999827968.0), (LodType.GEOMETRY, 0.0))
        self.assertEqual(decode_signature(999999986991104.0), (LodType.MEMORY, 0.0))
        self.assertEqual(decode_signature(3000000028082176.0), (LodType.ROADWAY, 0.0))
        self.assertEqual(decode_signature(19999999655936.0), (LodType.GEOMETRY_BUOY, 0.0))
        self.assertEqual(decode_signature(1100.0), (LodType.VIEW_PILOT, 0.0))
        self.assertEqual(decode_signature(10000.0), (LodType.SHADOW, 0.0))

    def test_float_resolutions_are_kept(self):
        # A3OB rounded these to int.
        self.assertEqual(decode_signature(f32(1.5)), (LodType.VISUAL, 1.5))
        self.assertEqual(decode_signature(f32(0.3)), (LodType.VISUAL, 0.3))
        self.assertEqual(decode_signature(f32(1200.25)), (LodType.VIEW_CARGO, 0.25))
        self.assertEqual(decode_signature(f32(10000.5)), (LodType.SHADOW, 0.5))
        self.assertEqual(decode_signature(f32(20003.0)), (LodType.EDIT, 3.0))
        self.assertEqual(decode_signature(f32(123456.5)), (LodType.UNKNOWN, 123456.5))

    def test_small_resolutions_are_shortened(self):
        # Review minor: rounding to decimal places stopped at 11 places and left small values unshortened.
        self.assertEqual(decode_signature(f32(1.234e-9)), (LodType.VISUAL, 1.234e-09))
        self.assertEqual(decode_signature(f32(1.234e-5)), (LodType.VISUAL, 1.234e-05))
        self.assertEqual(decode_signature(f32(3.0e-30)), (LodType.VISUAL, 3e-30))
        self.assertEqual(decode_signature(f32(-2.5e-7)), (LodType.UNKNOWN, -2.5e-07))
        self.assertEqual(format_lod_name(*decode_signature(f32(1.234e-9))), "Resolution 1.234e-09")
        for value in (1.234e-9, 7.77e-21, 0.1, 123.456, 999.5, 0.3333333):
            with self.subTest(value=value):
                signature = f32(value)
                lod_type, resolution = decode_signature(signature)
                self.assertEqual(encode_signature(lod_type, resolution), signature)
                self.assertLessEqual(len(repr(resolution)), len(repr(value)))

    def test_cargo_geometry_resolutions(self):
        for lod_type in (LodType.VIEW_CARGO_GEOMETRY, LodType.SHADOW_VIEW_CARGO):
            for resolution in (0.0, 1.0, 5.0, 12.0, 37.0, 99.0):
                with self.subTest(lod_type=lod_type.name, resolution=resolution):
                    signature = encode_signature(lod_type, resolution)
                    self.assertEqual(decode_signature(signature), (lod_type, resolution))

    def test_round_trip_every_type(self):
        samples = {LodType.VISUAL: 3.0, LodType.VIEW_CARGO: 2.0, LodType.SHADOW: 1000.0, LodType.EDIT: 7.5,
                   LodType.VIEW_CARGO_GEOMETRY: 2.0, LodType.SHADOW_VIEW_CARGO: 3.0, LodType.UNKNOWN: -12.5}
        for lod_type in LodType:
            resolution = samples.get(lod_type, 0.0)
            with self.subTest(lod_type=lod_type.name):
                signature = encode_signature(lod_type, resolution)
                self.assertEqual(signature, f32(signature))
                self.assertEqual(decode_signature(signature), (lod_type, resolution))

    def test_unknown_and_special_values(self):
        self.assertEqual(decode_signature(-1.0), (LodType.UNKNOWN, -1.0))  # A3OB: Visual
        self.assertEqual(decode_signature(1050.0), (LodType.UNKNOWN, 1050.0))
        self.assertEqual(decode_signature(5.5e15)[0], LodType.UNKNOWN)
        lod_type, resolution = decode_signature(float("nan"))
        self.assertEqual(lod_type, LodType.UNKNOWN)
        self.assertTrue(math.isnan(resolution))
        self.assertEqual(decode_signature(float("inf"))[0], LodType.UNKNOWN)

    def test_shadow_buffer_is_shadow_1000(self):
        # 1.1e4 (ShadowBuffer) is commented out in A3OB and falls in the Shadow Volume range.
        self.assertEqual(decode_signature(11000.0), (LodType.SHADOW, 1000.0))


class TestEncode(unittest.TestCase):
    def test_errors(self):
        with self.assertRaises(ValueError):
            encode_signature(LodType.GEOMETRY, 1.0)   # no resolution
        with self.assertRaises(ValueError):
            encode_signature(LodType.VISUAL, 1000.0)  # that is View - Gunner
        with self.assertRaises(ValueError):
            encode_signature(LodType.VISUAL, -1.0)
        with self.assertRaises(ValueError):
            encode_signature(LodType.UNKNOWN, 1e13)   # that is Geometry
        with self.assertRaises(ValueError):
            encode_signature(LodType.UNKNOWN, 1e300)  # not a float32

    def test_f32(self):
        self.assertEqual(f32(1e13), 9999999827968.0)
        with self.assertRaises(ValueError):
            f32(1e39)

    def test_names(self):
        self.assertEqual(format_lod_name(LodType.VISUAL, 1.0), "Resolution 1")
        self.assertEqual(format_lod_name(LodType.VISUAL, 1.5), "Resolution 1.5")
        self.assertEqual(format_lod_name(LodType.GEOMETRY), "Geometry")
        self.assertEqual(format_lod_name(LodType.SHADOW, 0.0), "Shadow Volume 0")
        self.assertEqual(format_lod_name(LodType.UNKNOWN, 123.5), "Unknown 123.5")


if __name__ == "__main__":
    unittest.main()

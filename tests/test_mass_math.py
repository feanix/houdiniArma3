"""The arithmetic of the Mass node and the parameter table of the Mass and Proxy nodes. Plain Python, no ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import unittest

from a3ob_houdini import hda_source, mass_math, nodes
from a3ob_houdini.nodes import proxy
from tests.hou_support import REPO_ROOT

CUBE_FACES = ((4, 5, 7, 6), (0, 2, 3, 1), (1, 3, 7, 5), (0, 4, 6, 2), (2, 6, 7, 3), (0, 1, 5, 4))


def cube_points(side=1.0, offset=(0.0, 0.0, 0.0)):
    return [(offset[0] + side * (n & 1), offset[1] + side * ((n >> 1) & 1), offset[2] + side * ((n >> 2) & 1))
            for n in range(8)]


class TestComponents(unittest.TestCase):
    def test_a_cube_is_one_closed_component_with_its_volume(self):
        found = mass_math.closed_components(CUBE_FACES)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].closed)
        self.assertEqual(found[0].points, tuple(range(8)))
        self.assertAlmostEqual(mass_math.component_volume(cube_points(2.0), CUBE_FACES, found[0].polygons), 8.0)

    def test_a_cube_away_from_the_origin_has_the_same_volume(self):
        volume = mass_math.component_volume(cube_points(2.0, (5, -3, 7)), CUBE_FACES, range(6))
        self.assertAlmostEqual(volume, 8.0)

    def test_an_open_shape_and_a_flat_quad_are_not_closed(self):
        found = mass_math.closed_components(CUBE_FACES[:5] + ((20, 21, 22, 23),))
        self.assertEqual([component.closed for component in found], [False, False])

    def test_two_cubes_are_two_components(self):
        second = tuple(tuple(point + 8 for point in face) for face in CUBE_FACES)
        found = mass_math.closed_components(CUBE_FACES + second)
        self.assertEqual([component.closed for component in found], [True, True])
        self.assertEqual(found[1].points, tuple(range(8, 16)))

    def test_a_face_with_a_flipped_neighbour_is_not_closed(self):
        flipped = list(CUBE_FACES)
        flipped[0] = tuple(reversed(flipped[0]))
        self.assertFalse(mass_math.closed_components(flipped)[0].closed)


class TestRampAndStats(unittest.TestCase):
    def test_scale_keeps_zero_and_maps_the_rest_to_001_and_1(self):
        self.assertEqual(mass_math.scale_factors([0, 2, 6]), [0, 0.001, 1.0])
        self.assertEqual(mass_math.scale_factors([0, 3, 3]), [0, 1.0, 1.0])
        self.assertEqual(mass_math.scale_factors([0, 0]), [0, 0])

    def test_ramp_runs_black_blue_to_red(self):
        colors = mass_math.ramp_colors([0, 1, 3, 5])
        self.assertEqual(colors[0], (0, 0, 0))
        self.assertEqual(colors[1], (0, 0, 1))
        self.assertEqual(colors[3], (1, 0, 0))
        self.assertAlmostEqual(colors[2][0], 0.002, places=3)  # half way is green, just turning yellow
        self.assertEqual(colors[2][1], 1.0)

    def test_visualizer_ramp_gives_the_same_colours(self):
        # #32: ramp_stops + ramp_range, read the way a node Color visualizer does (linear, clamped at both ends)
        def visualizer_color(value, masses):
            low, high = mass_math.ramp_range(masses)
            position = min(1.0, max(0.0, (value - low) / (high - low)))
            stops = mass_math.ramp_stops()
            for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
                if position <= p1:
                    rate = (position - p0) / (p1 - p0)
                    return tuple(c0[k] + (c1[k] - c0[k]) * rate for k in range(3))
            return stops[-1][1]

        for masses in ([0, 1, 3, 5, 2, 0], [0, 4, 4], [2.5, 7.5, 100.0, 0], [0.5, 100.0, 0]):
            for got, want in zip((visualizer_color(m, masses) for m in masses), mass_math.ramp_colors(masses)):
                for a, b in zip(got, want):
                    self.assertAlmostEqual(a, b, places=2, msg=str(masses))
        self.assertEqual(mass_math.ramp_range([0, 0]), (0.0, 1.0))

    def test_stats_and_centre_ignore_zero_mass(self):
        positions = [(0, 0, 0), (2, 0, 0), (9, 9, 9)]
        masses = [1.0, 3.0, 0.0]
        result = mass_math.stats(masses)
        self.assertEqual((result.total, result.minimum, result.average, result.maximum, result.count),
                         (4.0, 1.0, 2.0, 3.0, 2))
        self.assertEqual(mass_math.centre_of_mass(positions, masses), (1.5, 0.0, 0.0))
        self.assertIsNone(mass_math.centre_of_mass(positions, [0, 0, 0]))


class TestParameterTables(unittest.TestCase):
    def test_both_nodes_are_found_and_their_hda_text_is_current(self):
        for kind in ("mass", "proxy"):
            self.assertIn(kind, nodes.KINDS)
        otls = os.path.join(REPO_ROOT, "houdini", "otls")
        for kind in ("mass", "proxy"):
            self.assertEqual(hda_source.differences(nodes.load(kind).SPEC, otls), [], kind)

    def test_mass_and_material_assign_have_a_guarded_python_onloaded(self):
        for kind in ("mass", "material_assign"):
            spec = nodes.load(kind).SPEC
            files = hda_source.render(spec)
            section = hda_source.section_name(spec)
            script = files[section + "/OnLoaded"]
            self.assertIn("except ImportError", script)  # without the package the node loads quietly
            self.assertIn("e.loaded(%r, kwargs)" % kind, script)
            self.assertEqual(json.loads(files[section + "/ExtraFileOptions"]),
                             {"OnLoaded/IsPython": {"type": "bool", "value": True}})
            self.assertIn("OnLoaded\tOnLoaded", files[section + "/Sections.list"])
        spec = nodes.load("mass").SPEC
        dialog = hda_source.render(spec)[hda_source.section_name(spec) + "/DialogScript"]
        self.assertIn("e.button('mass', 'visualize', kwargs)", dialog)  # the Visualize toggle runs the callback

    def test_both_events_share_one_extra_file_options(self):
        text = hda_source._extra_file_options(["OnCreated", "OnLoaded"])
        self.assertEqual(sorted(json.loads(text)), ["OnCreated/IsPython", "OnLoaded/IsPython"])

    def test_mass_modes_and_proxy_defaults(self):
        mass = nodes.load("mass").SPEC
        self.assertEqual([token for token, _ in mass.parm_index["mode"].items],
                         ["each", "total", "density", "clear", "copyfrom", "copyto"])
        self.assertEqual(mass.parm_index["mode"].default, "total")
        spec = nodes.load("proxy").SPEC
        self.assertEqual([parm.name for parm in spec.parms],
                         ["path", "common", "index", "autoindex", "t", "r", "useorient"])
        self.assertEqual(spec.parm_index["index"].default, 1)
        self.assertEqual(spec.inputs, (1, 2))

    def test_path_is_normalised(self):
        self.assertEqual(proxy.normalise_path(" a3/data_f/top.p3d "), "a3\\data_f\\top")
        self.assertEqual(proxy.normalise_path("\\a3\\data_f\\top.P3D"), "\\a3\\data_f\\top")
        self.assertEqual(proxy.normalise_path("\\a3\\p3d_stuff\\top"), "\\a3\\p3d_stuff\\top")
        self.assertEqual(proxy.normalise_path("x.p3d.p3d"), "x.p3d")

    def test_common_proxies_have_no_drive_and_no_ending(self):
        for _, _, path in proxy.COMMON:
            self.assertTrue(path.startswith("\\a3\\"), path)
            self.assertFalse(path.lower().endswith(".p3d"), path)


if __name__ == "__main__":
    unittest.main()

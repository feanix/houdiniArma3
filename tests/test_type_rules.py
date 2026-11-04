"""Tier-2 per-LOD-type rules, flag values and degenerate proxy triangles (spec 17.1 and 17.1.1). They work on the plain
data of the exporter, so these tests run under plain Python. Each rule has a failing example and its switch silences it.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import unittest

from a3ob_core.p3d.lod_types import LodType
from a3ob_houdini import messages
from a3ob_houdini.nodes import _export_common as common
from a3ob_houdini.p3d_export import ExportOptions, build
from a3ob_houdini.p3d_export import flag_check, proxy_rebuild, type_rules
from a3ob_houdini.p3d_export.data import Collection, Details, GroupData, LodData, MeshData, Poly, ProxyData
from a3ob_houdini.p3d_export.normals import face_normal

SHADOW_TYPES_FOR_TEST = (LodType.SHADOW, LodType.SHADOW_VIEW_CARGO, LodType.SHADOW_VIEW_PILOT, LodType.SHADOW_VIEW_GUNNER)
CUBE_POINTS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
#: Every face runs counter-clockwise seen from outside, so the shared edges run in opposite directions.
CUBE_FACES = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (3, 7, 6, 2), (0, 4, 7, 3), (1, 2, 6, 5)]
#: The cube with its top face pushed in to a point (a dent): the edges around the dent are concave.
DENT_POINTS = CUBE_POINTS + [(0.5, 0.5, 0.5)]
DENT_FACES = [(0, 3, 2, 1), (0, 1, 5, 4), (3, 7, 6, 2), (0, 4, 7, 3), (1, 2, 6, 5),
              (4, 5, 8), (5, 6, 8), (6, 7, 8), (7, 4, 8)]


def flat_poly(number, indices, points, **kwargs):
    """A polygon with vertex normals equal to its face normal (a flat polygon)."""
    x, y, z = face_normal([points[i] for i in indices])
    length = (x * x + y * y + z * z) ** 0.5
    if length == 0.0:
        return Poly(number, tuple(indices), **kwargs)
    normal = (x / length, y / length, z / length)
    return Poly(number, tuple(indices), [normal] * len(indices), **kwargs)


def lod(lod_type, faces=CUBE_FACES, points=CUBE_POINTS, resolution=0.0, flat=True, **details):
    polys = [flat_poly(n, f, points) if flat else Poly(n, tuple(f)) for n, f in enumerate(faces)]
    mesh = MeshData(points=[tuple(map(float, p)) for p in points], polys=polys)
    data = Details(lod_type=int(lod_type), resolution=resolution)
    for key, value in details.items():
        setattr(data, key, value)
    return LodData(0, mesh, data)


def run(data, rules_off=(), **options):
    """The warnings. All rules are on unless named: the default-off ones (D24) are tested on their own."""
    report = messages.Report()
    type_rules.check_lod_rules(data, ExportOptions(rules_off=tuple(rules_off), **options), report)
    return report.texts(messages.WARNING)


def run_notes(data, rules_off=(), **options):
    """The info notes (T2-MASS since D24)."""
    report = messages.Report()
    type_rules.check_lod_rules(data, ExportOptions(rules_off=tuple(rules_off), **options), report)
    return report.texts(messages.INFO)


def run_all(data, rules_off=()):
    return run(data, rules_off) + run_notes(data, rules_off)


#: A quad with a reflex corner at the third point (not convex), and a convex quad.
DENT_QUAD_POINTS = [(0, 0, 0), (2, 0, 0), (0.5, 0, 0.5), (0, 0, 2)]
FLAT_QUAD_POINTS = [(0, 0, 0), (2, 0, 0), (2, 0, 2), (0, 0, 2)]


def with_rule(texts, rule_id):
    return [text for text in texts if "(rule %s)" % rule_id in text]


def group(name, members):
    return GroupData(name, name, list(members))


def failing_examples():
    """``{rule id: LodData that fails exactly that rule}``."""
    shadow = lod(LodType.SHADOW, resolution=1000.0)
    examples = {}
    uv = lod(LodType.VISUAL, resolution=1.0)
    uv.mesh.uv_names = ["uv", "uv2", "uv3"]
    examples["T2-UV2"] = uv
    flipped = lod(LodType.GEOMETRY)
    flipped.mesh.polys[1] = flat_poly(1, CUBE_FACES[1][::-1], CUBE_POINTS)
    examples["T2-CONTIG"] = flipped
    examples["T2-TRI"] = lod(LodType.GEOMETRY, faces=[(0, 1, 2, 3)], points=DENT_QUAD_POINTS)
    examples["T2-CONVEX"] = lod(LodType.GEOMETRY, DENT_FACES, DENT_POINTS)
    examples["T2-COMP"] = lod(LodType.GEOMETRY)
    examples["T2-MASS"] = lod(LodType.GEOMETRY)
    massless = lod(LodType.GEOMETRY)
    massless.mesh.masses = [1.0] * 7 + [0.0]
    examples["T2-MASSALL"] = massless
    textured = lod(LodType.UNDERGROUND)
    textured.mesh.polys[2].texture = "a.paa"
    examples["T2-NOMAT"] = textured
    examples["T2-SHADOWRES"] = lod(LodType.SHADOW, resolution=7.0)
    examples["T2-SHADOWOFF"] = lod(LodType.SHADOW, resolution=1000.0, prop_names=[" LODNoShadow"], prop_values=["1 "])
    edge = lod(LodType.MEMORY, faces=[(0, 1)])
    examples["T2-PC-EDGE"] = edge
    examples["T2-PC-FACE"] = lod(LodType.MEMORY)
    examples["T2-RW-FACES"] = lod(LodType.ROADWAY, faces=[])
    many = lod(LodType.ROADWAY, faces=[(0, 1, 2)])
    many.mesh.points = [(float(i), 0.0, 0.0) for i in range(256)]
    many.mesh.polys[0].texture = "road.paa"
    examples["T2-RW-LIMIT"] = many
    examples["T2-RW-SOUND"] = lod(LodType.ROADWAY)
    layer = lod(LodType.GROUNDLAYER)
    layer.mesh.uv_names = ["uv", "uv2"]
    examples["T2-GL-UV"] = layer
    values = lod(LodType.GROUNDLAYER, faces=[(0, 1, 2)])
    values.mesh.uv_names = ["uv"]
    values.mesh.polys[0].uvs["uv"] = [(0.0, 0.0), (1.0, 0.0), (0.5, 0.5)]
    examples["T2-GL-UVVALUES"] = values
    examples["T2-PATH-ENTRY"] = lod(LodType.PATHS)
    examples["T2-PATH-POS"] = lod(LodType.PATHS)
    return examples


def passing_examples():
    """Data that passes every rule of its type, so the failing examples above fail for their own rule only."""
    complete = lod(LodType.GEOMETRY)
    complete.mesh.masses = [1.0] * 8
    complete.mesh.groups = [group("Component01", range(8))]
    return complete


class TestRules(unittest.TestCase):
    def test_every_rule_has_a_failing_example_and_a_switch(self):
        examples = failing_examples()
        self.assertEqual(sorted(examples), sorted(type_rules.RULE_IDS))
        self.assertEqual(len(set(type_rules.RULE_IDS)), 19)
        for rule_id, data in examples.items():
            with self.subTest(rule=rule_id):
                texts = run_all(data)
                self.assertTrue(with_rule(texts, rule_id), texts)
                self.assertFalse(with_rule(run_all(data, rules_off=[rule_id]), rule_id))
                others = [text for text in with_rule(texts, rule_id)]
                self.assertEqual(len(others), 1)  # once per LOD
                self.assertTrue(texts[0].startswith(data.label + ": "))

    def test_a_rule_applies_to_its_types_only(self):
        data = lod(LodType.VISUAL, resolution=1.0)  # not triangulated, no mass, no components: all fine for a visual LOD
        self.assertEqual(run(data), [])
        self.assertTrue(with_rule(run_notes(lod(LodType.GEOMETRY)), "T2-MASS"))
        for name in ("GEOMETRY_BUOY", "GEOMETRY_PHYSX"):  # D16: mass is checked on the Geometry LOD only
            texts = run_all(lod(getattr(LodType, name)))
            self.assertEqual(with_rule(texts, "T2-MASS"), [])
            self.assertEqual(with_rule(texts, "T2-MASSALL"), [])
        self.assertEqual(run(lod(LodType.FIRE_GEOMETRY)), [])  # not ported (spec 17.1.1 notes)
        self.assertEqual(run(lod(LodType.VIEW_GEOMETRY)), [])

    def test_a_clean_geometry_lod_with_convex_quads_has_no_message(self):
        # D24: the cube's six faces are convex quads, which Geometry LODs may hold.
        self.assertEqual(run_all(passing_examples()), [])

    def test_tri_allows_convex_quads_in_geometry_and_shadow_lods(self):
        for lod_type in (LodType.GEOMETRY, LodType.GEOMETRY_BUOY, LodType.GEOMETRY_PHYSX, LodType.SHADOW):
            convex = lod(lod_type, faces=[(0, 1, 2, 3)], points=FLAT_QUAD_POINTS, resolution=1000.0)
            self.assertEqual(with_rule(run(convex), "T2-TRI"), [], lod_type)
            dent = lod(lod_type, faces=[(0, 1, 2, 3)], points=DENT_QUAD_POINTS, resolution=1000.0)
            texts = with_rule(run(dent), "T2-TRI")
            self.assertEqual(len(texts), 1, lod_type)
            self.assertIn("a quad that is not convex", texts[0])

    def test_tri_warns_for_five_sided_polygons_and_any_quad_in_underground(self):
        points = [(0, 0, 0), (2, 0, 0), (3, 0, 1), (1, 0, 3), (-1, 0, 1)]
        five = lod(LodType.GEOMETRY, faces=[(0, 1, 2, 3, 4)], points=points)
        texts = with_rule(run(five), "T2-TRI")
        self.assertEqual(len(texts), 1)
        self.assertIn("a polygon with 5 points", texts[0])
        underground = lod(LodType.UNDERGROUND, faces=[(0, 1, 2, 3)], points=FLAT_QUAD_POINTS)
        self.assertEqual(len(with_rule(run(underground), "T2-TRI")), 1)
        triangle = lod(LodType.UNDERGROUND, faces=[(0, 1, 2)], points=FLAT_QUAD_POINTS)
        self.assertEqual(with_rule(run(triangle), "T2-TRI"), [])

    def test_a_bow_tie_quad_is_not_convex(self):
        bow = lod(LodType.GEOMETRY, faces=[(0, 1, 3, 2)], points=FLAT_QUAD_POINTS)
        self.assertEqual(len(with_rule(run(bow), "T2-TRI")), 1)

    def test_mass_is_a_note_not_a_warning(self):
        data = lod(LodType.GEOMETRY)
        self.assertEqual(with_rule(run(data), "T2-MASS"), [])
        notes = with_rule(run_notes(data), "T2-MASS")
        self.assertEqual(len(notes), 1)
        self.assertIn("has no mass. Fine for items such as vests or scopes; physical objects need mass (rule T2-MASS)".lower(), notes[0].lower())
        self.assertIn("Fine for items such as vests or scopes; physical objects need mass", notes[0])
        data.mesh.masses = [1.0] * 8
        self.assertEqual(with_rule(run_notes(data), "T2-MASS"), [])
        data.mesh.masses = [0.0] * 8
        self.assertEqual(len(with_rule(run_notes(data), "T2-MASS")), 1)
        # T2-MASSALL stays a warning.
        data.mesh.masses = [1.0] * 7 + [0.0]
        self.assertEqual(len(with_rule(run(data), "T2-MASSALL")), 1)

    def test_nomat_allows_shadow_volumes_but_not_underground(self):
        for lod_type in SHADOW_TYPES_FOR_TEST:
            data = lod(lod_type, resolution=1000.0)
            data.mesh.polys[0].texture = "leaf.paa"
            data.mesh.polys[1].material = "leaf.rvmat"
            self.assertEqual(with_rule(run(data), "T2-NOMAT"), [], lod_type)
        clean = lod(LodType.UNDERGROUND)
        self.assertEqual(with_rule(run(clean), "T2-NOMAT"), [])
        clean.mesh.polys[0].material = "x.rvmat"
        self.assertEqual(len(with_rule(run(clean), "T2-NOMAT")), 1)

    def test_shadow_resolution_and_shadow_off_are_off_by_default(self):
        self.assertEqual(type_rules.rule_by_id("T2-SHADOWRES").default_on, False)
        self.assertEqual(type_rules.rule_by_id("T2-SHADOWOFF").default_on, False)
        self.assertEqual(ExportOptions().rules_off, ("T2-SHADOWOFF", "T2-SHADOWRES"))
        data = lod(LodType.SHADOW, resolution=7.0, prop_names=["LODNoShadow"], prop_values=["1"])
        report = messages.Report()
        type_rules.check_lod_rules(data, ExportOptions(), report)
        self.assertEqual(report.texts(messages.WARNING), [])
        # Switched on (a node switch or a preset) they still work.
        self.assertEqual(len(with_rule(run(data), "T2-SHADOWRES")), 1)
        self.assertEqual(len(with_rule(run(data), "T2-SHADOWOFF")), 1)

    def test_unchanged_rules_still_warn(self):
        for rule_id in ("T2-MASSALL", "T2-CONTIG", "T2-CONVEX", "T2-RW-LIMIT"):
            with self.subTest(rule=rule_id):
                self.assertEqual(len(with_rule(run(failing_examples()[rule_id]), rule_id)), 1)

    def test_convex_cube_in_either_winding_and_dent(self):
        for faces in (CUBE_FACES, [f[::-1] for f in CUBE_FACES]):
            self.assertEqual(with_rule(run(lod(LodType.GEOMETRY, faces)), "T2-CONVEX"), [])
            self.assertEqual(with_rule(run(lod(LodType.GEOMETRY, faces)), "T2-CONTIG"), [])
        dent = with_rule(run(lod(LodType.GEOMETRY, DENT_FACES, DENT_POINTS)), "T2-CONVEX")
        self.assertEqual(len(dent), 1)
        self.assertIn("polygons", dent[0])

    def test_flat_neighbours_are_convex(self):
        flat = lod(LodType.GEOMETRY, faces=[(0, 1, 2), (0, 2, 3)], points=CUBE_POINTS[:4])
        self.assertEqual(with_rule(run(flat), "T2-CONVEX"), [])

    def test_occluder_group_skips_contiguity_and_components(self):
        data = failing_examples()["T2-CONTIG"]
        data.mesh.groups = [group("occluder01", range(8))]
        texts = run(data)
        self.assertEqual(with_rule(texts, "T2-CONTIG"), [])
        self.assertEqual(with_rule(texts, "T2-COMP"), [])

    def test_group_names_match_at_the_start_ignoring_case(self):
        data = lod(LodType.GEOMETRY)
        data.mesh.groups = [group("COMPONENT7_left", [0])]
        self.assertEqual(with_rule(run(data), "T2-COMP"), [])
        data.mesh.groups = [group("my_component1", [0])]
        self.assertEqual(len(with_rule(run(data), "T2-COMP")), 1)
        data.mesh.points = []
        data.mesh.polys = []
        data.mesh.groups = [group("component1", [])]
        self.assertEqual(len(with_rule(run(data), "T2-COMP")), 1)  # A3OB: no points, no match

    def test_auto_components_count_as_present(self):
        data = lod(LodType.GEOMETRY)
        self.assertEqual(len(with_rule(run(data), "T2-COMP")), 1)
        self.assertEqual(with_rule(run(data, auto_components=True), "T2-COMP"), [])

    def test_conventional_shadow_resolutions_pass(self):
        for resolution in (0.0, 10.0, 1000.0, 1010.0, 1020.0):
            self.assertEqual(with_rule(run(lod(LodType.SHADOW, resolution=resolution)), "T2-SHADOWRES"), [])

    def test_valid_groundlayer_and_roadway_pass(self):
        layer = lod(LodType.GROUNDLAYER, faces=[(0, 1, 2)])
        layer.mesh.uv_names = ["uv"]
        layer.mesh.polys[0].uvs["uv"] = [(0.0, 0.0), (1.0, 0.0), (1.0, -1.0)]
        self.assertEqual(run(layer), [])
        road = lod(LodType.ROADWAY, faces=[(0, 1, 2)])
        road.mesh.polys[0].texture = "road.paa"
        self.assertEqual(run(road), [])

    def test_master_switch_in_the_node_options(self):
        values = {"typerules": False}
        self.assertEqual(sorted(common.options_from(values).rules_off), sorted(type_rules.RULE_IDS))
        values = {"typerules": True, "t2tri": False}
        self.assertCountEqual(common.options_from(values).rules_off, ("T2-TRI", "T2-SHADOWRES", "T2-SHADOWOFF"))
        self.assertEqual(common.options_from({}).rules_off, ("T2-SHADOWRES", "T2-SHADOWOFF"))
        on = {"typerules": True, "t2shadowres": True, "t2shadowoff": True}
        self.assertEqual(common.options_from(on).rules_off, ())
        defaults = {parm.name: parm.default for parm in common.rule_parms()}
        self.assertEqual([name for name, value in defaults.items() if value is False], ["t2shadowres", "t2shadowoff"])
        names = [common.rule_parm_name(rule_id) for rule_id in type_rules.RULE_IDS]
        self.assertEqual(len(set(names)), 19)

    def test_rules_run_in_the_build_and_the_switches_silence_them(self):
        data = failing_examples()["T2-SHADOWRES"]
        report = messages.Report()
        model = build(Collection([data]), ExportOptions(rules_off=()), report)
        self.assertIsNotNone(model)
        self.assertTrue(with_rule(report.texts(messages.WARNING), "T2-SHADOWRES"))
        self.assertFalse(report.has_errors)
        quiet = messages.Report()
        build(Collection([failing_examples()["T2-SHADOWRES"]]), ExportOptions(rules_off=("T2-SHADOWRES",)), quiet)
        self.assertFalse(with_rule(quiet.texts(messages.WARNING), "T2-SHADOWRES"))


class TestFlags(unittest.TestCase):
    def test_known_and_unknown_values(self):
        for value in (0, 0x02000000, 0x04000000, 0x01000000 | 0x1, 0x2002, 0x100, 0x40, 0xFFFFFFFF & 0x02000000):
            self.assertFalse(flag_check.unknown_vertex_flags(value), hex(value))
        for value in (0x3, 0x06000000, 0x3000, 0x50, 0x8000, 0x12345678):
            self.assertTrue(flag_check.unknown_vertex_flags(value), hex(value))
        # 0x1000 and 0x2000: Bohemia's own Samples (the male, the plant) use them on faces (D24, D25). A3OB lists them as
        # vertex fog values only.
        for value in (0, 0x20, 0x80, 0x100000, 0x200000, 0x300, 0x10, 0x01000000, 0xFE000000, 0x100, 0x1000, 0x2000,
                      0x1020, 0x2000 | 0x300):
            self.assertFalse(flag_check.unknown_face_flags(value), hex(value))
        for value in (0x40, 0x300000, 0x2, 0xA0, 0x4000, 0x3000, 0x8000):
            self.assertTrue(flag_check.unknown_face_flags(value), hex(value))
        self.assertTrue(flag_check.unknown_vertex_flags(-0x80000000))  # int32 attributes hold the same bits as u32

    def test_interpolated_flags_warn_once_per_lod(self):
        data = lod(LodType.VISUAL, resolution=1.0)
        data.mesh.point_flags = [0x02000000, 0x01000003, 0x02000000, 0x1234, 0, 0, 0, 0]
        data.mesh.polys[1].flags = 0x40
        report = messages.Report()
        flag_check.check_flags(data, report)
        warnings = report.texts(messages.WARNING)
        self.assertEqual(len(warnings), 2)
        self.assertIn("2 points", warnings[0])
        self.assertIn("a3ob_vertex_flags", warnings[0])
        self.assertIn("points 1 (0x1000003), 3 (0x1234)", warnings[0])
        self.assertIn("a3ob_face_flags", warnings[1])
        self.assertIn("prims 1 (0x40)", warnings[1])

    def test_the_build_warns(self):
        data = lod(LodType.VISUAL, resolution=1.0)
        data.mesh.point_flags = [0x1234] + [0x02000000] * 7
        report = messages.Report()
        self.assertIsNotNone(build(Collection([data]), ExportOptions(), report))
        self.assertTrue(any("a3ob_vertex_flags" in text for text in report.texts(messages.WARNING)))


class TestDegenerateProxy(unittest.TestCase):
    def test_shapes(self):
        canonical = ((0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 1.0))
        self.assertIsNone(proxy_rebuild.degenerate_triangle(canonical))
        self.assertEqual(proxy_rebuild.degenerate_triangle(((0, 0, 0), (1, 0, 0), (2, 0, 0))), "zero area")
        self.assertEqual(proxy_rebuild.degenerate_triangle(((1, 1, 1),) * 3), "zero area")
        self.assertEqual(proxy_rebuild.degenerate_triangle(((0, 0, 0), (2, 0, 0), (1, 1.5, 0))), "two sides of equal length")
        self.assertEqual(proxy_rebuild.degenerate_triangle(((0, 0, 0), (1, 0, 0), (0, 1, 0))), "two sides of equal length")

    def proxy(self, points):
        poly = Poly(0, (0, 1, 2), [(-1.0, 0.0, 0.0)] * 3)
        mesh = MeshData(points=[tuple(map(float, p)) for p in points], polys=[poly])
        identity = (1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0)
        return ProxyData(3, "\\a3\\x\\p", 1, "\\a3\\x\\p", 1, identity, mesh)

    def test_rebuild_warns_for_a_collapsed_triangle_only(self):
        report = messages.Report()
        self.assertIsNotNone(proxy_rebuild.rebuild_proxy(self.proxy([(0, 0, 0), (1, 0, 0), (2, 0, 0)]), "Resolution 1", report))
        warnings = report.texts(messages.WARNING)
        self.assertEqual(len(warnings), 1)
        self.assertIn("degenerate triangle (zero area)", warnings[0])
        self.assertTrue(warnings[0].startswith("Resolution 1: "))
        report = messages.Report()
        proxy_rebuild.rebuild_proxy(self.proxy([(0, 0, 0), (0, 2, 0), (0, 0, -1)]), "Resolution 1", report)
        self.assertEqual(report.items, [])


class TestPolicy(unittest.TestCase):
    ITEMS = [(messages.ERROR, "A: bad."), (messages.WARNING, "B: odd."), (messages.INFO, "fine")]

    def test_error_level_keeps_severities(self):
        self.assertEqual(common.apply_policy(self.ITEMS, "error", False), self.ITEMS)

    def test_warning_level_turns_errors_into_warnings_with_error_prefix(self):
        shown = common.apply_policy(self.ITEMS, "warning", False)
        self.assertEqual(shown[0], (messages.WARNING, "Error: A: bad."))
        self.assertEqual(shown[1], self.ITEMS[1])

    def test_strict_turns_warnings_into_errors(self):
        shown = common.apply_policy(self.ITEMS, "error", True)
        self.assertEqual([kind for kind, _ in shown], [messages.ERROR, messages.ERROR, messages.INFO])
        self.assertTrue(shown[1][1].startswith("Warning as error: "))
        both = common.apply_policy(self.ITEMS, "warning", True)
        self.assertEqual([kind for kind, _ in both], [messages.WARNING, messages.WARNING, messages.INFO])
        self.assertEqual(both[1][1], "Warning as error: B: odd.")

    def test_blocked(self):
        self.assertTrue(common.is_blocked(self.ITEMS, False))
        self.assertFalse(common.is_blocked(self.ITEMS[1:], False))
        self.assertTrue(common.is_blocked(self.ITEMS[1:], True))

    def test_max_messages_is_kept_on_the_report(self):
        report = messages.Report()
        common.show(report, [(messages.WARNING, "w%d" % n) for n in range(10)], {"maxmessages": 3})
        self.assertEqual(report.max_messages, 3)
        self.assertEqual(len(report.lines()), 3)


if __name__ == "__main__":
    unittest.main()

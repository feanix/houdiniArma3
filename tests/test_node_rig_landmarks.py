"""The Rig Landmarks node in hython (tasks #71 and #74, D42). The position, mirror and selection tests need only ``hou``; the
guess comparison with Rig Fit needs KineFX (do not run it with HOUDINI_PACKAGE_SKIP=1) and the Arma 3 Samples (it skips
without them). The viewer state cannot be opened in hython, so its decisions are tested as functions and its glue with a
stand-in for ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import types
import unittest

import numpy as np

from a3ob_core.rig import LANDMARK_NAMES
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"

if HAS_HOU:
    from a3ob_houdini import landmark_io, rig_landmark_state, rig_weights
    from a3ob_houdini.errors import UserError
    from a3ob_houdini.nodes import rig_landmarks as lm


def short(name):
    return name[len("lm_"):]


def line_mesh(count=10):
    """A strip of ``count`` points with one triangle, as a File SOP geometry."""
    geo = hou.Geometry()
    points = geo.createPoints([(float(i), 0.0, 0.0) for i in range(count)])
    prim = geo.createPolygon()
    for point in points[:3]:
        prim.addVertex(point)
    return geo


class FakeSelection:
    """What ``hou.GeometrySelection`` gives ``average_selected``: ``nodes()`` and ``selections()`` side by side."""

    def __init__(self, node, numbers):
        self._node, self._numbers = node, numbers

    def nodes(self):
        return [self._node]

    def selections(self):
        numbers = self._numbers
        return [types.SimpleNamespace(points=lambda geo: [geo.iterPoints()[n] for n in numbers])]


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestRigLandmarksPositions(NodeTestCase):
    def make(self, geo=None, **fields):
        source = make_source(self.network, geo if geo is not None else line_mesh())
        node = self.network.createNode("a3ob_rig_landmarks")
        node.setInput(0, source)
        for name, value in fields.items():
            if name == "mirror":
                node.parm("mirror").set(value)
            else:
                node.parmTuple(name).set(value)
        return node

    def detail(self, node):
        return {name: tuple(node.geometry().attribValue("a3ob_" + name)) for name in LANDMARK_NAMES
                if node.geometry().findGlobalAttrib("a3ob_" + name) is not None}

    def test_one_vector_per_landmark_in_the_order_rig_fit_reads_them(self):
        node = self.make()
        shorts = [short(name) for name in LANDMARK_NAMES]
        parms = [t.name() for t in node.parmTuples() if t.name() in shorts]
        self.assertEqual(parms, shorts)
        for name in shorts:
            self.assertEqual(len(node.parmTuple(name)), 3)
            self.assertEqual(tuple(node.parmTuple(name).eval()), (0.0, 0.0, 0.0))
        self.assertEqual(node.parm("mirror").eval(), 1)  # on by default

    def test_match_buttons_copy_one_side_to_the_other_mirrored(self):
        node = self.make(chin=(0.0, 1.7, 0.1), shoulder_l=(0.2, 1.5, 0.0), knee_r=(-0.1, 0.5, 0.02), mirror=0)
        lm.BUTTONS["left_to_right"]({"node": node})
        self.assertEqual(tuple(node.parmTuple("shoulder_r").eval()), (-0.2, 1.5, 0.0))
        self.assertEqual(tuple(node.parmTuple("knee_r").eval()), (-0.1, 0.5, 0.02))  # unset left: right kept
        self.assertEqual(tuple(node.parmTuple("chin").eval()), (0.0, 1.7, 0.1))
        lm.BUTTONS["right_to_left"]({"node": node})
        self.assertEqual(tuple(node.parmTuple("knee_l").eval()), (0.1, 0.5, 0.02))
        self.assertEqual(tuple(node.parmTuple("shoulder_l").eval()), (0.2, 1.5, 0.0))
        self.assertEqual(tuple(node.parmTuple("chin").eval()), (0.0, 1.7, 0.1))
        self.assertEqual(tuple(node.parmTuple("groin").eval()), (0.0, 0.0, 0.0))

    def test_with_mirror_on_left_to_right_is_disabled_and_right_to_left_keeps_the_pair_mirrored(self):
        node = self.make(knee_r=(-0.1, 0.5, 0.02), mirror=0)
        node.parm("mirror").set(1)
        rule = lambda name: node.parm(name).parmTemplate().conditionals().get(hou.parmCondType.DisableWhen)
        self.assertEqual(rule("leftToRight"), "{ mirror != 0 }")  # disabled while Mirror is on
        self.assertIsNone(rule("rightToLeft"))
        lm.BUTTONS["right_to_left"]({"node": node})
        self.assertEqual(tuple(node.parmTuple("knee_l").eval()), (0.1, 0.5, 0.02))
        self.assertEqual(tuple(node.parmTuple("knee_r").eval()), (-0.1, 0.5, 0.02))

    def test_a_fresh_node_says_it_needs_the_guess_and_hands_rig_fit_no_zeros(self):
        node = self.make()
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("Guess Landmarks", " ".join(result.warnings))
        self.assertIn("18 landmarks are not set", " ".join(result.warnings))
        self.assertEqual(self.detail(node), {})

    def test_positions_are_written_as_detail_vectors_and_the_mesh_is_not_changed(self):
        node = self.make(chin=(0.0, 1.7, 0.1), shoulder_l=(0.2, 1.5, 0.0), mirror=0)
        source = node.inputs()[0]
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertIn("16 landmarks are not set", " ".join(result.warnings))
        got = self.detail(node)
        self.assertEqual(sorted(got), ["lm_chin", "lm_shoulder_l"])
        np.testing.assert_allclose(got["lm_chin"], (0.0, 1.7, 0.1), atol=1e-6)
        np.testing.assert_allclose(got["lm_shoulder_l"], (0.2, 1.5, 0.0), atol=1e-6)
        self.assertEqual(node.geometry().pointFloatAttribValues("P"), source.geometry().pointFloatAttribValues("P"))
        self.assertEqual(len(node.geometry().prims()), len(source.geometry().prims()))
        self.assertEqual([a.name() for a in node.geometry().pointAttribs()], [a.name() for a in source.geometry().pointAttribs()])
        self.assertEqual(node.geometry().pointGroups(), source.geometry().pointGroups())

    def test_mirror_on_the_right_side_follows_the_left_about_x_0(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.05))
        cook(node)
        got = self.detail(node)
        np.testing.assert_allclose(got["lm_shoulder_r"], (-0.2, 1.5, 0.05), atol=1e-6)
        # the field is brought in step by the edit callback
        lm.sync_mirror(node)
        self.assertEqual(tuple(node.parmTuple("shoulder_r").eval()), (-0.2, 1.5, 0.05))
        # moving the left one moves the right one
        node.parmTuple("shoulder_l").set((0.3, 1.4, 0.0))
        lm.sync_mirror(node)
        self.assertEqual(tuple(node.parmTuple("shoulder_r").eval()), (-0.3, 1.4, 0.0))

    def test_the_cook_mirrors_even_when_the_right_field_is_stale(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.0), shoulder_r=(-0.9, 0.0, 0.0))
        cook(node)
        np.testing.assert_allclose(self.detail(node)["lm_shoulder_r"], (-0.2, 1.5, 0.0), atol=1e-6)

    def test_mirror_off_makes_both_sides_free(self):
        node = self.make(mirror=0, shoulder_l=(0.2, 1.5, 0.0), shoulder_r=(-0.35, 1.45, 0.0))
        cook(node)
        got = self.detail(node)
        np.testing.assert_allclose(got["lm_shoulder_r"], (-0.35, 1.45, 0.0), atol=1e-6)
        lm.sync_mirror(node)  # does nothing with Mirror off
        self.assertEqual(tuple(node.parmTuple("shoulder_r").eval()), (-0.35, 1.45, 0.0))

    def test_the_right_fields_are_disabled_only_while_mirror_is_on(self):
        node = self.make()
        def rule(name):
            return node.parmTuple(name).parmTemplate().conditionals().get(hou.parmCondType.DisableWhen)

        self.assertEqual(rule("elbow_r"), "{ mirror != 0 }")  # disabled while Mirror is on
        self.assertIsNone(rule("elbow_l"))
        self.assertIsNone(rule("chin"))

    def test_a_right_landmark_set_alone_makes_its_left_partner_follow(self):
        node = self.make(knee_r=(-0.15, 0.5, 0.02))
        cook(node)
        lm.sync_mirror(node)
        self.assertEqual(tuple(node.parmTuple("knee_l").eval()), (0.15, 0.5, 0.02))

    def test_set_landmark_on_a_right_one_sets_its_left_partner_with_mirror_on(self):
        node = self.make()
        lm.set_landmark(node, "wrist_r", (-0.7, 1.3, 0.0))
        self.assertEqual(tuple(node.parmTuple("wrist_l").eval()), (0.7, 1.3, 0.0))
        node.parm("mirror").set(0)
        lm.set_landmark(node, "wrist_r", (-0.8, 1.2, 0.0))
        self.assertEqual(tuple(node.parmTuple("wrist_l").eval()), (0.7, 1.3, 0.0))

    def test_the_edit_callback_is_on_the_fields_and_the_toggle(self):
        node = self.make()
        self.assertIn("'moved'", node.parmTuple("chin").parmTemplate().scriptCallback())
        self.assertIn("'mirror_changed'", node.parm("mirror").parmTemplate().scriptCallback())

    def test_set_from_selection_is_the_average_and_only_the_position_is_kept(self):
        node = self.make(mirror=0)
        node.parm("which").set("chin")
        lm.set_from_selection(node, FakeSelection(node, [2, 4, 6]))
        np.testing.assert_allclose(node.parmTuple("chin").eval(), (4.0, 0.0, 0.0))
        result = cook(node)
        self.assertEqual(result.errors, ())
        self.assertEqual(node.geometry().pointGroups(), ())  # no selection or group is stored
        np.testing.assert_allclose(self.detail(node)["lm_chin"], (4.0, 0.0, 0.0))

    def test_set_from_selection_with_mirror_moves_the_partner(self):
        node = self.make()
        node.parm("which").set("hip_l")
        lm.set_from_selection(node, FakeSelection(node, [1, 3]))
        self.assertEqual(tuple(node.parmTuple("hip_l").eval()), (2.0, 0.0, 0.0))
        self.assertEqual(tuple(node.parmTuple("hip_r").eval()), (-2.0, 0.0, 0.0))

    def test_set_from_selection_needs_selected_points_and_a_viewport(self):
        node = self.make()
        with self.assertRaises(UserError):
            lm.average_selected(FakeSelection(node, []))
        with self.assertRaises(UserError):  # hython has no viewport
            lm.set_from_selection(node)

    def test_guess_needs_the_skeleton_and_the_sample_body(self):
        node = self.make()
        with self.assertRaises(UserError) as caught:
            lm.guess_landmarks_on(node)
        self.assertIn("skeleton", str(caught.exception))

    def test_the_buttons_never_raise_in_hython(self):
        node = self.make()
        for action in ("guess", "guessone", "fromsel"):
            lm.BUTTONS[action]({"node": node})

    def test_no_input_is_an_error(self):
        node = self.network.createNode("a3ob_rig_landmarks")
        self.assertTrue(cook(node).errors)  # Houdini itself: not enough sources


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestViewerState(NodeTestCase):
    """The decisions of the viewer state and its glue. The asset carries the state; the GUI runs it (owner check)."""

    def make(self, **fields):
        node = self.network.createNode("a3ob_rig_landmarks")
        for name, value in fields.items():
            if name == "mirror":
                node.parm("mirror").set(value)
            else:
                node.parmTuple(name).set(value)
        return node

    def test_the_asset_carries_the_state_sections(self):
        node = self.make()
        sections = node.type().definition().sections()
        self.assertEqual(sections["DefaultState"].contents(), "a3ob_rig_landmarks_state")
        self.assertIn("create_template", sections["ViewerStateModule"].contents())
        self.assertTrue(hasattr(node.type().hdaViewerStateModule(), "createViewerStateTemplate"))
        self.assertTrue(node.type().definition().extraFileOptions()["ViewerStateModule/IsPython"])

    def test_the_asset_registers_its_state_like_a_stock_asset(self):
        """After a plain installFile the definition carries the sections that register the embedded state, with the
        same contents and file options as a stock asset (attribpaint) with an embedded state. hython has no UI, so the
        registration itself cannot run here: the owner checks that in the GUI (select the node, press Enter)."""
        definition = self.make().type().definition()
        sections, options = definition.sections(), definition.extraFileOptions()
        stock = [d for d in hou.hda.definitionsInFile(hou.text.expandString("$HFS/houdini/otls/OPlibSop.hda"))
                 if d.nodeTypeName() == "attribpaint"][0]
        for name in ("ViewerStateInstall", "ViewerStateUninstall"):
            self.assertEqual(sections[name].contents(), stock.sections()[name].contents())
        self.assertIn("register_pystate_embedded(kwargs['type'])", sections["ViewerStateInstall"].contents())
        self.assertIn("unregister_pystate_embedded(kwargs['type'])", sections["ViewerStateUninstall"].contents())
        for name in ("ViewerStateModule", "ViewerStateInstall", "ViewerStateUninstall"):
            for flag in ("IsPython", "IsScript", "IsViewerState"):
                self.assertTrue(stock.extraFileOptions()["%s/%s" % (name, flag)])
                self.assertTrue(options["%s/%s" % (name, flag)], name + "/" + flag)

    def test_reload_all_does_not_fail_headless(self):
        """Headless there is no UI, so the states are not registered again; only the module reload runs."""
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = subprocess.run(
            [sys.executable, "-c", "import hou\nfrom a3ob_houdini import reload\nassert reload.reload_all() > 2\n"
             "print('reloaded', hou.isUIAvailable())"],
            env=dict(os.environ, PYTHONPATH=os.path.join(repo, "python")), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("reloaded False", done.stdout)  # a separate process: reload_all here would split module identity
        from a3ob_houdini import reload
        self.assertEqual(reload.reregister_states(types.SimpleNamespace(
            sopNodeTypeCategory=lambda: (_ for _ in ()).throw(RuntimeError("no"))), types.SimpleNamespace()), [])

    def test_reregister_unregisters_then_registers_the_a3ob_types_with_a_state(self):
        from a3ob_houdini import reload
        calls = []

        def node_type(name, sections):
            definition = types.SimpleNamespace(sections=lambda: sections)
            return types.SimpleNamespace(name=lambda: name, definition=lambda: definition)

        mine = node_type("a3ob_rig_landmarks", {"DefaultState": 1})
        fake_hou = types.SimpleNamespace(sopNodeTypeCategory=lambda: types.SimpleNamespace(nodeTypes=lambda: {
            "a": mine, "b": node_type("a3ob_mass", {}), "c": node_type("attribpaint", {"DefaultState": 1})}))
        utils = types.SimpleNamespace(unregister_pystate_embedded=lambda t: calls.append(("un", t.name())),
                                      register_pystate_embedded=lambda t: calls.append(("re", t.name())))
        self.assertEqual(reload.reregister_states(fake_hou, utils), ["a3ob_rig_landmarks"])
        self.assertEqual(calls, [("un", "a3ob_rig_landmarks"), ("re", "a3ob_rig_landmarks")])

    def test_the_template_binds_one_translate_handle_per_landmark(self):
        calls = []

        class Template:
            def __init__(self, *args):
                calls.append(("template",) + args)

            def bindFactory(self, factory):
                calls.append(("factory", factory))

            def bindHandle(self, kind, name, *args):
                calls.append(("handle", kind, name))

        fake = types.SimpleNamespace(ViewerStateTemplate=Template, sopNodeTypeCategory=lambda: "Sop")
        real = rig_landmark_state.hou
        rig_landmark_state.hou = fake
        try:
            template = rig_landmark_state.create_template("a3ob_rig_landmarks_state")
        finally:
            rig_landmark_state.hou = real
        self.assertIsInstance(template, Template)
        handles = [call[2] for call in calls if call[0] == "handle"]
        self.assertEqual(handles, [name for name in LANDMARK_NAMES])
        self.assertTrue(all(call[1] == "translate" for call in calls if call[0] == "handle"))
        self.assertIn(("factory", rig_landmark_state.State), calls)

    def test_markers_are_drawn_for_landmarks_that_are_set_and_right_ones_are_not_draggable_while_mirrored(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.0), chin=(0.0, 1.7, 0.1))
        rows = {row[0]: row for row in rig_landmark_state.marker_rows(lm.positions_of(node), True)}
        self.assertEqual([row[0] for row in rig_landmark_state.marker_rows(lm.positions_of(node), True)],
                         [short(name) for name in LANDMARK_NAMES])
        self.assertEqual(rows["shoulder_l"][1:], ("Left Shoulder", (0.2, 1.5, 0.0), True, True, "left"))
        self.assertEqual(rows["shoulder_r"][1:], ("Right Shoulder", (-0.2, 1.5, 0.0), True, False, "right"))
        self.assertEqual(rows["chin"][3:], (True, True, "middle"))
        self.assertFalse(rows["elbow_l"][3])  # not set: no marker
        free = {row[0]: row for row in rig_landmark_state.marker_rows(lm.positions_of(node), False)}
        self.assertFalse(free["shoulder_r"][3])  # not set, and mirror off does not invent it
        self.assertTrue(free["shoulder_l"][4])

    def test_the_handle_follows_the_field_and_hides_when_not_set_or_mirrored(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.0))
        self.assertEqual(rig_landmark_state.handle_position(node, "shoulder_l"), ((0.2, 1.5, 0.0), True))
        self.assertEqual(rig_landmark_state.handle_position(node, "shoulder_r"), ((-0.2, 1.5, 0.0), False))
        self.assertEqual(rig_landmark_state.handle_position(node, "elbow_l")[1], False)
        node.parm("mirror").set(0)
        node.parmTuple("shoulder_r").set((-0.25, 1.5, 0.0))
        self.assertEqual(rig_landmark_state.handle_position(node, "shoulder_r"), ((-0.25, 1.5, 0.0), True))

    def test_dragging_a_handle_writes_the_field_and_the_partner(self):
        node = self.make()
        rig_landmark_state.apply_handle_edit(node, "knee_l", (0.12, 0.5, 0.03))
        self.assertEqual(tuple(node.parmTuple("knee_l").eval()), (0.12, 0.5, 0.03))
        self.assertEqual(tuple(node.parmTuple("knee_r").eval()), (-0.12, 0.5, 0.03))
        node.parm("mirror").set(0)
        rig_landmark_state.apply_handle_edit(node, "knee_l", (0.2, 0.5, 0.03))
        self.assertEqual(tuple(node.parmTuple("knee_r").eval()), (-0.12, 0.5, 0.03))

    def test_spheres_follow_the_label_colours_and_only_set_landmarks_have_one(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.0), chin=(0.0, 1.7, 0.1))
        spheres = {row[0]: row for row in rig_landmark_state.sphere_markers(lm.positions_of(node), True)}
        self.assertEqual(sorted(spheres), ["chin", "shoulder_l", "shoulder_r"])  # mirror shows the right partner
        self.assertEqual(spheres["shoulder_l"][1:], ((0.2, 1.5, 0.0), rig_landmark_state.SPHERE_RADIUS,
                                                     rig_landmark_state.COLORS["left"]))
        self.assertEqual(spheres["shoulder_r"][3], rig_landmark_state.COLORS["right"])
        self.assertEqual(spheres["chin"][3], rig_landmark_state.COLORS["middle"])
        self.assertTrue(0.015 <= rig_landmark_state.SPHERE_RADIUS <= 0.02)
        free = [row[0] for row in rig_landmark_state.sphere_markers(lm.positions_of(node), False)]
        self.assertEqual(sorted(free), ["chin", "shoulder_l"])

    def test_the_ghost_shading_is_set_and_put_back_exactly(self):
        class DisplaySet:
            def __init__(self, mode):
                self.mode = mode

            def shadedMode(self):
                return self.mode

            def setShadedMode(self, mode):
                self.mode = mode

        sets = [DisplaySet("smooth"), DisplaySet("wire")]
        saved = rig_landmark_state.ghost_display(None, "ghost", sets)
        self.assertEqual([item.mode for item in sets], ["ghost", "ghost"])
        rig_landmark_state.restore_display(saved)
        self.assertEqual([item.mode for item in sets], ["smooth", "wire"])

    def test_the_state_callbacks_pass_the_events_to_those_functions(self):
        node = self.make(shoulder_l=(0.2, 1.5, 0.0))

        class Label:
            def draw(self, *args):
                pass

            def show(self, visible):
                pass

        shown = []

        class Handle:
            def __init__(self, viewer, name):
                self.name = name

            def show(self, visible):
                shown.append((self.name, visible))

            def update(self):
                pass

        class Display:
            mode = "smooth"

            def shadedMode(self):
                return self.mode

            def setShadedMode(self, mode):
                self.mode = mode

        display = {"DisplayModel": Display(), "CurrentModel": Display()}
        names = lambda: types.SimpleNamespace(DisplayModel="DisplayModel", CurrentModel="CurrentModel")
        verb = types.SimpleNamespace(setParms=lambda parms: None, execute=lambda geometry, inputs: None)
        fake = types.SimpleNamespace(
            TextDrawable=lambda *args: Label(), Handle=Handle, Geometry=lambda: None,
            GeometryDrawable=lambda *args, **kwargs: Label(), drawableGeometryType=types.SimpleNamespace(Face="Face"),
            sopNodeTypeCategory=lambda: types.SimpleNamespace(nodeVerb=lambda name: verb),
            displaySetType=names(), glShadingType=types.SimpleNamespace(WireGhost="ghost"))
        real = rig_landmark_state.hou
        rig_landmark_state.hou = fake
        try:
            settings = types.SimpleNamespace(displaySet=lambda name: display[name])
            viewer = types.SimpleNamespace(beginStateUndo=lambda text: None, endStateUndo=lambda: None,
                                           curViewport=lambda: types.SimpleNamespace(settings=lambda: settings))
            state = rig_landmark_state.State("a3ob_rig_landmarks_state", viewer)
            state.onEnter({"node": node})
            self.assertEqual([item.mode for item in display.values()], ["ghost", "ghost"])
            state.onExit({})
            self.assertEqual([item.mode for item in display.values()], ["smooth", "smooth"])
            state.onResume({"node": node})
            parms = {"tx": 0.0, "ty": 0.0, "tz": 0.0}
            state.onStateToHandle({"handle": "lm_shoulder_l", "parms": parms})
            self.assertEqual((parms["tx"], parms["ty"], parms["tz"]), (0.2, 1.5, 0.0))
            self.assertIn(("lm_shoulder_l", True), shown)
            state.onBeginHandleToState({})
            state.onHandleToState({"handle": "lm_shoulder_l", "parms": {"tx": 0.25, "ty": 1.45, "tz": 0.01}})
            state.onEndHandleToState({})
        finally:
            rig_landmark_state.hou = real
        self.assertEqual(tuple(node.parmTuple("shoulder_l").eval()), (0.25, 1.45, 0.01))
        self.assertEqual(tuple(node.parmTuple("shoulder_r").eval()), (-0.25, 1.45, 0.01))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestRigLandmarksWithRigFit(NodeTestCase):
    def setUp(self):
        super().setUp()
        path = samples_support.path
        importer = self.network.createNode("a3ob_p3d_import")
        importer.parm("file").set(path(SAMPLE).replace("\\", "/"))
        self.body = self.network.createNode("a3ob_extract_lod")
        self.body.setInput(0, importer)
        self.body.parm("lod").set("Resolution 1")
        self.skeleton = self.network.createNode("a3ob_rig_skeleton")
        self.skeleton.parm("cfg").set(path(SAMPLE_CFG).replace("\\", "/"))
        self.skeleton.parm("pivots").set(path(SAMPLE_PIVOTS).replace("\\", "/"))
        for node in (self.body, self.skeleton):
            self.assertEqual(cook(node).errors, ())

    def landmarks(self, mirror=0):
        node = self.network.createNode("a3ob_rig_landmarks")
        node.setInput(0, self.body)
        node.setInput(1, self.skeleton)
        node.setInput(2, self.body)
        node.parm("mirror").set(mirror)
        return node

    def fit(self, mesh, **parms):
        node = self.network.createNode("a3ob_rig_fit")
        node.setInput(0, mesh)
        node.setInput(1, self.skeleton)
        node.setInput(2, self.body)
        node.parm("show").set("landmarks")
        parms.setdefault("refine", 0)  # Refine is on by default (task #79); these tests are about the landmarks
        for name, value in parms.items():
            node.parm(name).set(value)
        return node

    def used(self, node):
        geo = rig_weights.read_part("test", node.geometry(), "landmarks")
        return {n: np.array(p.position()) for n, p in zip(geo.pointStringAttribValues("name"), geo.points())}

    def test_guess_fills_the_fields_with_the_values_rig_fit_guesses(self):
        node = self.landmarks(mirror=0)
        lm.guess_landmarks_on(node)
        fit = self.fit(self.body, landmarks="guess", snap=0, symmetry=0)
        self.assertEqual(cook(fit).errors, ())
        guessed = self.used(fit)
        for name in LANDMARK_NAMES:
            np.testing.assert_allclose(node.parmTuple(short(name)).eval(), guessed[name], atol=1e-9, err_msg=name)
        self.assertEqual([w for w in cook(node).warnings], [])  # all 18 are set now

    def test_guess_with_mirror_on_keeps_the_left_guess_and_mirrors_it(self):
        node = self.landmarks(mirror=1)
        lm.guess_landmarks_on(node)
        fit = self.fit(self.body, landmarks="guess", snap=0, symmetry=0)
        self.assertEqual(cook(fit).errors, ())
        guessed = self.used(fit)
        for name in LANDMARK_NAMES:
            if name.endswith("_r"):
                continue
            np.testing.assert_allclose(node.parmTuple(short(name)).eval(), guessed[name], atol=1e-9, err_msg=name)
        np.testing.assert_allclose(node.parmTuple("knee_r").eval(), np.array(node.parmTuple("knee_l").eval()) * (-1, 1, 1))

    def test_the_guess_in_the_node_equals_the_guess_in_rig_fit_end_to_end(self):
        node = self.landmarks(mirror=0)
        lm.guess_landmarks_on(node)
        by_node = self.fit(node, landmarks="landmarks", symmetry=0)
        by_guess = self.fit(self.body, landmarks="guess", symmetry=0)
        self.assertEqual(cook(by_node).errors, ())
        self.assertEqual(cook(by_guess).errors, ())
        first, second = self.used(by_node), self.used(by_guess)
        for name in LANDMARK_NAMES:
            np.testing.assert_allclose(first[name], second[name], atol=1e-5, err_msg=name)  # the detail vectors are float32

    def test_guess_this_landmark_puts_one_back_and_leaves_the_others(self):
        node = self.landmarks(mirror=0)
        lm.guess_landmarks_on(node)
        guessed = {name: tuple(node.parmTuple(short(name)).eval()) for name in LANDMARK_NAMES}
        node.parmTuple("knee_l").set((9.0, 9.0, 9.0))
        node.parmTuple("knee_r").set((8.0, 8.0, 8.0))
        node.parm("which").set("knee_l")
        lm.BUTTONS["guessone"]({"node": node})
        self.assertEqual(tuple(node.parmTuple("knee_l").eval()), guessed["lm_knee_l"])
        self.assertEqual(tuple(node.parmTuple("knee_r").eval()), (8.0, 8.0, 8.0))

    def test_a_fresh_node_gives_rig_fit_nothing_and_it_guesses(self):
        fresh = self.landmarks()
        self.assertIn("not set", " ".join(cook(fresh).warnings))
        both = self.fit(fresh, landmarks="both")
        result = cook(both)
        self.assertEqual(result.errors, ())
        self.assertIn("0 from Rig Landmarks, 18 guessed", "\n".join(result.messages))
        only = self.fit(fresh, landmarks="landmarks")
        self.assertIn("not set", " ".join(cook(only).errors))

    def test_picks_turned_into_their_average_reach_rig_fit_as_that_average(self):
        """The conversion of the owner's point picks (task #74, criterion 7): the average of the points is the landmark."""
        flat = np.array(self.body.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        info = rig_weights.read_skeleton("test", self.skeleton.geometry())
        rest = np.array(info.positions)
        index = {n.lower(): i for i, n in enumerate(info.names)}
        node = self.landmarks(mirror=0)
        expected = {}
        from a3ob_core.rig import BONE_OF
        for name in ("lm_shoulder_l", "lm_elbow_l", "lm_knee_l", "lm_knee_r"):
            joint = rest[index[BONE_OF[name].lower()]]
            picks = np.argsort(np.linalg.norm(flat - joint, axis=1))[:6]
            expected[name] = flat[picks].mean(axis=0)
            node.parmTuple(short(name)).set(tuple(float(v) for v in expected[name]))
        fit = self.fit(node, landmarks="both", snap=0, symmetry=0)
        result = cook(fit)
        self.assertEqual(result.errors, ())
        self.assertIn("4 from Rig Landmarks, 14 guessed", "\n".join(result.messages))
        got = self.used(fit)
        for name, value in expected.items():
            np.testing.assert_allclose(got[name], value, atol=1e-5, err_msg=name)


if __name__ == "__main__":
    unittest.main()

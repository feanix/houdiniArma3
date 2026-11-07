"""The rig digests in hython (task #81): what the digest reads from a geometry equals what P3D Export reads, the command line
and its exit codes, and the digests on the Arma 3 Samples character (they skip without the Samples). Needs ``hou`` with
KineFX (do not run with HOUDINI_PACKAGE_SKIP=1).

What these catch: a weight read that differs from the export's (the trap: an ``a3ob_w_`` value on a point that is not in the
bone's selection), a digest that reads a Rig Fit part wrongly, an exit code that does not say what happened.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from a3ob_core.digest import weight_figures
from tests import hou_support as hs
from tests import samples_support
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source

SAMPLE = "Addons/Test_Character_01/A3_character_example.p3d"
SAMPLE_CFG = "Addons/Test_Character_01/model.cfg"
SAMPLE_PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"

if HAS_HOU:
    from a3ob_houdini import digest, messages, names
    from a3ob_houdini.p3d_export import ExportOptions, read_geo, type_rules
    from a3ob_houdini.p3d_export.data import Details, LodData


def weighted_geo():
    """Six points, three triangles, bone selections spelled as the export accepts them (ignoring case), with the trap: weights
    in ``a3ob_w_`` on points that are not in the selection, and a member whose weight is 0."""
    geo = hou.Geometry()
    points = geo.createPoints([(float(i), 0.0, 0.0) for i in range(6)])
    for ring in ((0, 1, 2), (3, 4, 5), (1, 2, 3)):
        poly = geo.createPolygon()
        for number in ring:
            poly.addVertex(points[number])

    def select(group, members, values):
        geo.createPointGroup(group).add([points[i] for i in members])
        attrib = names.point_weight_attrib(group)
        geo.addAttrib(hou.attribType.Point, attrib, 1.0)
        geo.setPointFloatAttribValues(attrib, values)

    select("LeftArm", (0, 1, 2), [0.6, 0.5, 0.4, 0.9, 0.9, 0.9])  # points 3, 4, 5 hold 0.9 but are not members
    select("head", (2, 3), [0.0, 0.0, 0.5, 0.5, 0.0, 0.0])
    select("pelvis", (0, 5), [0.4, 0.0, 0.0, 0.0, 0.0, 0.0])  # lower case; point 5 is a member with weight 0
    geo.createPointGroup("not_a_bone").add([points[4]])
    return geo


def skeleton_geo(scale=1.0, lift=0.0):
    """A Pelvis, a Spine and a head joint with bone lines from parent to child."""
    geo = hou.Geometry()
    geo.addAttrib(hou.attribType.Point, "name", "")
    pts = geo.createPoints([(0.0, 1.0 * scale + lift, 0.0), (0.0, 1.2 * scale + lift, 0.0), (0.0, 1.6 * scale + lift, 0.0)])
    for point, name in zip(pts, ("Pelvis", "Spine", "head")):
        point.setAttribValue("name", name)
    for a, b in ((0, 1), (1, 2)):
        line = geo.createPolygon()
        line.addVertex(pts[a])
        line.addVertex(pts[b])
        line.setIsClosed(False)
    return geo


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestWeightsEqualTheExport(NodeTestCase):
    def export_side(self, geo):
        report = messages.Report()
        mesh = read_geo.read_mesh(geo, "t", report)
        return type_rules._Context(LodData(0, mesh, Details()), ExportOptions()).bone_weights

    def test_the_digest_reads_weights_as_the_export_does(self):
        geo = weighted_geo()
        used, total = self.export_side(geo)
        matrix, how, zero = digest._read_weights(geo, "t")
        figures = weight_figures(matrix)
        self.assertEqual(list(figures["influences"]), used)
        for mine, theirs in zip(figures["sums"], total):
            self.assertAlmostEqual(float(mine), theirs)
        self.assertEqual(how, "bone selections")
        self.assertEqual(list(figures["influences"]), [2, 1, 2, 1, 0, 0])
        # point 3 holds 0.9 in a3ob_w_LeftArm but is not in LeftArm: only its head weight (0.5) counts
        self.assertAlmostEqual(float(matrix[3].sum()), 0.5)
        self.assertEqual(zero, 1)  # pelvis point 5: a member with weight 0, which the export writes as "no weight"

    def test_the_page_through_a_node(self):
        node = make_source(self.network, weighted_geo())
        page = digest.weights(node.path())
        text = page.text()
        self.assertIn("6 points; 2 unweighted", page.lines()[0])
        self.assertIn("node: " + node.path() + "; whole output", text)
        self.assertIn("scene: ", text)
        self.assertTrue(page.lines()[-1].startswith("next:"))

    def test_no_weights_cannot_be_answered(self):
        geo = hou.Geometry()
        geo.createPoints([(0.0, 0.0, 0.0)])
        node = make_source(self.network, geo)
        with self.assertRaises(digest.CouldNotAnswer):
            digest.weights(node.path())


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestDigestsOnNodes(NodeTestCase):
    def test_skeleton_alone_says_the_rest_is_not_recorded(self):
        node = make_source(self.network, skeleton_geo())
        page = digest.skeleton(node.path())
        self.assertIn("3 joints, 1 root", page.lines()[0])
        self.assertIn("Arma rest: n/r", page.text())

    def test_skeleton_beside_a_rest_node(self):
        a = make_source(self.network, skeleton_geo(1.0))
        b = make_source(self.network, skeleton_geo(2.0))
        page = digest.skeleton(b.path(), rest=a.path())
        self.assertIn("40.0/20.0", page.text())  # Spine: 0.4 m in the doubled copy, 0.2 m in the rest

    def test_skeleton_compare_with_itself_has_no_offset(self):
        a = make_source(self.network, skeleton_geo())
        page = digest.skeleton_compare(a.path(), a.path())
        self.assertIn("offset median 0.0 cm, max 0.0 cm", page.lines()[0])

    def test_deform_compare_with_itself_is_one(self):
        geo = weighted_geo()
        node = make_source(self.network, geo)
        page = digest.deform_compare(node.path(), node.path())
        self.assertIn("median 1.000, p1 1.000, p99 1.000", page.lines()[0])

    def test_deform_compare_of_other_topology_cannot_be_answered(self):
        a = make_source(self.network, weighted_geo())
        small = hou.Geometry()
        small.createPoints([(0.0, 0.0, 0.0)] * 3)
        poly = small.createPolygon()
        for point in small.points():
            poly.addVertex(point)
        b = make_source(self.network, small)
        with self.assertRaises(digest.CouldNotAnswer):
            digest.deform_compare(a.path(), b.path())

    def test_wrong_questions(self):
        node = make_source(self.network, weighted_geo())
        with self.assertRaises(digest.WrongQuestion):
            digest.weights("/obj/no_such_node")
        with self.assertRaises(digest.WrongQuestion):
            digest.deform_compare(node.path(), node.path(), group="no_such_group")
        with self.assertRaises(digest.WrongQuestion):
            digest.landmarks(node.path())  # a File SOP is neither a Rig Fit nor a Rig Landmarks node


DRIVER = """
import sys
sys.path.insert(0, sys.argv[1])
from a3ob_houdini import digest
scene, node = sys.argv[2], sys.argv[3]
for argv in (["weights", scene, node], ["weights", scene, "/obj/nope"], ["skeleton", scene, node]):
    print("CODE", digest.main(argv))
sys.stdout.flush()
"""


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestCommandLine(unittest.TestCase):
    """The exit codes. The commands that load a scene run in a hython of their own, so this session keeps its HDAs and nodes."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = digest.main(argv)
            except SystemExit as stop:  # argparse: the question was written wrongly
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_questions_written_wrongly_end_before_the_scene_loads(self):
        folder = tempfile.mkdtemp(prefix="a3ob_digest_")
        scene = os.path.join(folder, "scene.hip")
        with open(scene, "wb") as file:
            file.write(b"not a scene")
        self.assertEqual(self.run_main(["weights", os.path.join(folder, "none.hip"), "/obj/x"])[0], 2)
        self.assertEqual(self.run_main(["no-such-digest", scene, "/obj/x"])[0], 2)
        self.assertEqual(self.run_main(["skeleton-compare", scene, "/obj/x"])[0], 2)  # needs two nodes
        self.assertEqual(self.run_main(["weights", scene, "/obj/x", "/obj/y"])[0], 2)

    def test_exit_codes_on_a_saved_scene_and_the_scene_is_not_saved(self):
        folder = tempfile.mkdtemp(prefix="a3ob_digest_")
        scene = os.path.join(folder, "scene.hip")
        network = hou.node("/obj").createNode("geo", "digest_cli")
        node = make_source(network, weighted_geo())
        before = hou.hipFile.path()
        try:
            hou.hipFile.save(scene, save_to_recent_files=False)
            hou.hipFile.setName(before)
            stamp = os.stat(scene).st_mtime_ns
            driver = os.path.join(folder, "driver.py")
            with open(driver, "w") as file:
                file.write(DRIVER)
            python_dir = os.path.join(hs.REPO_ROOT, "python")
            done = subprocess.run([sys.executable, driver, python_dir, scene, node.path()], capture_output=True, text=True,
                                  timeout=300)
            codes = [line.split()[1] for line in done.stdout.splitlines() if line.startswith("CODE")]
            self.assertEqual(codes, ["0", "2", "1"], done.stdout + done.stderr)
            self.assertIn("weights: 6 points", done.stdout)
            self.assertIn("scene: scene.hip", done.stdout)
            self.assertEqual(os.stat(scene).st_mtime_ns, stamp)
        finally:
            path = node.userData("a3ob_test_file")
            network.destroy()
            if path and os.path.exists(path):
                os.remove(path)


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestDigestsOnTheSamples(NodeTestCase):
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
        self.fit = self.network.createNode("a3ob_rig_fit")
        self.fit.setInput(0, self.body)
        self.fit.setInput(1, self.skeleton)
        self.fit.setInput(2, self.body)
        # The sample has no body group, and Refine (on by default since #79) needs one.
        self.fit.parm("refine").set(0)
        for node in (self.body, self.skeleton, self.fit):
            self.assertEqual(cook(node).errors, ())

    def test_the_five_digests(self):
        skeleton = digest.skeleton(self.skeleton.path())
        self.assertIn("64 joints, 1 root", skeleton.lines()[0])
        fitted = digest.skeleton(self.fit.path())
        self.assertIn("part fitted_skeleton", fitted.text())
        self.assertIn("(input 2)", fitted.text())
        compare = digest.skeleton_compare(self.skeleton.path(), self.fit.path())
        self.assertIn("64 joints", compare.lines()[0])
        weights = digest.weights(self.body.path())
        self.assertIn("bone selections", weights.text())
        deform = digest.deform_compare(self.body.path(), self.body.path())
        self.assertIn("median 1.000, p1 1.000, p99 1.000", deform.lines()[0])
        landmarks = digest.landmarks(self.fit.path())
        self.assertIn("18 of 18 set", landmarks.lines()[0])
        for page in (skeleton, fitted, compare, weights, deform, landmarks):
            self.assertEqual(page.size_notice(), "", page.text())

    def test_feet_and_wrap_distance(self):
        """What this catches: a feet page that reads the wrong parts of Rig Fit (the sample's soles are flat, as the advisor
        measured on the owner scene: -0.6 and -0.2 degrees), a wrap distance that is not zero for a body wrapped onto itself."""
        feet = digest.feet([self.fit.path()])
        rows = [line.split() for line in feet.lines() if line.startswith(("your body", "wrapped body", "Arma sample"))]
        self.assertEqual(len(rows), 6, feet.text())
        for row in rows:
            self.assertLess(abs(float(row[-5])), 1.0, feet.text())  # the pitch column of each row, in degrees
        wrap = digest.wrap_distance(self.fit.path())
        self.assertIn("median 0.0,", wrap.lines()[0])  # the fitted sample body is the sample itself (Refine is off)
        self.assertIn("foot ends", wrap.text())
        for page in (feet, wrap):
            self.assertEqual(page.size_notice(), "", page.text())


if __name__ == "__main__":
    unittest.main()

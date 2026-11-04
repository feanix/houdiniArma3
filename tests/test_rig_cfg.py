"""The model.cfg reader, the pivots reader and the joint plan of the Rig Skeleton node (a3ob_core.rig). Plain Python;
the tests on the real Samples files skip when the Samples are not there (tests/samples_support.py).

Design tests 1.2 and 1.3 of docs/design/character-rigging.md (6.3).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import unittest

from a3ob_core import p3d
from a3ob_core.rig import (CfgError, check_bones, parse_cfg, pivot_positions, plan_skeleton, read_cfg_file,
                           read_pivots_file, resolve_skeleton, skeleton_names)
from tests import samples_support

TEXT = '''
#include "\\a3\\anims_f\\config.cpp"
// A comment with "quotes" and class Fake { };
class CfgSkeletons {
    class Base;                       /* a reference, not a skeleton */
    class Parent {
        isDiscrete = 0;
        skeletonInherit = "";
        skeletonBones[] = {
            "Pelvis", "",
            "Spine", "Pelvis",     // trailing comment
            "head", "Spine"
        };
        pivotsModel = "a3\\anims_f\\skeleton.p3d";
    };
    class Child: Parent {
        isDiscrete = 1;
        skeletonInherit = "Parent";
        skeletonBones[] = {"Hand", "head", "SPINE", "Pelvis"};
    };
    class Plain: Parent {};
};
class CfgModels { class Default {}; };
'''


def bones(*pairs):
    return [pair for pair in pairs]


class TestParser(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_cfg(TEXT)

    def test_skeleton_classes_are_listed_without_references(self):
        self.assertEqual(skeleton_names(self.cfg), ["Parent", "Child", "Plain"])

    def test_comments_and_strings_are_read(self):
        parent = resolve_skeleton(self.cfg, "Parent")
        self.assertEqual(parent.bones, [("Pelvis", ""), ("Spine", "Pelvis"), ("head", "Spine")])
        self.assertEqual(parent.is_discrete, 0)
        self.assertEqual(parent.pivots_model, "a3\\anims_f\\skeleton.p3d")

    def test_the_include_line_is_reported_not_followed(self):
        self.assertEqual(self.cfg.includes, ["\\a3\\anims_f\\config.cpp"])
        self.assertEqual(self.cfg.directives, [])

    def test_other_directives_are_listed(self):
        cfg = parse_cfg('#define X 1\nclass CfgSkeletons {};')
        self.assertEqual(cfg.directives, ["#define X 1"])

    def test_inherited_bones_come_first_and_duplicates_are_dropped(self):
        child = resolve_skeleton(self.cfg, "Child")
        self.assertEqual([b for b, _ in child.bones], ["Pelvis", "Spine", "head", "Hand"])
        self.assertEqual(child.dropped, ["SPINE"])  # same name as an inherited bone, ignoring case
        self.assertEqual(child.inherits, ["Parent"])
        self.assertEqual(child.is_discrete, 1)

    def test_class_a_colon_b_inherits_the_base_class_properties(self):
        plain = resolve_skeleton(self.cfg, "Plain")
        self.assertEqual(len(plain.bones), 3)
        self.assertEqual(plain.pivots_model, "a3\\anims_f\\skeleton.p3d")

    def test_names_ignore_case(self):
        self.assertEqual(resolve_skeleton(self.cfg, "child").name, "Child")
        cfg = parse_cfg('class cfgskeletons { class S { skeletonbones[] = {"a", ""}; }; };')
        self.assertEqual(resolve_skeleton(cfg, "S").bones, [("a", "")])

    def test_plus_equals_adds_to_an_array(self):
        cfg = parse_cfg('class CfgSkeletons { class S { skeletonBones[] = {"a", ""}; skeletonBones[] += {"b", "a"}; }; };')
        self.assertEqual(resolve_skeleton(cfg, "S").bones, [("a", ""), ("b", "a")])

    def test_a_quote_doubled_inside_a_string(self):
        cfg = parse_cfg('class CfgSkeletons { class S { pivotsModel = "a""b"; }; };')
        self.assertEqual(resolve_skeleton(cfg, "S").pivots_model, 'a"b')

    def test_errors_say_what_is_wrong(self):
        for text, expected in (
                ("/* never closed", "comment"),
                ('class A { x = "open;\n};', "string"),
                ("class A { x = 1 };", "no ;"),
                ("class A { x = 1;", "closed"),
                ("}", "unexpected"),
        ):
            with self.assertRaises(CfgError, msg=text) as raised:
                parse_cfg(text)
            self.assertIn(expected, str(raised.exception))

    def test_odd_bone_list_and_unknown_skeleton_and_loops(self):
        cfg = parse_cfg('class CfgSkeletons { class S { skeletonBones[] = {"a"}; };'
                        ' class L { skeletonInherit = "M"; }; class M { skeletonInherit = "L"; };'
                        ' class N { skeletonInherit = "Nope"; }; };')
        for name, expected in (("S", "pairs"), ("Zzz", "not in this model.cfg"), ("L", "loops"), ("N", "Nope")):
            with self.assertRaises(CfgError, msg=name) as raised:
                resolve_skeleton(cfg, name)
            self.assertIn(expected, str(raised.exception))

    def test_a_binarized_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "model.cfg")
            with open(path, "wb") as file:
                file.write(b"\0raP" + b"\0" * 20)
            with self.assertRaises(CfgError) as raised:
                read_cfg_file(path)
            self.assertIn("binarized", str(raised.exception))
        with self.assertRaises(CfgError):
            read_cfg_file(os.path.join(folder, "missing.cfg"))


class TestBoneChecks(unittest.TestCase):
    def test_good_list(self):
        self.assertEqual(check_bones([("a", ""), ("b", "a"), ("c", "b")]), [])

    def test_duplicate_name_differing_only_in_case(self):
        problems = check_bones([("Head", ""), ("head", "Head")])
        self.assertEqual(len(problems), 1)
        self.assertIn("listed twice", problems[0])

    def test_child_before_parent_and_unknown_parent(self):
        problems = check_bones([("b", "a"), ("a", ""), ("c", "zzz")])
        self.assertTrue(any("before its parent" in text for text in problems))
        self.assertTrue(any("not a bone" in text for text in problems))

    def test_name_length_and_ascii(self):
        self.assertEqual(check_bones([("x" * 31, "")]), [])
        self.assertIn("32 characters", " ".join(check_bones([("x" * 32, "")])))
        self.assertIn("ASCII", " ".join(check_bones([("kostě", "")])))


class TestPlan(unittest.TestCase):
    BONES = [("Pelvis", ""), ("Spine", "Pelvis"), ("head", "Spine"), ("Face_Jaw", "head"), ("EyeLeft", "head"),
             ("Camera", "head"), ("Hand", "Camera"), ("Leg", "Pelvis")]
    PIVOTS = {"pelvis": (0.0, 1.0, 0.0), "Spine": (0.0, 1.5, 0.0), "Head": (0.0, 1.7, 0.1), "Leg": (0.1, 0.5, 0.0),
              "Hips": (0.0, 0.0, 0.0), "Hand": (0.5, 1.4, 0.0), "Face_Jaw": (0.0, 1.6, 0.1)}

    def test_rules(self):
        plan = plan_skeleton(self.BONES, self.PIVOTS)
        self.assertEqual([j.name for j in plan.joints], ["Pelvis", "Spine", "head", "Hand", "Leg"])  # model.cfg spelling
        self.assertEqual(plan.skipped, ["Face_Jaw", "EyeLeft"])
        self.assertEqual(plan.missing, ["Camera"])
        self.assertEqual(plan.unknown, ["Hips"])
        self.assertEqual(plan.bone_count, 8)
        self.assertEqual(plan.joints[2].position, (0.0, 1.7, 0.1))  # matched ignoring case

    def test_parents_come_first_and_a_left_out_parent_is_replaced_by_the_one_above(self):
        plan = plan_skeleton(self.BONES, self.PIVOTS)
        for index, joint in enumerate(plan.joints):
            self.assertLess(joint.parent, index)
        hand = plan.joints[3]
        self.assertEqual(plan.joints[hand.parent].name, "head")  # Camera has no pivot
        self.assertEqual(plan.reparented, ["Hand"])
        self.assertEqual(plan.joints[0].parent, -1)

    def test_pivot_is_the_first_point_of_the_memory_selection(self):
        model = p3d.P3DModel([])
        lod = p3d.LOD()
        lod.set_lod_type(p3d.LodType.MEMORY)
        first = lod.add_point((1.0, 2.0, 3.0))
        second = lod.add_point((9.0, 9.0, 9.0))
        lod.add_selection("Spine", {second: 1.0, first: 1.0})
        lod.add_selection("empty")
        model.lods.append(lod)
        self.assertEqual(pivot_positions(model), {"Spine": (1.0, 2.0, 3.0)})
        with self.assertRaises(CfgError):
            pivot_positions(p3d.P3DModel([p3d.LOD()]))


@unittest.skipUnless(samples_support.AVAILABLE, samples_support.SKIP_REASON)
class TestSamples(unittest.TestCase):
    """Design test 1.1 without Houdini, and 1.2 on both Samples model.cfg files."""

    CFG = "Addons/Test_Character_01/model.cfg"
    PIVOTS = "Addons/TemplateRTM/ManSkeleton_Pivots.p3d"

    def test_the_picker_lists_the_classes_of_both_samples_cfg_files(self):
        sample = read_cfg_file(samples_support.path(self.CFG))
        self.assertIn("OFP2_ManSkeleton", skeleton_names(sample))
        template = read_cfg_file(samples_support.path("Addons/TemplateRTM/model.cfg"))
        self.assertIn("ManSkeleton", skeleton_names(template))

    def test_the_sample_skeleton_gives_64_joints(self):
        skeleton = resolve_skeleton(read_cfg_file(samples_support.path(self.CFG)), "OFP2_ManSkeleton")
        self.assertEqual(check_bones(skeleton.bones), [])
        self.assertEqual(len(skeleton.bones), 103)
        pivots = read_pivots_file(samples_support.path(self.PIVOTS))
        plan = plan_skeleton(skeleton.bones, pivots)
        self.assertEqual(len(plan.joints), 64)
        self.assertEqual(sorted(plan.missing), ["Camera", "launcher", "weapon"])
        self.assertEqual(plan.unknown, ["Hips"])
        self.assertEqual(len(plan.skipped), 36)
        self.assertFalse([j for j in plan.joints if j.name.startswith(("Face_", "Eye"))])
        for index, joint in enumerate(plan.joints):
            self.assertLess(joint.parent, index)
        self.assertEqual(plan.reparented, [])


if __name__ == "__main__":
    unittest.main()

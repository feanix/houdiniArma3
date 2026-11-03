"""Node QA parts D and E in hython: the Material Assign and Proxy nodes as Houdini builds them from the HDAs.

Checks the parameter kinds (file pickers, colour with alpha), which parameters are hidden or disabled in each state, that
every Texture Source still writes the same ``a3ob_texture``, that an old scene keeps its colour, and that a picked
absolute proxy path gives the same name as typing it. Needs ``hou``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import tempfile
import unittest

from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_node_proxy import PATH, cube_geometry

TEXTURE_PARMS = ("texsource", "texture", "texcolor", "colortype", "raw", "commonproc")


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestMaterialAssignNode(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.node = self.network.createNode("a3ob_material_assign")
        self.node.setInput(0, make_source(self.network, cube_geometry()))

    def template(self, name):
        return self.node.parmTemplateGroup().find(name)

    def state(self, name):
        self.node.updateParmStates()
        parm = self.node.parm(name) or self.node.parmTuple(name)[0]
        return parm.isHidden(), parm.isDisabled()

    def texture_values(self):
        result = cook(self.node)
        self.assertEqual(result.errors, ())
        return set(self.node.geometry().primStringAttribValues("a3ob_texture"))

    def test_file_pickers_and_colour_kind(self):
        texture = self.template("texture")
        self.assertEqual(texture.stringType(), hou.stringParmType.FileReference)
        self.assertEqual(texture.tags()["filechooser_mode"], "read")
        self.assertEqual(texture.tags()["filechooser_pattern"], "*.paa *.tga *.png")
        material = self.template("material")
        self.assertEqual(material.stringType(), hou.stringParmType.FileReference)
        self.assertEqual(material.tags()["filechooser_pattern"], "*.rvmat")
        color = self.template("texcolor")
        self.assertEqual((color.numComponents(), color.look(), color.namingScheme()),
                         (4, hou.parmLook.ColorSquare, hou.parmNamingScheme.RGBA))

    def test_renamed_labels(self):
        self.assertEqual(self.template("texsource").menuLabels()[2], "Custom texture text")
        self.assertEqual(self.template("raw").label(), "Texture Text")
        self.assertEqual(self.template("commonproc").label(), "Pick a Common Texture")
        self.assertEqual(self.template("commonmat").label(), "Pick a Common Material")

    def test_visibility_follows_the_texture_source(self):
        shown = {"file": {"texture"}, "color": {"texcolor", "colortype"}, "custom": {"raw", "commonproc"}}
        for source, names in shown.items():
            self.node.parm("texsource").set(source)
            for name in TEXTURE_PARMS[1:]:
                self.assertEqual(self.state(name)[0], name not in names, (source, name))

    def test_set_texture_and_set_material_disable_their_fields(self):
        for source in ("file", "color", "custom"):
            self.node.parm("texsource").set(source)
            for on in (1, 0):
                self.node.parm("settex").set(on)
                for name in TEXTURE_PARMS:
                    self.assertEqual(self.state(name)[1], not on, (source, on, name))
        for on in (1, 0):
            self.node.parm("setmat").set(on)
            for name in ("material", "commonmat"):
                self.assertEqual(self.state(name), (False, not on), (on, name))
        self.assertEqual(self.state("settex"), (False, False))
        self.assertEqual(self.state("setmat"), (False, False))

    def test_each_source_writes_the_same_texture_as_before(self):
        self.node.parm("setmat").set(0)
        self.node.parm("texture").set("a3/box/lid_co.paa")
        self.assertEqual(self.texture_values(), {"a3\\box\\lid_co.paa"})
        self.node.parm("texsource").set("color")
        self.node.parmTuple("texcolor").set((0.25, 0.5, 0.75, 1.0))
        self.node.parm("colortype").set("CA")
        self.assertEqual(self.texture_values(), {"#(argb,8,8,3)color(0.250,0.500,0.750,1.000,CA)"})
        self.node.parm("texsource").set("custom")
        self.node.parm("raw").set("#(argb,8,8,3)color(1,0,0,1,CO)")
        self.assertEqual(self.texture_values(), {"#(argb,8,8,3)color(1,0,0,1,CO)"})

    def old_colour_node(self):
        self.node.parm("setmat").set(0)
        self.node.parm("texsource").set("color")
        self.node.parmTuple("color").set((0.25, 0.5, 0.75, 1.0))  # what a scene saved with the old Color holds
        return self.node

    def test_the_cook_no_longer_reads_the_old_colour(self):
        self.old_colour_node()
        self.assertEqual(self.texture_values(), {"#(argb,8,8,3)color(1.000,1.000,1.000,1.000,CO)"})
        self.assertTrue(self.state("color")[0])  # the old fields never show

    def test_loading_a_scene_moves_the_old_colour_into_the_picker(self):
        # #32: saved in a hip and loaded again, OnLoaded moves it; then a white typed later counts
        path = self.old_colour_node().path()
        hip = os.path.join(tempfile.mkdtemp(prefix="a3ob_material_hip_"), "old.hip")
        hou.hipFile.save(hip)
        hou.hipFile.load(hip, suppress_save_prompt=True)
        node = hou.node(path)
        self.network = node.parent()  # the old reference is gone after a load
        self.node = node
        self.assertEqual(tuple(node.parmTuple("texcolor").eval()), (0.25, 0.5, 0.75, 1.0))
        self.assertEqual(tuple(node.parmTuple("color").eval()), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(self.texture_values(), {"#(argb,8,8,3)color(0.250,0.500,0.750,1.000,CO)"})
        node.parmTuple("texcolor").set((1.0, 1.0, 1.0, 1.0))
        self.assertEqual(self.texture_values(), {"#(argb,8,8,3)color(1.000,1.000,1.000,1.000,CO)"})


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestProxyPathPicker(NodeTestCase):
    def proxy_path(self, text):
        node = self.network.createNode("a3ob_proxy")
        node.setInput(0, make_source(self.network, cube_geometry()))
        node.parm("path").set(text)
        self.assertEqual(cook(node).errors, ())
        prims = [p for p in node.geometry().prims() if isinstance(p, hou.PackedPrim)]
        return prims[0].attribValue("a3ob_proxy_path")

    def test_path_is_a_p3d_file_picker(self):
        node = self.network.createNode("a3ob_proxy")
        template = node.parmTemplateGroup().find("path")
        self.assertEqual(template.stringType(), hou.stringParmType.FileReference)
        self.assertEqual(template.tags()["filechooser_mode"], "read")
        self.assertEqual(template.tags()["filechooser_pattern"], "*.p3d")

    def test_a_picked_absolute_path_gives_the_name_of_the_typed_one(self):
        typed = self.proxy_path("P:\\a3\\data_f\\proxies\\weapon_slots\\top")
        picked = self.proxy_path("P:/a3/data_f/proxies/weapon_slots/top.p3d")
        self.assertEqual(picked, typed)
        self.assertEqual(self.proxy_path("P:\\a3\\data_f\\proxies\\weapon_slots\\top.p3d"), typed)
        self.assertEqual(self.proxy_path(PATH.replace("\\", "/") + ".p3d"), PATH)

    def test_export_makes_the_picked_path_relative(self):
        from a3ob_houdini.p3d_export.options import relative_proxy_name
        picked = self.proxy_path("P:/a3/data_f/proxies/weapon_slots/top.p3d")
        self.assertEqual(relative_proxy_name("proxy:%s.001" % picked, "P:\\"),
                         "proxy:a3\\data_f\\proxies\\weapon_slots\\top.001")


if __name__ == "__main__":
    unittest.main()

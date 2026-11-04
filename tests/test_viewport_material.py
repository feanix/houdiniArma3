"""Material Assign "Show in Viewport" and the .paa ending on export (task #54, D29).

The text rules run under plain Python; the material tests cook the real asset in hython.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from a3ob_core import paa, png
from a3ob_houdini import entry, material_text, paa_cache, viewport_material
from a3ob_houdini.p3d_export import ExportOptions
from tests.hou_support import HAS_HOU, hou
from tests.mass_proxy_support import NodeTestCase, cook, make_source
from tests.test_node_proxy import cube_geometry
from tests.test_paa import block_dxt1, make_paa

RED = bytes([255, 0, 0, 255])
GREEN = bytes([0, 255, 0, 255])
BLUE = bytes([0, 0, 255, 255])
WHITE = bytes([255, 255, 255, 255])


class Folder(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="a3ob_viewport_test_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.cache = os.path.join(self.folder, "cache")

    def path(self, *parts):
        return os.path.join(self.folder, *parts)

    def make_png(self, *parts):
        target = self.path(*parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        png.write_png(target, 2, 2, RED + GREEN + BLUE + WHITE)
        return target

    def make_paa(self, *parts):
        target = self.path(*parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as file:
            file.write(make_paa(paa.TYPE_DXT1, 4, 4, block_dxt1(0xF800, 0xF800, [0] * 16)))
        return target


class TestPaaEnding(unittest.TestCase):
    def test_other_endings_become_paa(self):
        for text in ("camo1_co.png", "a\\b\\camo1_co.tga", "P:\\x\\y.TIF", "x.jpg"):
            self.assertTrue(material_text.paa_texture(text).endswith(".paa"), text)
        self.assertEqual(material_text.paa_texture("a\\b\\camo1_co.png"), "a\\b\\camo1_co.paa")

    def test_paa_procedural_empty_and_no_ending_stay(self):
        for text in ("a\\b.paa", "A\\B.PAA", "", "#(argb,8,8,3)color(1,0,0,1,CO)", "a.b\\noending"):
            self.assertEqual(material_text.paa_texture(text), text)


class TestResolve(Folder):
    def resolve(self, text, root=None):
        return viewport_material.resolve(text, root or self.path("root"), self.cache)

    def test_image_is_used_directly(self):
        image = self.make_png("tex", "a_co.png")
        shown = self.resolve(image)
        self.assertEqual((shown.kind, shown.path), ("image", image))

    def test_paa_prefers_sibling_images_in_order(self):
        paa_file = self.make_paa("tex", "b_co.paa")
        tga = self.make_png("tex", "b_co.tga")
        self.assertEqual(self.resolve(paa_file).path, tga)
        png_file = self.make_png("tex", "b_co.png")
        self.assertEqual(self.resolve(paa_file).path, png_file)

    def test_paa_without_sibling_uses_the_cache(self):
        paa_file = self.make_paa("tex", "c_co.paa")
        shown = self.resolve(paa_file)
        self.assertEqual(shown.kind, "image")
        self.assertTrue(shown.path.startswith(self.cache) and shown.path.endswith(".png"))
        self.assertTrue(os.path.isfile(shown.path))

    def test_bad_paa_and_missing_file(self):
        bad = self.path("bad_co.paa")
        with open(bad, "wb") as file:
            file.write(b"nonsense")
        self.assertEqual(self.resolve(bad).kind, "failed")
        self.assertEqual(self.resolve(self.path("none_co.png")).kind, "missing")

    def test_colour_custom_and_empty(self):
        shown = self.resolve("#(argb,8,8,3)color(1.000,0.500,0.000,1.000,CO)")
        self.assertEqual((shown.kind, shown.color), ("color", (1.0, 0.5, 0.0)))
        self.assertEqual(self.resolve("#(ai,64,64,1)perlinNoise(4,4,0.5,0.5)").kind, "custom")
        self.assertEqual(self.resolve("a\\b.rvmat").kind, "custom")
        self.assertEqual(self.resolve("").kind, "empty")

    def test_ingame_path_is_found_under_the_project_root(self):
        self.make_png("root", "mod", "x_co.png")
        shown = self.resolve("mod\\x_co.paa")  # the sibling next to the (missing) .paa under the root
        self.assertEqual(shown.kind, "image")
        self.assertEqual(self.resolve("mod\\gone_co.paa").kind, "ingame")


RED565, BLUE565 = 0xF800, 0x001F


def write_paa(target, color):
    with open(target, "wb") as file:
        file.write(make_paa(paa.TYPE_DXT1, 4, 4, block_dxt1(color, color, [0] * 16)))


class TestForce(Folder):
    def test_force_decodes_again_when_the_key_matches(self):
        source = self.make_paa("tex", "f_co.paa")
        cached = paa_cache.cached_image(source, self.cache)
        with open(cached, "wb") as file:
            file.write(b"stale")
        self.assertEqual(paa_cache.cached_image(source, self.cache), cached)
        self.assertEqual(open(cached, "rb").read(), b"stale")  # no force: the entry is used as it is
        self.assertEqual(paa_cache.cached_image(source, self.cache, force=True), cached)
        width, height, rgba = paa.decode_paa(source)
        self.assertEqual(open(cached, "rb").read(), png.encode_png(width, height, rgba))


@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestViewportNode(NodeTestCase, Folder):
    def setUp(self):
        Folder.setUp(self)
        NodeTestCase.setUp(self)
        settings_file = self.path("settings.json")
        with open(settings_file, "w") as file:
            json.dump({"paa_cache_dir": self.cache, "project_root": self.path("root")}, file)
        patcher = mock.patch.dict(os.environ, {"A3OB_SETTINGS": settings_file, "A3OB_PROJECT_ROOT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        NodeTestCase.tearDown(self)

    def assign(self, texture="", source=None, **parms):
        geo = source if source is not None else cube_geometry()
        node = self.network.createNode("a3ob_material_assign")
        node.setInput(0, make_source(self.network, geo))
        parms.setdefault("setmat", 0)
        if texture:
            parms["texture"] = texture.replace("\\", "/")
        for name, value in parms.items():
            (node.parmTuple(name) if isinstance(value, tuple) else node.parm(name)).set(value)
        return node

    def paths(self, node):
        attrib = node.geometry().findPrimAttrib("shop_materialpath")
        return None if attrib is None else set(node.geometry().primStringAttribValues("shop_materialpath"))

    def material(self, node):
        (path,) = self.paths(node)
        found = hou.node(path)
        self.assertIsNotNone(found, path)
        self.assertEqual(found.type().name(), "principledshader::2.0")
        self.assertEqual(found.parent().name(), viewport_material.MATNET_NAME)
        self.assertEqual(found.parent().parent(), self.network)
        return found

    def test_png_texture_parm_points_at_the_file(self):
        image = self.make_png("tex", "wall_co.png")
        node = self.assign(image)
        self.assertEqual(cook(node).errors, ())
        material = self.material(node)
        self.assertEqual(material.name(), "wall_co")
        self.assertEqual(material.evalParm("basecolor_useTexture"), 1)
        self.assertEqual(material.evalParm("basecolor_texture"), image.replace("\\", "/"))

    def test_texture_material_is_not_tinted_and_reuse_heals(self):
        node = self.assign(self.make_png("tex", "tint_co.png"))
        cook(node)
        material = self.material(node)
        self.assertEqual(tuple(material.evalParmTuple("basecolor")), (1.0, 1.0, 1.0))
        material.parmTuple("basecolor").set((0.2, 0.2, 0.2))  # as the old code left it
        node.cook(force=True)
        self.assertEqual(self.material(node), material)
        self.assertEqual(tuple(material.evalParmTuple("basecolor")), (1.0, 1.0, 1.0))

    def test_paa_uses_the_sibling_png(self):
        paa_file = self.make_paa("tex", "d_co.paa")
        image = self.make_png("tex", "d_co.png")
        node = self.assign(paa_file)
        cook(node)
        self.assertEqual(self.material(node).evalParm("basecolor_texture"), image.replace("\\", "/"))

    def test_paa_without_sibling_uses_the_cache_file(self):
        paa_file = self.make_paa("tex", "e_co.paa")
        node = self.assign(paa_file)
        self.assertEqual(cook(node).errors, ())
        self.assertEqual(self.material(node).name(), "e_co")  # named after the .paa, not the cache file
        texture = self.material(node).evalParm("basecolor_texture")
        self.assertTrue(texture.startswith(self.cache.replace("\\", "/")), texture)
        self.assertTrue(os.path.isfile(texture))
        # a second cook reuses the material and the decode
        stamp = os.stat(texture).st_mtime_ns
        material = self.material(node)
        cook(node)
        self.assertEqual(self.material(node), material)
        self.assertEqual(os.stat(texture).st_mtime_ns, stamp)
        self.assertEqual(len(material.parent().children()), 1)

    def test_colour_procedural_is_a_flat_colour(self):
        node = self.assign(texsource="color", texcolor=(1.0, 0.5, 0.0, 1.0), colortype="CO")
        cook(node)
        material = self.material(node)
        self.assertEqual(material.evalParm("basecolor_useTexture"), 0)
        self.assertEqual(material.evalParm("basecolor_usePointColor"), 0)
        self.assertEqual(tuple(material.evalParmTuple("basecolor")), (1.0, 0.5, 0.0))

    def test_same_texture_shares_one_material(self):
        image = self.make_png("tex", "shared_co.png")
        first = self.assign(image)
        second = self.assign(image)
        cook(first)
        cook(second)
        self.assertEqual(self.paths(first), self.paths(second))
        self.assertEqual(len(self.network.node(viewport_material.MATNET_NAME).children()), 1)
        other = self.assign(self.make_png("other", "shared_co.png"))  # same name, other folder: its own material
        cook(other)
        self.assertNotEqual(self.paths(other), self.paths(first))
        self.assertEqual(len(self.network.node(viewport_material.MATNET_NAME).children()), 2)

    def test_toggle_off_leaves_shop_materialpath_alone(self):
        image = self.make_png("tex", "off_co.png")
        node = self.assign(image, showviewport=0)
        cook(node)
        self.assertIsNone(self.paths(node))
        self.assertIsNone(self.network.node(viewport_material.MATNET_NAME))
        source = cube_geometry()
        source.addAttrib(hou.attribType.Prim, "shop_materialpath", "")
        source.setPrimStringAttribValues("shop_materialpath", ["/mat/mine"] * len(source.prims()))
        node = self.assign(image, source=source, showviewport=0)
        cook(node)
        self.assertEqual(self.paths(node), {"/mat/mine"})

    def test_missing_file_warns_and_ingame_paths_give_one_info_line(self):
        node = self.assign(self.path("nothing_co.png"))
        result = cook(node)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("nothing_co.png", result.warnings[0])
        self.assertIsNone(self.paths(node))
        source = cube_geometry()
        source.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
        count = len(source.prims())
        source.setPrimStringAttribValues("a3ob_texture", ["mod\\a_co.paa", "mod\\b_co.paa"] * (count // 2))
        node = self.assign(source=source, settex=0)
        result = cook(node)
        self.assertEqual(result.warnings, ())
        info = [text for text in result.messages if "in-game" in text]
        self.assertEqual(len(info), 1)
        self.assertIn("2 textures", info[0])
        self.assertIsNone(self.paths(node))

    def test_a_texture_line_that_is_no_file_gives_an_info_line(self):
        node = self.assign(texsource="custom", raw="#(ai,64,64,1)perlinNoise(4,4,0.5,0.5)")
        result = cook(node)
        self.assertEqual(result.warnings, ())
        self.assertTrue(any("not a file or a plain colour" in text for text in result.messages))
        self.assertIsNone(self.paths(node))

    def test_removing_the_node_leaves_no_broken_reference(self):
        node = self.assign(self.make_png("tex", "gone_co.png"))
        cook(node)
        material = self.material(node)
        node.destroy()
        self.assertEqual(hou.node(material.path()), material)  # the material stays; nothing points at it

    def test_polygon_uv_samples_the_png_the_right_way_up(self):
        """The importer stores v' = 1 - v and the PNG is top-row-first; Houdini puts the top row at v = 1."""
        image = self.make_png("tex", "orient_co.png")
        net = self.network.createNode("cop2net")
        picture = net.createNode("file")
        picture.parm("filename1").set(image.replace("\\", "/"))
        file_u, file_v = 0.1, 0.1  # near the top left of the picture in a file (v runs top-down)
        houdini_v = 1.0 - file_v  # what the importer writes to uv
        red, green, blue = picture.getPixelByUV("C", file_u, houdini_v)
        self.assertGreater(red, green + 0.05)
        self.assertGreater(red, blue + 0.05)  # the top left is the red pixel: no extra V flip in the material

    def press(self, node):
        entry.button("material_assign", "reloadtex", {"node": node})

    def test_button_exists_and_depends_on_show_in_viewport(self):
        node = self.assign()
        parm = node.parm("reloadtex")
        self.assertIsNotNone(parm)
        conditions = parm.parmTemplate().conditionals()
        self.assertEqual(conditions[hou.parmCondType.DisableWhen], "{ showviewport != 1 }")

    def test_replaced_paa_gives_a_new_decoded_image(self):
        source = self.make_paa("tex", "g_co.paa")
        write_paa(source, RED565)
        node = self.assign(source)
        cook(node)
        old = self.material(node).evalParm("basecolor_texture")
        write_paa(source, BLUE565)
        os.utime(source, ns=(1, 1))  # another modification time, so the cache key changes
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
        self.assertEqual(drop.call_count, 1)
        self.assertEqual(node.errors(), ())
        new = self.material(node).evalParm("basecolor_texture")
        self.assertNotEqual(old, new)
        width, height, rgba = paa.decode_paa(source)
        with open(new, "rb") as file:
            self.assertEqual(file.read(), png.encode_png(width, height, rgba))

    def test_same_key_entry_is_rebuilt(self):
        source = self.make_paa("tex", "h_co.paa")
        node = self.assign(source)
        cook(node)
        cached = self.material(node).evalParm("basecolor_texture")
        with open(cached, "wb") as file:
            file.write(b"stale")
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
        self.assertEqual(drop.call_count, 1)
        self.assertNotEqual(open(cached, "rb").read(), b"stale")
        self.assertIn("Reloaded 1 texture.", node.messages())

    def test_png_texture_reloads_once_per_press(self):
        node = self.assign(self.make_png("tex", "i_co.png"))
        cook(node)
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
            self.press(node)
        self.assertEqual(drop.call_count, 2)

    def test_sibling_image_case(self):
        source = self.make_paa("tex", "j_co.paa")
        image = self.make_png("tex", "j_co.png")
        node = self.assign(source)
        cook(node)
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
        self.assertEqual(drop.call_count, 1)
        self.assertEqual(self.material(node).evalParm("basecolor_texture"), image.replace("\\", "/"))

    def test_nothing_shown_gives_one_info_line(self):
        node = self.assign(self.make_png("tex", "k_co.png"), showviewport=0)
        cook(node)
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
        self.assertEqual(drop.call_count, 0)
        self.assertEqual((node.errors(), node.warnings()), ((), ()))
        self.assertEqual(len([text for text in node.messages() if text.startswith("Reload Textures")]), 1)
        # on, but no texture file found
        node.parm("showviewport").set(1)
        node.parm("texture").set(self.path("none_co.png").replace("\\", "/"))
        cook(node)
        with mock.patch.object(viewport_material, "drop_viewport_images") as drop:
            self.press(node)
        self.assertEqual(drop.call_count, 0)
        self.assertEqual(node.errors(), ())
        self.assertEqual(len([text for text in node.messages() if text.startswith("Reload Textures")]), 1)

    def test_failure_shows_on_the_node_and_does_not_raise(self):
        node = self.assign(self.make_png("tex", "l_co.png"))
        cook(node)
        with mock.patch.object(viewport_material, "drop_viewport_images", side_effect=RuntimeError("boom")):
            self.press(node)  # must not raise
        self.assertTrue(any("Reload Textures failed" in text for text in node.warnings()))

    def test_the_real_drop_commands_run_headless(self):
        calls = []
        real = hou.hscript
        with mock.patch.object(hou, "hscript", side_effect=lambda text: calls.append(text) or real(text)):
            viewport_material.drop_viewport_images()
        self.assertEqual(calls, ["texcache -c", "glcache -c"])



@unittest.skipUnless(HAS_HOU, "needs hou (run under hython)")
class TestExportEnding(unittest.TestCase):
    def test_export_writes_paa_for_png_tga_tif(self):
        from tests import hou_support as hs
        from tests.test_export_roundtrip import export, lod_geometry
        _, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        edited = lod_geometry(geo, 0)
        names = ["a\\camo1_co.png", "a\\b_co.tga", "a\\c_co.tif", "a\\d_co.paa", "#(argb,8,8,3)color(1,0,0,1,CO)"]
        for number, name in enumerate(names):
            edited.prims()[number].setAttribValue("a3ob_texture", name)
        model, report, _ = export([edited], ExportOptions(paa_textures=True))
        written = model.lods[0].face_textures
        self.assertEqual(written[:5], ["a\\camo1_co.paa", "a\\b_co.paa", "a\\c_co.paa", "a\\d_co.paa", names[4]])
        self.assertEqual(report.texts("error"), [])
        # D34: with the option off (Lossless) the paths are written as they are.
        model, _, _ = export([edited])
        self.assertEqual(model.lods[0].face_textures[:5], names)

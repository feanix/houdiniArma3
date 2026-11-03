"""Task #51: Relative Paths maps files to in-game paths with $PBOPREFIX$ files and HEMTT projects (p3d_export/path_map.py).

Temp trees are built in code. One test reads the owner's real layout read-only and skips when it is absent. The last test
exports through the P3D Export node and needs hou; it skips under plain Python.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from a3ob_core import p3d
from a3ob_houdini import messages
from a3ob_houdini.p3d_export import ExportOptions, build
from a3ob_houdini.p3d_export import path_map
from a3ob_houdini.p3d_export.data import Collection
from a3ob_houdini.p3d_export.path_map import PathMapper
from tests import hou_support as hs
from tests.test_export_plan import lod_data, quad_mesh

OWNER = "E:\\Projects\\HettycKad"

if hs.HAS_HOU:
    from a3ob_houdini import lod_container
    from a3ob_houdini.nodes import p3d_export as export_node
    from tests.mass_proxy_support import NodeTestCase, make_source
else:
    NodeTestCase = unittest.TestCase


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return path


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="a3ob_pbo_"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def at(self, *parts):
        return os.path.join(self.root, *parts)


@unittest.skipUnless(os.name == "nt", "the paths are Windows drive paths")
class TestMapper(TreeCase):
    def test_pboprefix_maps_a_texture(self):
        write(self.at("addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props\n")
        mapper = PathMapper()
        got = mapper.map_path(self.at("addons", "props", "wallScreen", "camo1_co.paa"), "texture")
        self.assertEqual(got, "hyck\\ame\\addons\\props\\wallScreen\\camo1_co.paa")
        self.assertEqual(mapper.warnings, [])

    def test_prefix_file_forms(self):
        forms = {
            "plain": "hyck\\ame\\addons\\a",
            "slashes": "hyck/ame/addons/a",
            "leading backslash": "\\hyck\\ame\\addons\\a\\",
            "blank lines, CRLF, spaces": "\r\n\r\n  hyck\\ame\\addons\\a  \r\nsecond line ignored\r\n",
            "key form": "prefix=hyck\\ame\\addons\\a",
            "key form with spaces and semicolon": "Prefix = \\hyck\\ame\\addons\\a;\r\n",
        }
        for name, text in forms.items():
            with self.subTest(form=name):
                path = write(self.at("forms", name.replace(" ", "_").replace(",", ""), "$PBOPREFIX$"), text)
                self.assertEqual(path_map.read_prefix_file(path), "hyck\\ame\\addons\\a")
        empty = write(self.at("forms", "empty", "$PBOPREFIX$"), "\n  \n")
        self.assertEqual(path_map.read_prefix_file(empty), "")
        self.assertEqual(path_map.read_prefix_file(self.at("forms", "missing", "$PBOPREFIX$")), "")

    def test_nested_addons_pick_the_nearest_prefix(self):
        write(self.at("addons", "utilities", "$PBOPREFIX$"), "hyck\\ame\\addons\\utilities")
        write(self.at("addons", "utilities", "optre", "$PBOPREFIX$"), "hyck\\ame\\addons\\utilities\\optre")
        mapper = PathMapper()
        self.assertEqual(mapper.map_path(self.at("addons", "utilities", "optre", "data", "a.paa"), "texture"),
                         "hyck\\ame\\addons\\utilities\\optre\\data\\a.paa")
        self.assertEqual(mapper.map_path(self.at("addons", "utilities", "tas", "b.paa"), "texture"),
                         "hyck\\ame\\addons\\utilities\\tas\\b.paa")

    def test_an_empty_prefix_file_is_skipped_and_the_walk_goes_on(self):
        write(self.at("addons", "x", "$PBOPREFIX$"), "z\\x\\addons\\x")
        write(self.at("addons", "x", "sub", "$PBOPREFIX$"), "\n")
        got = PathMapper().map_path(self.at("addons", "x", "sub", "a.paa"), "material")
        self.assertEqual(got, "z\\x\\addons\\x\\sub\\a.paa")

    def test_hemtt_project_without_a_prefix_file(self):
        write(self.at(".hemtt", "project.toml"), 'name = "T"\nprefix = "ame"\nmainprefix = "hyck"\n')
        mapper = PathMapper()
        got = mapper.map_path(self.at("addons", "props", "wallScreen", "a.rvmat"), "material")
        self.assertEqual(got, "hyck\\ame\\addons\\props\\wallScreen\\a.rvmat")
        self.assertEqual(mapper.warnings, [])

    def test_hemtt_project_without_a_main_prefix(self):
        write(self.at(".hemtt", "project.toml"), 'prefix = "abe"\n')
        got = PathMapper().map_path(self.at("addons", "main", "a.paa"), "texture")
        self.assertEqual(got, "abe\\addons\\main\\a.paa")

    def test_pboprefix_wins_over_hemtt(self):
        write(self.at(".hemtt", "project.toml"), 'prefix = "ame"\nmainprefix = "hyck"\n')
        write(self.at("addons", "props", "$PBOPREFIX$"), "custom\\props")
        got = PathMapper().map_path(self.at("addons", "props", "a.paa"), "texture")
        self.assertEqual(got, "custom\\props\\a.paa")

    def test_hemtt_file_outside_addons_falls_through_with_a_warning(self):
        write(self.at(".hemtt", "project.toml"), 'prefix = "ame"\nmainprefix = "hyck"\n')
        mapper = PathMapper()
        path = self.at("sources", "a.paa")
        got = mapper.map_path(path, "texture", "LOD 1")
        self.assertEqual(got, path[3:])  # only the drive letter and its slash go
        self.assertEqual(len(mapper.warnings), 1)
        self.assertIn(path, mapper.warnings[0])
        self.assertIn("project root", mapper.warnings[0])
        self.assertIn("$PBOPREFIX$", mapper.warnings[0])

    def test_project_root_then_drive_strip(self):
        path = self.at("tex", "a.paa")
        mapper = PathMapper(self.root)
        self.assertEqual(mapper.map_path(path, "texture"), "tex\\a.paa")
        self.assertEqual(mapper.warnings, [])  # the project root matched: no warning
        bare = PathMapper("")
        self.assertEqual(bare.map_path(path, "texture"), path[3:])
        bare.map_path(path.upper(), "texture")  # the same path again is warned about once
        self.assertEqual(len(bare.warnings), 1)

    def test_a_path_without_a_drive_is_written_as_typed(self):
        mapper = PathMapper(self.root)
        self.assertEqual(mapper.map_path("hyck\\ame\\addons\\props\\a.paa", "texture"), "hyck\\ame\\addons\\props\\a.paa")
        self.assertEqual(mapper.map_path("\\a3\\x\\a.paa", "texture"), "a3\\x\\a.paa")
        self.assertEqual(mapper.map_path("", "texture"), "")
        self.assertEqual(mapper.warnings, [])
        self.assertIsNone(mapper.info_line())

    def test_a_proxy_path_drops_p3d_after_mapping(self):
        write(self.at("addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props")
        mapper = PathMapper()
        self.assertEqual(mapper.map_proxy_name("proxy:%s.001" % self.at("addons", "props", "p", "seat.p3d"), None),
                         "proxy:hyck\\ame\\addons\\props\\p\\seat.001")
        self.assertEqual(mapper.map_proxy_name("head", None), "head")

    def test_folders_are_looked_up_once_per_export(self):
        write(self.at("addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props")
        mapper = PathMapper()
        mapper.map_path(self.at("addons", "props", "a", "1.paa"), "texture")
        with mock.patch.object(path_map.os.path, "isfile", side_effect=AssertionError("looked again")):
            mapper.map_path(self.at("addons", "props", "a", "2.paa"), "texture")
            mapper.map_path(self.at("addons", "props", "a", "3.rvmat"), "material")

    def test_the_info_line(self):
        write(self.at("addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props")
        mapper = PathMapper()
        for name in ("1.paa", "2.paa", "2.paa", "3.paa"):
            mapper.map_path(self.at("addons", "props", name), "texture")
        for name in ("a.rvmat", "b.rvmat"):
            mapper.map_path(self.at("addons", "props", name), "material")
        self.assertEqual(mapper.info_line(),
                         "Paths mapped with addons\\props\\$PBOPREFIX$ (hyck\\ame\\addons\\props): "
                         "3 textures, 2 materials.")

    def test_the_owner_layout_read_only(self):
        if not os.path.isfile(OWNER + "\\addons\\props\\$PBOPREFIX$"):
            self.skipTest("the owner's project is not on this machine")
        mapper = PathMapper()
        got = mapper.map_path(OWNER + "\\addons\\props\\wallScreen\\camo1_co.paa", "texture")
        self.assertEqual(got, "hyck\\ame\\addons\\props\\wallScreen\\camo1_co.paa")


@unittest.skipUnless(os.name == "nt", "the paths are Windows drive paths")
class TestInTheBuild(TreeCase):
    def setUp(self):
        super().setUp()
        write(self.at("addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props")
        write(self.at("loose", "x.txt"), "")

    def build_one(self, options):
        mesh = quad_mesh()
        mesh.polys[0].texture = self.at("addons", "props", "wallScreen", "camo1_co.paa")
        mesh.polys[0].material = self.at("addons", "props", "wallScreen", "wall.rvmat")
        mesh.polys[1].texture = self.at("loose", "b_co.paa")
        mesh.polys[1].proxy = (self.at("addons", "props", "proxies", "seat.p3d"), 2)
        report = messages.Report()
        model = build(Collection([lod_data(mesh)]), options, report)
        return model, report

    def test_relative_paths_map_textures_materials_and_proxies(self):
        model, report = self.build_one(ExportOptions(relative_paths=True, lowercase=True))
        self.assertEqual(report.texts(messages.ERROR), [])
        lod = model.lods[0]
        self.assertIn("hyck\\ame\\addons\\props\\wallscreen\\camo1_co.paa", lod.face_textures)
        self.assertIn("hyck\\ame\\addons\\props\\wallscreen\\wall.rvmat", lod.face_materials)
        self.assertIsNotNone(lod.selection("proxy:hyck\\ame\\addons\\props\\proxies\\seat.002"))
        info = [text for text in report.texts(messages.INFO) if text.startswith("Paths mapped with")]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0], "Paths mapped with addons\\props\\$PBOPREFIX$ (hyck\\ame\\addons\\props): "
                                  "1 texture, 1 material, 1 proxy.")
        warned = [text for text in report.texts(messages.WARNING) if "matches no $PBOPREFIX$" in text]
        self.assertEqual(len(warned), 1)  # the loose texture only
        self.assertIn(self.at("loose", "b_co.paa"), warned[0])

    def test_the_project_root_catches_the_loose_file_without_a_warning(self):
        model, report = self.build_one(ExportOptions(relative_paths=True, project_root=self.at("loose")))
        self.assertIn("b_co.paa", model.lods[0].face_textures)
        self.assertEqual([text for text in report.texts(messages.WARNING) if "$PBOPREFIX$" in text], [])

    def test_relative_paths_off_writes_the_paths_unchanged(self):
        model, report = self.build_one(ExportOptions())
        lod = model.lods[0]
        self.assertIn(self.at("addons", "props", "wallScreen", "camo1_co.paa"), lod.face_textures)
        self.assertIn(self.at("loose", "b_co.paa"), lod.face_textures)
        self.assertEqual([text for text in report.texts(messages.INFO) if "Paths mapped" in text], [])
        self.assertEqual([text for text in report.texts(messages.WARNING) if "$PBOPREFIX$" in text], [])


@unittest.skipUnless(hs.HAS_HOU and os.name == "nt", "needs hou (run under hython)")
class TestThroughTheExportNode(NodeTestCase):
    def test_save_to_disk_writes_the_mapped_paths(self):
        folder = os.path.realpath(tempfile.mkdtemp(prefix="a3ob_pbo_node_"))
        self.addCleanup(shutil.rmtree, folder, True)
        write(os.path.join(folder, "addons", "props", "$PBOPREFIX$"), "hyck\\ame\\addons\\props")
        texture = os.path.join(folder, "addons", "props", "wallScreen", "camo1_co.paa")
        _, geo, _, _ = hs.imported_fixture("sample_2_crate.p3d")
        import hou
        lod = hou.Geometry()
        lod.merge(lod_container.lod_content(geo.prims()[0]))
        for prim in lod.prims():
            prim.setAttribValue("a3ob_texture", texture)
        node = self.network.createNode("a3ob_p3d_export")
        node.setInput(0, make_source(self.network, lod))
        target = os.path.join(folder, "out.p3d")
        node.parm("file").set(target.replace("\\", "/"))
        node.parm("relpaths").set(1)
        report = export_node.save(node)
        with open(target, "rb") as handle:
            model = p3d.read_p3d(handle.read(), strict=False)
        self.assertEqual(set(model.lods[0].face_textures), {"hyck\\ame\\addons\\props\\wallScreen\\camo1_co.paa"})
        self.assertTrue(any(text.startswith("Paths mapped with") for text in report.texts("info")), report.texts("info"))


if __name__ == "__main__":
    unittest.main()

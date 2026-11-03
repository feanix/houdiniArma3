"""Run inside a child hython by tests/test_hda_export.py. Not a test file (the name does not start with ``test``).

The parent sets HOUDINI_PATH (it includes <repo>/houdini) and PYTHONPATH (<repo>/python) for this process only, so the
Validate and P3D Export assets load from houdini/otls as they do for a user. The script presses Save to Disk with
``pressButton`` and prints one line ``A3OB_RESULT <json>``.

Usage: hython hda_child_check_export.py <fixtures folder> <scratch folder>

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import sys

import hou

PENTAGON = """
geo = hou.pwd().geometry()
poly = geo.createPolygon()
for point in geo.points()[:5]:
    poly.addVertex(point)
"""


NO_MASS = """
geo = hou.pwd().geometry()
for attrib in list(geo.pointAttribs()):
    if attrib.name() in ("mass", "a3ob_mass"):
        attrib.destroy()
"""


def cook(node):
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    return {"errors": list(node.errors()), "warnings": list(node.warnings()), "messages": list(node.messages())}


def type_info(name):
    node_type = hou.nodeType(hou.sopNodeTypeCategory(), name)
    definition = node_type.definition()
    return {
        "label": node_type.description(),
        "inputs": [node_type.minNumInputs(), node_type.maxNumInputs()],
        "parms": [template.name() for template in node_type.parmTemplates()],
        "pythoncook": definition.sections()["PythonCook"].contents().strip(),
        "library": definition.libraryFilePath().replace("\\", "/"),
        "submenu": [list(tool.toolMenuLocations()) for tool in hou.shelves.tools().values()
                    if tool.name() == "sop_" + name],
    }


def npoints(node):
    """Points of the output; a red node has no geometry at all."""
    geo = node.geometry()
    return 0 if geo is None else len(geo.points())


def read(path):
    with open(path, "rb") as file:
        return file.read()


def lod_chain(container, importer, number, tag):
    """One LOD of the import as a LOD geometry: Blast keeps the prim, Unpack opens it."""
    blast = container.createNode("blast", "keep_%s" % tag)
    blast.setInput(0, importer)
    blast.parm("group").set(str(number))
    blast.parm("grouptype").set(4)
    blast.parm("negate").set(1)
    unpack = container.createNode("unpack", "unpack_%s" % tag)
    unpack.setInput(0, blast)
    return unpack


def export_node(container, source, path, name):
    node = container.createNode("a3ob_p3d_export", name)
    node.setInput(0, source)
    node.parm("file").set(path)
    return node


def main(fixtures, scratch):
    result = {"types": {name: type_info(name) for name in ("a3ob_validate", "a3ob_p3d_export")}}
    container = hou.node("/obj").createNode("geo", "container")

    # L0: Import > Export, press Save to Disk, byte-identical.
    for key, name in (("crate", "sample_2_crate.p3d"), ("character", "sample_1_character.p3d")):
        importer = container.createNode("a3ob_p3d_import", "import_" + key)
        importer.parm("file").set(os.path.join(fixtures, name))
        folder = os.path.join(scratch, key)
        os.makedirs(folder)
        target = os.path.join(folder, "out.p3d")
        node = export_node(container, importer, target, "export_" + key)
        before = cook(node)
        result[key + "_cook"] = before
        result[key + "_cook_wrote"] = os.path.exists(target)
        node.parm("save").pressButton()
        after = {"errors": list(node.errors()), "warnings": list(node.warnings()), "messages": list(node.messages())}
        result[key + "_save"] = after
        result[key + "_identical"] = read(target) == read(os.path.join(fixtures, name))
        result[key + "_files"] = sorted(os.listdir(folder))
        result[key + "_prims"] = [len(node.geometry().prims()), len(importer.geometry().prims())]

    # Backup: saving again keeps the old file as .bak.
    crate_node = hou.node("/obj/container/export_crate")
    crate_node.parm("backup").set(1)
    crate_node.parm("save").pressButton()
    result["backup_files"] = sorted(os.listdir(os.path.join(scratch, "crate")))
    crate_node.parm("backup").set(0)

    # A failing case: a polygon with 5 sides in LOD 0 of the crate.
    importer = hou.node("/obj/container/import_crate")
    unpack = lod_chain(container, importer, 0, "bad")
    pentagon = container.createNode("python", "pentagon")
    pentagon.setInput(0, unpack)
    pentagon.parm("python").set(PENTAGON)
    folder = os.path.join(scratch, "bad")
    os.makedirs(folder)
    target = os.path.join(folder, "bad.p3d")
    with open(target, "wb") as file:
        file.write(b"old file")
    bad = export_node(container, pentagon, target, "export_bad")
    result["bad_cook"] = cook(bad)
    result["bad_geometry_points"] = npoints(bad)
    bad.parm("save").pressButton()
    result["bad_save"] = {"errors": list(bad.errors()), "warnings": list(bad.warnings()), "messages": list(bad.messages())}
    result["bad_files"] = sorted(os.listdir(folder))
    result["bad_old_file_kept"] = read(target) == b"old file"
    from a3ob_houdini import nodes
    from a3ob_houdini.errors import UserError
    try:
        nodes.load("p3d_export").save(bad)
        result["script_save"] = "no error"
    except UserError as ex:
        result["script_save"] = str(ex)
    missing = export_node(container, pentagon, os.path.join(scratch, "bad2", "x.p3d"), "export_bad2")
    missing.parm("save").pressButton()
    result["bad2_exists"] = os.path.exists(os.path.join(scratch, "bad2"))
    result["bad2_errors"] = list(missing.errors())

    # Validate and Export give the same messages. Validate: error level and warning level.
    validate = container.createNode("a3ob_validate", "validate_bad")
    validate.setInput(0, pentagon)
    result["validate_bad"] = cook(validate)
    result["validate_bad_points"] = npoints(validate)
    validate.parm("errorlevel").set("warning")
    result["validate_bad_soft"] = cook(validate)
    result["validate_bad_soft_points"] = npoints(validate)
    soft_export = export_node(container, pentagon, os.path.join(folder, "soft.p3d"), "export_soft")
    soft_export.parm("errorlevel").set("warning")
    result["export_bad_soft"] = cook(soft_export)
    soft_export.parm("save").pressButton()
    result["export_bad_soft_after_save"] = {"errors": list(soft_export.errors()), "warnings": list(soft_export.warnings())}
    result["soft_exists"] = os.path.exists(os.path.join(folder, "soft.p3d"))

    good_validate = container.createNode("a3ob_validate", "validate_crate")
    good_validate.setInput(0, importer)
    result["validate_crate"] = cook(good_validate)
    result["validate_crate_points"] = npoints(good_validate)
    result["validate_crate_prims"] = [len(good_validate.geometry().prims()), len(importer.geometry().prims())]
    good_validate.parm("typerules").set(0)
    result["validate_crate_rules_off"] = cook(good_validate)
    good_validate.parm("typerules").set(1)
    good_validate.parm("strict").set(1)
    result["validate_crate_strict"] = cook(good_validate)
    good_validate.parm("strict").set(0)
    good_validate.parm("maxmessages").set(1)
    result["validate_crate_two"] = cook(good_validate)

    # D16: the crate's Buoyancy LOD has no mass and no mass warning. The Geometry LOD (index 3) without mass warns.
    geometry = lod_chain(container, importer, 3, "geometry")
    no_mass = container.createNode("python", "no_mass")
    no_mass.setInput(0, geometry)
    no_mass.parm("python").set(NO_MASS)
    mass_validate = container.createNode("a3ob_validate", "validate_no_mass")
    mass_validate.setInput(0, no_mass)
    result["validate_no_mass"] = cook(mass_validate)
    mass_validate.parm("t2mass").set(0)
    result["validate_no_mass_mass_off"] = cook(mass_validate)

    # Strict export writes nothing when there are warnings.
    strict_folder = os.path.join(scratch, "strict")
    os.makedirs(strict_folder)
    strict = export_node(container, importer, os.path.join(strict_folder, "s.p3d"), "export_strict")
    strict.parm("strict").set(1)
    strict.parm("save").pressButton()
    result["strict_files"] = os.listdir(strict_folder)
    result["strict_errors"] = list(strict.errors())

    # Check when saving: the cook does not check.
    late = export_node(container, importer, os.path.join(scratch, "late.p3d"), "export_late")
    late.parm("check").set("save")
    result["late_cook"] = cook(late)
    late.parm("save").pressButton()
    result["late_exists"] = os.path.exists(os.path.join(scratch, "late.p3d"))
    result["late_after"] = {"errors": list(late.errors()), "messages": list(late.messages())}

    # No input.
    lonely = container.createNode("a3ob_p3d_export", "lonely")
    result["lonely"] = cook(lonely)
    print("A3OB_RESULT " + json.dumps(result))


main(sys.argv[1], sys.argv[2])

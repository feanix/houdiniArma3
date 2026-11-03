"""Run inside a child hython by tests/test_hda_node.py. Not a test file (the name does not start with ``test``).

The parent sets HOUDINI_PATH (it includes <repo>/houdini) and PYTHONPATH (<repo>/python) for this process only, so the
asset in houdini/otls loads exactly as it does for a user of the package. The script prints one line
``A3OB_RESULT <json>`` for the parent to check.

Usage: hython hda_child_check.py <sample_1_character.p3d> <sample_2_crate.p3d> <scratch dir>

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
import sys
import time

import hou

import a3ob_houdini.entry as entry
from a3ob_houdini import params, nodes


def cook(node):
    """Cook and return the error texts instead of raising."""
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    return list(node.errors())


def first_uid(node, lod_index):
    """The uid on the first polygon of a LOD, or None for a LOD without polygons (nothing can carry a uid)."""
    inner = node.geometry().prims()[lod_index].getEmbeddedGeometry()
    for prim in inner.prims():
        if isinstance(prim, hou.Polygon):
            return prim.attribValue("a3ob_lod_uid")
    return None


def main(character, crate, scratch):
    result = {}
    category = hou.sopNodeTypeCategory()
    node_type = hou.nodeType(category, "a3ob_p3d_import")
    result["type_found"] = node_type is not None
    definition = node_type.definition()
    result["library"] = definition.libraryFilePath()
    result["label"] = node_type.description()
    result["inputs"] = [node_type.minNumInputs(), node_type.maxNumInputs()]
    result["parms"] = [template.name() for template in node_type.parmTemplates()]
    result["sections"] = sorted(definition.sections().keys())
    result["pythoncook"] = definition.sections()["PythonCook"].contents()
    result["submenu"] = [list(tool.toolMenuLocations()) for tool in hou.shelves.tools().values()
                         if tool.name() == "sop_a3ob_p3d_import"]

    container = hou.node("/obj").createNode("geo", "container")
    first = container.createNode("a3ob_p3d_import", "import_a")
    first.parm("file").set(character)
    started = time.perf_counter()
    result["errors"] = cook(first)
    result["cook_seconds"] = time.perf_counter() - started
    result["warnings"] = list(first.warnings())
    result["messages"] = list(first.messages())
    result["prim_names"] = [prim.attribValue("name") for prim in first.geometry().prims()]
    uids = [first_uid(first, index) for index in range(len(result["prim_names"]))]
    result["uids_missing"] = [index for index, uid in enumerate(uids) if uid is None]
    cook(first)
    result["uids_stable"] = uids == [first_uid(first, index) for index in range(len(uids))]
    second = container.createNode("a3ob_p3d_import", "import_b")
    second.parm("file").set(character)
    cook(second)
    other = [first_uid(second, index) for index in range(len(uids))]
    result["uids_differ_between_nodes"] = all(a != b for a, b in zip(uids, other) if a is not None)
    stamped = [uid for uid in uids if uid is not None]
    result["uids_differ_between_lods"] = len(set(stamped)) == len(stamped)
    result["paths"] = [first.path(), second.path()]

    broken = container.createNode("a3ob_p3d_import", "broken")
    result["empty_file_errors"] = cook(broken)
    broken.parm("file").set(os.path.join(scratch, "missing.p3d"))
    result["missing_file_errors"] = cook(broken)
    garbage = os.path.join(scratch, "garbage.p3d")
    with open(garbage, "wb") as file:
        file.write(b"not a p3d file at all")
    broken.parm("file").set(garbage)
    result["garbage_file_errors"] = cook(broken)
    result["garbage_geometry_empty"] = broken.geometry() is None  # a node with an error outputs nothing
    broken.parm("file").set(crate)
    broken.parm("lods").set("x")
    result["bad_lods_errors"] = cook(broken)
    broken.parm("lods").set("0 2-3")
    result["partial_errors"] = cook(broken)
    result["partial_warnings"] = list(broken.warnings())
    result["partial_prims"] = len(broken.geometry().prims())
    broken.parm("lods").set("*")
    broken.parm("reload").pressButton()
    result["reload_errors"] = list(broken.errors())
    result["reload_prims"] = len(broken.geometry().prims())

    python_sop = container.createNode("python", "fallback")
    python_sop.parm("python").set("import a3ob_houdini.entry as e; e.cook('p3d_import', hou.pwd())")
    params.add_spare_parms(python_sop, nodes.load("p3d_import").SPEC)
    python_sop.parm("file").set(crate)
    result["fallback_errors"] = cook(python_sop)
    result["fallback_prims"] = len(python_sop.geometry().prims())

    bug = container.createNode("python", "bug")
    bug.parm("python").set("import a3ob_houdini.entry as e; e.cook('no_such_kind', hou.pwd())")
    result["bug_errors"] = cook(bug)
    entry.button("p3d_import", "no_such_action", {})  # must not raise
    print("A3OB_RESULT " + json.dumps(result))


if __name__ == "__main__":
    main(*sys.argv[1:4])

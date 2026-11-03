"""Run inside a child hython by tests/test_hda_lod_nodes.py. Not a test file (the name does not start with ``test``).

The parent sets HOUDINI_PATH (it includes <repo>/houdini) and PYTHONPATH (<repo>/python) for this process only, so the
assets in houdini/otls load as they do for a user of the package. The script prints one line ``A3OB_RESULT <json>``.

Usage: hython hda_child_check_lod.py <sample_2_crate.p3d>

SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import sys

import hou

import a3ob_houdini.entry as entry
from a3ob_houdini import nodes, params


def cook(node):
    """Cook and return the error texts instead of raising."""
    try:
        node.cook(force=True)
    except hou.OperationFailed:
        pass
    return list(node.errors())


def type_info(name):
    node_type = hou.nodeType(hou.sopNodeTypeCategory(), name)
    definition = node_type.definition()
    return {
        "found": node_type is not None,
        "label": node_type.description(),
        "inputs": [node_type.minNumInputs(), node_type.maxNumInputs()],
        "parms": [template.name() for template in node_type.parmTemplates()],
        "pythoncook": definition.sections()["PythonCook"].contents().strip(),
        "library": definition.libraryFilePath().replace("\\", "/"),
        "submenu": [list(tool.toolMenuLocations()) for tool in hou.shelves.tools().values()
                    if tool.name() == "sop_" + name],
    }


def main(crate):
    result = {"lod_properties": type_info("a3ob_lod_properties"), "material_assign": type_info("a3ob_material_assign")}
    container = hou.node("/obj").createNode("geo", "container")
    importer = container.createNode("a3ob_p3d_import", "import")
    importer.parm("file").set(crate)
    blast = container.createNode("blast", "keep_first")
    blast.setInput(0, importer)
    blast.parm("group").set("0")
    blast.parm("grouptype").set(4)
    blast.parm("negate").set(1)
    unpack = container.createNode("unpack", "unpack")
    unpack.setInput(0, blast)

    props = container.createNode("a3ob_lod_properties", "props")
    result["unconnected_errors"] = cook(props)
    props.setInput(0, unpack)
    props.parm("lodtype").set("geometry")
    props.parm("props").set(2)
    props.parm("prop_name1").set("class")
    props.parm("prop_value1").set("box")
    props.parm("prop_name2").set("AUTOCENTER")
    props.parm("prop_value2").set("0")
    props.parm("copies").set(1)
    props.parm("copy_type1").set("visual")
    props.parm("copy_res1").set(4.0)
    result["props_errors"] = cook(props)
    geo = props.geometry()
    result["props_messages"] = list(props.messages())
    result["props_type"] = geo.attribValue("a3ob_lod_type")
    result["props_resolution"] = geo.attribValue("a3ob_lod_resolution")
    result["props_signature"] = geo.attribValue("a3ob_lod_signature")
    result["props_names"] = list(geo.attribValue("a3ob_prop_names"))
    result["props_values"] = list(geo.attribValue("a3ob_prop_values"))
    result["props_copy_types"] = list(geo.attribValue("a3ob_lod_copy_types"))
    result["props_copy_resolutions"] = list(geo.attribValue("a3ob_lod_copy_resolutions"))
    result["props_points"] = len(geo.points())

    material = container.createNode("a3ob_material_assign", "material")
    material.setInput(0, props)
    material.parm("group").set("lid")
    material.parm("grouptype").set("points")
    material.parm("texture").set("a3/box/lid_co.paa")
    material.parm("material").set("a3/box/lid.rvmat")
    result["material_errors"] = cook(material)
    result["material_messages"] = list(material.messages())
    out = material.geometry()
    textures = out.primStringAttribValues("a3ob_texture")
    result["material_texture_values"] = sorted(set(textures))
    result["material_material_values"] = sorted(set(out.primStringAttribValues("a3ob_material")))
    result["material_set_count"] = sum(1 for text in textures if text == "a3\\box\\lid_co.paa")
    result["material_detail_type"] = out.attribValue("a3ob_lod_type")
    material.parmTuple("texcolor").set((0.25, 0.5, 0.75, 1.0))
    material.parm("texsource").set("color")
    material.parm("colortype").set("CA")
    material.parm("group").set("")
    result["color_errors"] = cook(material)
    result["color_values"] = sorted(set(material.geometry().primStringAttribValues("a3ob_texture")))

    # Read from Input: a second properties node reads the first one's output.
    reader = container.createNode("a3ob_lod_properties", "reader")
    reader.setInput(0, props)
    entry.button("lod_properties", "fromin", {"node": reader})
    result["fromin_type"] = reader.parm("lodtype").eval()
    result["fromin_props"] = reader.parm("props").eval()
    result["fromin_names"] = [reader.parm("prop_name1").eval(), reader.parm("prop_name2").eval()]
    result["fromin_values"] = [reader.parm("prop_value1").eval(), reader.parm("prop_value2").eval()]
    result["fromin_copies"] = reader.parm("copies").eval()
    result["fromin_copy_type"] = reader.parm("copy_type1").eval()
    result["fromin_copy_res"] = reader.parm("copy_res1").eval()
    result["fromin_errors"] = cook(reader)
    result["fromin_same_signature"] = reader.geometry().attribValue("a3ob_lod_signature") == result["props_signature"]

    # The Python SOP fallback builds the same parameters from the same table.
    python_sop = container.createNode("python", "fallback")
    params.add_spare_parms(python_sop, nodes.load("lod_properties").SPEC)
    result["fallback_parms"] = [template.name() for template in python_sop.parmTemplateGroup().entries()
                                if template.name() in ("lodtype", "props", "copies", "propmode")]
    python_sop.parm("lodtype").set("memory")
    python_sop.parm("props").set(1)
    result["fallback_multiparm_row"] = python_sop.parm("prop_name1") is not None
    result["fallback_read"] = params.read_parms(python_sop, nodes.load("lod_properties").SPEC)["lodtype"]
    print("A3OB_RESULT " + json.dumps(result))


main(sys.argv[1])

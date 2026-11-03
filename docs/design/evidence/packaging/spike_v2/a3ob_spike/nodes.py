"""Throwaway spike module, version 2.

Changes against v1, chosen to provoke the usual drift problems:
  * new parameter "scale" (layout drift)
  * default of "tag" changed to "hello-v2"
  * cook() renamed to run_cook() (renamed function)
"""
import json

import hou

VERSION = 2


def spare_parms():
    """Parameter layout of the node, version 2."""
    return [
        hou.StringParmTemplate("tag", "Tag", 1, default_value=("hello-v2",)),
        hou.FloatParmTemplate("scale", "Scale", 1, default_value=(1.0,)),
        hou.StringParmTemplate(
            "path", "File Path", 1, default_value=("",),
            string_type=hou.stringParmType.FileReference),
        hou.ButtonParmTemplate(
            "save", "Save to Disk",
            script_callback="import a3ob_spike.entry as e; e.save(kwargs)",
            script_callback_language=hou.scriptLanguage.Python),
    ]


def add_spare_parms(node):
    """Add every parameter of the current layout that the node lacks."""
    group = node.parmTemplateGroup()
    added = []
    for template in spare_parms():
        if group.find(template.name()) is None:
            group.append(template)
            added.append(template.name())
    if added:
        node.setParmTemplateGroup(group)
    return added


def run_cook(node):
    geo = node.geometry()
    geo.addAttrib(hou.attribType.Global, "tag", "")
    geo.setGlobalAttribValue("tag", node.evalParm("tag"))
    scale = node.evalParm("scale")
    for point in geo.points():
        point.setPosition(point.position() * scale)


def save(kwargs):
    node = kwargs["node"]
    path = node.evalParm("path")
    geo = node.geometry()
    with open(path, "w") as f:
        json.dump({"module_version": VERSION, "points": len(geo.points()),
                   "tag": geo.attribValue("tag")}, f)
    return path

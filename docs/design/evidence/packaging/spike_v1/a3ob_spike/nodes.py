"""Throwaway spike module, version 1. Stands in for python/a3ob_houdini.

All node logic is here. The node wrapper (Python SOP code or HDA PythonCook)
only calls entry.cook / entry.save.
"""
import json

import hou

VERSION = 1


def spare_parms():
    """Parameter layout of the node, version 1."""
    return [
        hou.StringParmTemplate("tag", "Tag", 1, default_value=("hello",)),
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


def cook(node):
    geo = node.geometry()
    geo.addAttrib(hou.attribType.Global, "tag", "")
    geo.setGlobalAttribValue("tag", node.evalParm("tag"))


def save(kwargs):
    node = kwargs["node"]
    path = node.evalParm("path")
    geo = node.geometry()
    with open(path, "w") as f:
        json.dump({"module_version": VERSION, "points": len(geo.points()),
                   "tag": geo.attribValue("tag")}, f)
    return path

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Generate the text files of a node's HDA from its parameter table (D13).

Each node ships as a folder ``houdini/otls/<type name>.hda/`` of plain text files, never saved from the Houdini GUI.
The layout is the one proven in docs/design/evidence/packaging/spike_v1/hda_src:

    houdini.hdalibrary            (empty)
    INDEX__SECTION                (the listing that ``hotl -V`` prints; must not be empty)
    Sections.list
    Sop_1<type name>/             (``/`` is ``_1`` and ``_`` is ``__`` in the section name)
        DialogScript              (the parameters, made from the table)
        PythonCook                (one line: a call into ``a3ob_houdini.entry``)
        TypePropertiesOptions
        Tools.shelf               (the Tab menu entry under the submenu named in the spec)
        Sections.list

``render`` makes the files as text, ``write`` puts them on disk, ``differences`` lists files that do not match.
``scripts/build_hdas.py`` is the command line; a test fails when the checked-in files differ from ``render``.
This module does not import ``hou``.
"""

import os
from typing import Dict, List

from .params import CALLBACK, MENU_SCRIPT, NodeSpec, Parm

__all__ = ["hda_folder_name", "section_name", "render", "write", "differences"]

#: Fixed, so that the generated files do not change from day to day.
MODIFIED = "Mon Nov  3 12:00:00 2026"

_TYPE_PROPERTIES = """ParmsFromVfl := 0;
PrefixDroppedParmName := 1;
UseDSParms := 1;
ForbidOutsideParms := 1;
LockContents := 1;
SaveSpareParms := 0;
CheckExternal := 1;
GzipContents := 1;
MakeDefault := 1;
PrefixDroppedParmLabel := 1;
UnlockOnCreate := 0;
"""

_TOOLS_SHELF = """<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <!-- This file contains definitions of shelves, toolbars, and tools.
  It should not be hand-edited when it is being used by the application.
  Note, that two definitions of the same element are not allowed in
  a single file. -->

  <tool name="$HDA_DEFAULT_TOOL" label="$HDA_LABEL" icon="$HDA_ICON">
    <toolMenuContext name="viewer">
      <contextNetType>SOP</contextNetType>
    </toolMenuContext>
    <toolMenuContext name="network">
      <contextOpType>$HDA_TABLE_AND_NAME</contextOpType>
    </toolMenuContext>
    <toolSubmenu>%(submenu)s</toolSubmenu>
    <script scriptType="python"><![CDATA[import soptoolutils

soptoolutils.genericTool(kwargs, '$HDA_NAME')]]></script>
  </tool>
</shelfDocument>
"""


def hda_folder_name(spec: NodeSpec) -> str:
    """``a3ob_p3d_import.hda``: the folder in ``houdini/otls``."""
    return spec.type_name + ".hda"


def section_name(spec: NodeSpec) -> str:
    """``Sop_1a3ob__p3d__import``: Houdini's name for the operator's section folder."""
    return "Sop_1" + spec.type_name.replace("_", "__")


def _quote(text: str) -> str:
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _default_text(parm: Parm) -> str:
    if parm.kind == "toggle":
        return "1" if parm.default else "0"
    if parm.kind in ("button",):
        return "0"
    return str(parm.default)


def _number(value: float) -> str:
    return "%d" % value if float(value) == int(value) else repr(float(value))


def _range_text(parm: Parm) -> str:
    """``0! 10``: the slider range; ``!`` after an end means Houdini clamps to it. An end not given is not strict."""
    low = "%s!" % _number(parm.minimum) if parm.minimum is not None else "0"
    high = "%s!" % _number(parm.maximum) if parm.maximum is not None else "10"
    return "%s %s" % (low, high)


def _parm_block(spec: NodeSpec, parm: Parm) -> str:
    """One ``parm { ... }`` (or ``multiparm { ... }``) block of the DialogScript."""
    if parm.kind == "multiparm":
        return _multiparm_block(spec, parm)
    kind = {"string": "string", "file": "file", "toggle": "toggle", "button": "button", "int": "integer",
            "float": "float", "menu": "string"}[parm.kind]
    if parm.kind == "float" and parm.size == 3:
        kind = "vector"  # Houdini names the components tx, ty, tz
    lines = ["    parm {", "\tname\t%s" % _quote(parm.name), "\tlabel\t%s" % _quote(parm.label), "\ttype\t%s" % kind]
    if parm.kind in ("int", "float"):
        lines.append("\tsize\t%d" % parm.size)
    if parm.size > 1:
        lines.append("\tdefault\t{ %s }" % " ".join(_quote(str(value)) for value in parm.default))
    else:
        lines.append("\tdefault\t{ %s }" % _quote(_default_text(parm)))
    if parm.kind in ("int", "float") and (parm.minimum is not None or parm.maximum is not None):
        lines.append("\trange\t{ %s }" % _range_text(parm))
    if parm.kind == "menu" and parm.menu_action:
        lines.append("\tmenu {")
        lines.append("\t    [ %s ]" % _quote(MENU_SCRIPT % (spec.kind, parm.menu_action)))
        lines.append("\t    language python")
        lines.append("\t}")
    elif parm.kind == "menu":
        lines.append("\tmenu {")
        lines.extend("\t    %s\t%s" % (_quote(token), _quote(label)) for token, label in parm.items)
        lines.append("\t}")
    if parm.help:
        lines.append("\thelp\t%s" % _quote(parm.help))
    if parm.kind == "file":
        lines.append('\tparmtag\t{ "filechooser_mode" "read" }')
        if parm.file_pattern:
            lines.append("\tparmtag\t{ \"filechooser_pattern\" %s }" % _quote(parm.file_pattern))
    if parm.kind == "button" or (parm.kind == "menu" and parm.action):
        callback = CALLBACK % (spec.kind, parm.action)
        lines.append("\tparmtag\t{ \"script_callback\" %s }" % _quote(callback))
        lines.append('\tparmtag\t{ "script_callback_language" "python" }')
    lines.append("    }")
    return "\n".join(lines)


def _multiparm_block(spec: NodeSpec, parm: Parm) -> str:
    lines = ["    multiparm {", "\tname\t%s" % _quote(parm.name), "\tlabel\t%s" % _quote(parm.label),
             "\tdefault\t%d" % int(parm.default), '\tparmtag\t{ "multistartoffset" "1" }']
    for child in parm.children:
        lines.append("")
        lines.extend("    " + line if line else line for line in _parm_block(spec, child).split("\n"))
    lines.append("    }")
    return "\n".join(lines)


def _dialog_script(spec: NodeSpec) -> str:
    lines = ["# Dialog script for %s, version 1" % spec.type_name, "", "{", "    name\t%s" % spec.type_name,
             "    script\t%s" % spec.type_name, "    label\t%s" % _quote(spec.label), "", "    help {",
             "\t%s" % _quote(spec.summary), "    }", ""]
    for number, label in enumerate(spec.input_labels, start=1):
        lines.append("    inputlabel\t%d\t%s" % (number, _quote(label)))
    if spec.input_labels:
        lines.append("")
    for parm in spec.parms:
        lines.append(_parm_block(spec, parm))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _index_section(spec: NodeSpec) -> str:
    low, high = spec.inputs
    return "\n".join([
        "Operator:     %s" % spec.type_name,
        "Label:        %s" % spec.label,
        "Path:         oplib:/Sop/%s?Sop/%s" % (spec.type_name, spec.type_name),
        "Icon:         SOP_python",
        "Table:        Sop",
        "License:      ",
        "Extra:        ",
        "User:         ",
        "Inputs:       %d to %d" % (low, high),
        "Subnet:       false",
        "Python:       true",
        "Empty:        false",
        "Modified:     %s" % MODIFIED,
        "", ""])


def _sections_list(entries) -> str:
    return '""\n' + "".join("%s\t%s\n" % entry for entry in entries)


def render(spec: NodeSpec) -> Dict[str, str]:
    """Every file of the node's HDA folder, as ``{relative path with "/": text}``."""
    section = section_name(spec)
    return {
        "houdini.hdalibrary": "",
        "INDEX__SECTION": _index_section(spec),
        "Sections.list": _sections_list([
            ("INDEX__SECTION", "INDEX_SECTION"), ("houdini.hdalibrary", "houdini.hdalibrary"),
            (section, "Sop/%s" % spec.type_name)]),
        section + "/DialogScript": _dialog_script(spec),
        section + "/PythonCook": "import a3ob_houdini.entry as e; e.cook(%r, hou.pwd())\n" % spec.kind,
        section + "/TypePropertiesOptions": _TYPE_PROPERTIES,
        section + "/Tools.shelf": _TOOLS_SHELF % {"submenu": spec.submenu},
        section + "/Sections.list": _sections_list([
            ("DialogScript", "DialogScript"), ("PythonCook", "PythonCook"),
            ("TypePropertiesOptions", "TypePropertiesOptions"), ("Tools.shelf", "Tools.shelf")]),
    }


def write(spec: NodeSpec, otls_dir: str) -> List[str]:
    """Write the node's HDA folder under ``otls_dir`` (LF line endings). Returns the paths written."""
    root = os.path.join(otls_dir, hda_folder_name(spec))
    written = []
    for relative, text in render(spec).items():
        path = os.path.join(root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        written.append(path)
    return written


def differences(spec: NodeSpec, otls_dir: str) -> List[str]:
    """Relative paths whose checked-in text differs from ``render`` (or is missing). Line endings are ignored."""
    root = os.path.join(otls_dir, hda_folder_name(spec))
    bad = []
    for relative, text in render(spec).items():
        path = os.path.join(root, *relative.split("/"))
        try:
            with open(path, "r", encoding="utf-8", newline=None) as file:  # universal newlines: CRLF == LF
                actual = file.read()
        except OSError:
            bad.append(relative)
            continue
        if actual != text:
            bad.append(relative)
    return bad

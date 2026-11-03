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
        Help                      (the node's help page in Houdini help wiki markup, made from the table)
        Sections.list

``render`` makes the files as text, ``write`` puts them on disk, ``differences`` lists files that do not match.
``scripts/build_hdas.py`` is the command line; a test fails when the checked-in files differ from ``render``.
This module does not import ``hou``.
"""

import os
from typing import Dict, List

from .docs_common import (FOLDER_TEXT, NODE_INTRO, collapse_inputs, condition_text, default_text, input_sentence,
                          menu_texts, parm_index_for, range_text, type_text, wiki_escape)
from .params import CALLBACK, CREATED_SCRIPT, MENU_SCRIPT, NodeSpec, Parm, hide_when_text

__all__ = ["hda_folder_name", "section_name", "render", "write", "differences"]

#: Fixed, so that the generated files do not change from day to day.
MODIFIED = "Mon Nov  3 12:00:00 2026"

#: The last line of every Help section. Houdini's help browser does not open file:// pages, so the line only names
#: the full page (``%s`` is the type name) and is not a link.
HELP_LAST_LINE = ("The full page for this node is docs/user/reference/%s.html in the A3OB folder. "
                  "Open it in a web browser.")

_TYPE_PROPERTIES ="""ParmsFromVfl := 0;
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

#: Tells Houdini that the OnCreated section is Python, not Hscript.
_EXTRA_FILE_OPTIONS = """{
\t"OnCreated/IsPython":{
\t\t"type":"bool",
\t\t"value":true
\t}
}
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
    if parm.kind == "folder":
        return _folder_block(spec, parm)
    kind = {"string": "string", "file": "file", "dir": "directory", "color": "color4", "toggle": "toggle",
            "button": "button", "int": "integer", "float": "float", "menu": "string"}[parm.kind]
    if parm.kind == "float" and parm.size == 3:
        kind = "vector"  # Houdini names the components tx, ty, tz
    lines = ["    parm {", "\tname\t%s" % _quote(parm.name), "\tlabel\t%s" % _quote(parm.label), "\ttype\t%s" % kind]
    if parm.kind in ("int", "float", "color"):
        lines.append("\tsize\t%d" % parm.size)
    if parm.kind == "color":
        lines.append("\tdefault\t{ %s }" % " ".join(_quote(_number(value)) for value in parm.default))
    elif parm.size > 1:
        lines.append("\tdefault\t{ %s }" % " ".join(_quote(str(value)) for value in parm.default))
    else:
        lines.append("\tdefault\t{ %s }" % _quote(_default_text(parm)))
    if parm.kind in ("int", "float") and (parm.minimum is not None or parm.maximum is not None):
        lines.append("\trange\t{ %s }" % _range_text(parm))
    if parm.kind == "color":
        lines.append("\trange\t{ 0 1 }")
    if parm.kind == "menu" and parm.menu_action:
        lines.append("\tmenutoggle {" if parm.menu_toggle else "\tmenu {")
        lines.append("\t    [ %s ]" % _quote(MENU_SCRIPT % (spec.kind, parm.menu_action)))
        lines.append("\t    language python")
        lines.append("\t}")
    elif parm.kind == "menu":
        lines.append("\tmenu {")
        lines.extend("\t    %s\t%s" % (_quote(token), _quote(label)) for token, label in parm.items)
        lines.append("\t}")
    if parm.help:
        lines.append("\thelp\t%s" % _quote(parm.help))
    if parm.enable_when:
        lines.append("\tdisablewhen\t%s" % _quote(hide_when_text(parm.enable_when)))
    if parm.show_when:
        lines.append("\thidewhen\t%s" % _quote(hide_when_text(parm.show_when)))
    if parm.kind == "file":
        lines.append('\tparmtag\t{ "filechooser_mode" "%s" }' % parm.file_mode)
        if parm.file_pattern:
            lines.append("\tparmtag\t{ \"filechooser_pattern\" %s }" % _quote(parm.file_pattern))
    if parm.kind == "button" or (parm.kind == "menu" and parm.action):
        callback = CALLBACK % (spec.kind, parm.action)
        lines.append("\tparmtag\t{ \"script_callback\" %s }" % _quote(callback))
        lines.append('\tparmtag\t{ "script_callback_language" "python" }')
    lines.append("    }")
    return "\n".join(lines)


def _folder_block(spec: NodeSpec, parm: Parm) -> str:
    """A folder: ``groupsimple`` / ``groupcollapsible`` / ``group`` (one tab; neighbours form the row of tabs).
    Its children are written inside it, one level deeper, as Houdini writes them."""
    word = {"simple": "groupsimple", "collapsible": "groupcollapsible", "tabs": "group"}[parm.folder_type]
    lines = ["    %s {" % word, "\tname\t%s" % _quote(parm.name), "\tlabel\t%s" % _quote(parm.label)]
    for child in parm.children:
        lines.append("")
        lines.extend("    " + line if line else line for line in _parm_block(spec, child).split("\n"))
    lines += ["    }", ""]
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


def _indent(lines: List[str]) -> List[str]:
    return [("    " + line) if line else "" for line in lines]


def _parm_help(spec: NodeSpec, parm: Parm, depth: int = 0, scope=None) -> List[str]:
    """Wiki entry for one parameter: the label as the term, then its facts. A multiparm nests its row parameters.
    A folder is a heading (one level deeper inside another folder) followed by its children. Text that the wiki would
    read as markup is escaped (``wiki_escape``)."""
    if parm.kind == "folder":
        bars = "=" * (2 + depth)
        lines = ["%s %s %s (%s)" % (bars, wiki_escape(parm.label), bars, parm.name), ""]
        lines.append("These parameters are in the %s %s." % (wiki_escape(parm.label), FOLDER_TEXT[parm.folder_type]))
        lines.append("")
        if parm.help:
            lines += [wiki_escape(parm.help), ""]
        for child in parm.children:
            lines += _parm_help(spec, child, depth + 1, scope)
        return lines
    intro, item_text = menu_texts(parm) if parm.kind == "menu" else (parm.help, {})
    body = ["#id: " + parm.name.replace("#", ""), ""]
    if intro:
        body += [wiki_escape(intro), ""]
    facts = ["Type: " + wiki_escape(type_text(parm))]
    default = default_text(parm)
    if default is not None:
        facts.append("Default: " + wiki_escape(default))
    allowed = range_text(parm)
    if allowed:
        facts.append("Allowed: " + wiki_escape(allowed))
    if parm.kind == "file" and parm.file_pattern:
        facts.append("Files: " + wiki_escape(parm.file_pattern))
    index = parm_index_for(spec, scope)
    if parm.show_when:
        facts.append("Shown when: " + wiki_escape(condition_text(index, parm.show_when)))
    if parm.enable_when:
        facts.append("Editable when: " + wiki_escape(condition_text(index, parm.enable_when)))
    body += [". ".join(facts) + ".", ""]
    if parm.kind == "menu" and parm.menu_action:
        body += ["The choices are made when you open the menu, from the geometry or the settings.", ""]
    plain = []
    for token, label in parm.items if parm.kind == "menu" else ():
        if item_text.get(token):
            body += [wiki_escape(label) + ":", "    " + wiki_escape(item_text[token]), ""]
        else:
            plain.append(wiki_escape(label))
    if plain:
        body += [("Other choices: " if len(plain) < len(parm.items) else "Choices: ") + ", ".join(plain) + ".", ""]
    if parm.kind == "multiparm":
        body += ["Each row has these parameters. The number in the row replaces the # in the name.", ""]
        for child in parm.children:
            body += _parm_help(spec, child, depth, parm)
    while body[-1] == "":
        body.pop()
    return [wiki_escape(parm.label) + ":"] + _indent(body) + [""]


def help_text(spec: NodeSpec) -> str:
    """The "Help" section: Houdini help wiki markup, made from the same node table as the HTML reference.

    Houdini's help server shows it for the node (``hou.helpServerUrl() + "nodes/sop/<type name>"``) and the ``?`` icon
    of the parameter pane opens it. The last line names the full HTML page; it is not a link."""
    lines = ["= %s =" % wiki_escape(spec.label), "", "#type: node", "#context: sop", "#internal: " + spec.type_name,
             "#tags: arma, p3d", "", '"""%s"""' % wiki_escape(spec.summary.replace('"""', "'''")), ""]
    for paragraph in NODE_INTRO.get(spec.kind, ()):
        lines += [wiki_escape(paragraph), ""]
    groups = collapse_inputs(spec)
    if groups:
        lines += ["@inputs", ""]
        for first, last, label in groups:
            lines.append(wiki_escape(label) + ":")
            lines.append("    " + wiki_escape(input_sentence(spec, first)))
            lines.append("")
        if spec.inputs[0] == 0:
            lines += ["The node also works with no input connected.", ""]
    lines += ["@parameters", ""]
    for parm in spec.parms:
        lines += _parm_help(spec, parm)
    lines += [HELP_LAST_LINE % spec.type_name, ""]
    return "\n".join(lines)


def _sections_list(entries) -> str:
    return '""\n' + "".join("%s\t%s\n" % entry for entry in entries)


def render(spec: NodeSpec) -> Dict[str, str]:
    """Every file of the node's HDA folder, as ``{relative path with "/": text}``."""
    section = section_name(spec)
    listed = [("DialogScript", "DialogScript"), ("PythonCook", "PythonCook"),
              ("TypePropertiesOptions", "TypePropertiesOptions"), ("Tools.shelf", "Tools.shelf"), ("Help", "Help")]
    files = {
        "houdini.hdalibrary": "",
        "INDEX__SECTION": _index_section(spec),
        "Sections.list": _sections_list([
            ("INDEX__SECTION", "INDEX_SECTION"), ("houdini.hdalibrary", "houdini.hdalibrary"),
            (section, "Sop/%s" % spec.type_name)]),
        section + "/DialogScript": _dialog_script(spec),
        section + "/PythonCook": "import a3ob_houdini.entry as e; e.cook(%r, hou.pwd())\n" % spec.kind,
        section + "/TypePropertiesOptions": _TYPE_PROPERTIES,
        section + "/Tools.shelf": _TOOLS_SHELF % {"submenu": spec.submenu},
        section + "/Help": help_text(spec),
    }
    if spec.on_created:
        files[section + "/OnCreated"] = CREATED_SCRIPT % spec.kind
        files[section + "/ExtraFileOptions"] = _EXTRA_FILE_OPTIONS
        listed += [("OnCreated", "OnCreated"), ("ExtraFileOptions", "ExtraFileOptions")]
    files[section + "/Sections.list"] = _sections_list(listed)
    return files


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

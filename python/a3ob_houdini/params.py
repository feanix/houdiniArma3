# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The parameter table of a node (D13).

Each A3OB node has one ``NodeSpec`` in ``a3ob_houdini/nodes/<kind>.py``. It is the only place that lists the
node's parameters. Two things are made from it:

- the HDA text files (``DialogScript`` and the others), by ``hda_source.py`` and ``scripts/build_hdas.py``;
- the spare parameters of a Python SOP, if the HDA route is ever dropped (``add_spare_parms``).

``read_parms`` reads the values at cook time. It falls back to the table's default when a parameter is missing, so an
old node does not crash when a newer package adds a parameter.

This module does not import ``hou`` at import time.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

__all__ = ["Parm", "NodeSpec", "type_name", "read_parms", "add_spare_parms", "KINDS"]

#: The parameter kinds the table and the generators know.
KINDS = ("string", "file", "toggle", "button", "int", "float", "menu", "multiparm")

TYPE_PREFIX = "a3ob_"


@dataclass(frozen=True)
class Parm:
    """One parameter. ``name`` is the short internal name; ``label`` is what the artist reads."""

    name: str
    label: str
    kind: str
    default: Any = ""
    help: str = ""
    #: For file parameters: the file chooser pattern, for example ``*.p3d``.
    file_pattern: str = ""
    #: For buttons: the action name. The button calls ``a3ob_houdini.entry.button(kind, action, kwargs)``.
    action: str = ""
    #: For menus: ``((token, label), ...)``. The default is a token and ``read_parms`` gives the token.
    items: Tuple[Tuple[str, str], ...] = ()
    #: For floats: how many components (a colour has 4). The default is then a tuple.
    size: int = 1
    #: For multiparms: the parameters of one row. Their names end in ``#`` (``prop_name#``); the row number
    #: replaces it (``prop_name1``). The default of the multiparm itself is the number of rows.
    children: Tuple["Parm", ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("parameter %r has unknown kind %r" % (self.name, self.kind))
        if self.kind == "button" and not self.action:
            raise ValueError("button %r needs an action name" % self.name)
        if self.kind == "menu" and not self.items:
            raise ValueError("menu %r needs items" % self.name)
        if self.kind == "multiparm" and not self.children:
            raise ValueError("multiparm %r needs children" % self.name)


@dataclass(frozen=True)
class NodeSpec:
    """Everything the node generators need to know about one node."""

    #: Short snake_case name, for example ``p3d_import``. The Houdini type name is ``a3ob_`` + kind.
    kind: str
    #: Label in the Tab menu and on the node, as in ux-tools.md (for example "P3D Import").
    label: str
    #: One sentence for the node's help.
    summary: str
    parms: Tuple[Parm, ...]
    #: How many inputs the node takes: (smallest, largest).
    inputs: Tuple[int, int] = (0, 0)
    input_labels: Tuple[str, ...] = ()
    #: Tab menu submenu.
    submenu: str = "A3OB"
    parm_index: Dict[str, Parm] = field(init=False, repr=False, compare=False, default_factory=dict)

    def __post_init__(self) -> None:
        names = [parm.name for parm in self.parms]
        if len(set(names)) != len(names):
            raise ValueError("node %r has a duplicate parameter name" % self.kind)
        object.__setattr__(self, "parm_index", {parm.name: parm for parm in self.parms})

    @property
    def type_name(self) -> str:
        return type_name(self.kind)


def type_name(kind: str) -> str:
    """The Houdini operator name for a node kind: ``a3ob_`` + kind."""
    return TYPE_PREFIX + kind


def read_parms(node, spec: NodeSpec) -> Dict[str, Any]:
    """Current values of every parameter (buttons left out). A missing parameter gives the table's default.

    A float with ``size`` above 1 gives a tuple. A multiparm gives a list with one dict per row, keyed by the
    children's names without the ``#``."""
    values = {}
    for parm in spec.parms:
        if parm.kind == "button":
            continue
        values[parm.name] = _read_one(node, parm)
    return values


def _read_one(node, parm: Parm):
    if parm.kind == "multiparm":
        count = node.parm(parm.name)
        rows = []
        for number in range(1, (int(parm.default) if count is None else int(count.eval())) + 1):
            rows.append({child.name.replace("#", ""): _read_row_parm(node, child, number) for child in parm.children})
        return rows
    if parm.kind == "float" and parm.size > 1:
        found = node.parmTuple(parm.name)
        return tuple(parm.default) if found is None else tuple(found.eval())
    found = node.parm(parm.name)
    return parm.default if found is None else found.eval()


def _read_row_parm(node, child: Parm, number: int):
    found = node.parm(child.name.replace("#", str(number)))
    return child.default if found is None else found.eval()


def add_spare_parms(node, spec: NodeSpec) -> None:
    """Python SOP fallback: add the table's parameters to a node as spare parameters."""
    group = node.parmTemplateGroup()
    for parm in spec.parms:
        group.append(_template(spec, parm))
    node.setParmTemplateGroup(group)


def _template(spec: NodeSpec, parm: Parm):
    import hou
    if parm.kind in ("string", "file"):
        template = hou.StringParmTemplate(parm.name, parm.label, 1, default_value=(str(parm.default),))
        if parm.kind == "file":
            template.setStringType(hou.stringParmType.FileReference)
    elif parm.kind == "menu":
        template = hou.StringParmTemplate(
            parm.name, parm.label, 1, default_value=(str(parm.default),),
            menu_items=tuple(token for token, _ in parm.items), menu_labels=tuple(label for _, label in parm.items))
    elif parm.kind == "toggle":
        template = hou.ToggleParmTemplate(parm.name, parm.label, default_value=bool(parm.default))
    elif parm.kind == "int":
        template = hou.IntParmTemplate(parm.name, parm.label, 1, default_value=(int(parm.default),))
    elif parm.kind == "float":
        defaults = tuple(parm.default) if parm.size > 1 else (float(parm.default),)
        template = hou.FloatParmTemplate(parm.name, parm.label, parm.size, default_value=defaults)
    elif parm.kind == "multiparm":
        template = hou.FolderParmTemplate(
            parm.name, parm.label, folder_type=hou.folderType.MultiparmBlock,
            parm_templates=[_template(spec, child) for child in parm.children])
    else:
        template = hou.ButtonParmTemplate(
            parm.name, parm.label, script_callback="import a3ob_houdini.entry as e; e.button(%r, %r, kwargs)"
            % (spec.kind, parm.action), script_callback_language=hou.scriptLanguage.Python)
    template.setHelp(parm.help)
    return template

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
KINDS = ("string", "file", "toggle", "button", "int", "float")

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

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("parameter %r has unknown kind %r" % (self.name, self.kind))
        if self.kind == "button" and not self.action:
            raise ValueError("button %r needs an action name" % self.name)


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
    """Current values of every parameter (buttons left out). A missing parameter gives the table's default."""
    values = {}
    for parm in spec.parms:
        if parm.kind == "button":
            continue
        found = node.parm(parm.name)
        values[parm.name] = parm.default if found is None else found.eval()
    return values


def add_spare_parms(node, spec: NodeSpec) -> None:
    """Python SOP fallback: add the table's parameters to a node as spare parameters."""
    import hou
    group = node.parmTemplateGroup()
    for parm in spec.parms:
        if parm.kind in ("string", "file"):
            template = hou.StringParmTemplate(parm.name, parm.label, 1, default_value=(str(parm.default),))
            if parm.kind == "file":
                template.setStringType(hou.stringParmType.FileReference)
        elif parm.kind == "toggle":
            template = hou.ToggleParmTemplate(parm.name, parm.label, default_value=bool(parm.default))
        elif parm.kind == "int":
            template = hou.IntParmTemplate(parm.name, parm.label, 1, default_value=(int(parm.default),))
        elif parm.kind == "float":
            template = hou.FloatParmTemplate(parm.name, parm.label, 1, default_value=(float(parm.default),))
        else:
            template = hou.ButtonParmTemplate(
                parm.name, parm.label, script_callback="import a3ob_houdini.entry as e; e.button(%r, %r, kwargs)"
                % (spec.kind, parm.action), script_callback_language=hou.scriptLanguage.Python)
        template.setHelp(parm.help)
        group.append(template)
    node.setParmTemplateGroup(group)

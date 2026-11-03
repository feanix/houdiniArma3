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
from typing import Any, Dict, Optional, Tuple

__all__ = ["Parm", "NodeSpec", "type_name", "read_parms", "add_spare_parms", "KINDS"]

#: The parameter kinds the table and the generators know.
KINDS = ("string", "file", "toggle", "button", "int", "float", "menu")

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
    #: For buttons, and for menus that fill other parameters: the action name. It calls
    #: ``a3ob_houdini.entry.button(kind, action, kwargs)``.
    action: str = ""
    #: For menus: ``((token, label), ...)``. The parameter value is the token (a string); ``default`` is a token.
    menu: Tuple[Tuple[str, str], ...] = ()
    #: For int and float parameters: how many components (a vector has ``size`` 3; its Houdini names are ``name`` +
    #: ``x``, ``y``, ``z``). ``default`` is then a tuple.
    size: int = 1
    #: For int and float parameters: the allowed range. Values outside it are clamped by Houdini.
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("parameter %r has unknown kind %r" % (self.name, self.kind))
        if self.kind == "button" and not self.action:
            raise ValueError("button %r needs an action name" % self.name)
        if self.kind == "menu":
            if not self.menu:
                raise ValueError("menu %r needs entries" % self.name)
            if self.default not in [token for token, _ in self.menu]:
                raise ValueError("menu %r: the default %r is not one of its tokens" % (self.name, self.default))
        if self.size != 1 and self.kind not in ("int", "float"):
            raise ValueError("parameter %r: only int and float can have a size" % self.name)
        if self.size > 1 and len(self.default) != self.size:
            raise ValueError("parameter %r needs a default with %d numbers" % (self.name, self.size))


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
        if parm.size > 1:
            found = node.parmTuple(parm.name)
            values[parm.name] = parm.default if found is None else tuple(found.eval())
            continue
        found = node.parm(parm.name)
        values[parm.name] = parm.default if found is None else found.eval()
    return values


def add_spare_parms(node, spec: NodeSpec) -> None:
    """Python SOP fallback: add the table's parameters to a node as spare parameters."""
    import hou
    group = node.parmTemplateGroup()
    callback = "import a3ob_houdini.entry as e; e.button(%r, %r, kwargs)"
    for parm in spec.parms:
        if parm.kind in ("string", "file"):
            template = hou.StringParmTemplate(parm.name, parm.label, 1, default_value=(str(parm.default),))
            if parm.kind == "file":
                template.setStringType(hou.stringParmType.FileReference)
        elif parm.kind == "menu":
            tokens = [token for token, _ in parm.menu]
            template = hou.MenuParmTemplate(parm.name, parm.label, tokens, [label for _, label in parm.menu],
                                            default_value=tokens.index(parm.default), menu_type=hou.menuType.Normal)
            if parm.action:
                template.setScriptCallback(callback % (spec.kind, parm.action))
                template.setScriptCallbackLanguage(hou.scriptLanguage.Python)
        elif parm.kind == "toggle":
            template = hou.ToggleParmTemplate(parm.name, parm.label, default_value=bool(parm.default))
        elif parm.kind == "int":
            default = parm.default if parm.size > 1 else (parm.default,)
            template = hou.IntParmTemplate(parm.name, parm.label, parm.size, default_value=tuple(int(v) for v in default),
                                           min=int(parm.minimum or 0), max=int(parm.maximum or 10),
                                           min_is_strict=parm.minimum is not None,
                                           max_is_strict=parm.maximum is not None)
        elif parm.kind == "float":
            default = parm.default if parm.size > 1 else (parm.default,)
            template = hou.FloatParmTemplate(parm.name, parm.label, parm.size,
                                             default_value=tuple(float(v) for v in default),
                                             min=float(parm.minimum or 0.0), max=float(parm.maximum or 10.0),
                                             min_is_strict=parm.minimum is not None,
                                             max_is_strict=parm.maximum is not None,
                                             naming_scheme=hou.parmNamingScheme.XYZW)
        else:
            template = hou.ButtonParmTemplate(
                parm.name, parm.label, script_callback="import a3ob_houdini.entry as e; e.button(%r, %r, kwargs)"
                % (spec.kind, parm.action), script_callback_language=hou.scriptLanguage.Python)
        template.setHelp(parm.help)
        group.append(template)
    node.setParmTemplateGroup(group)

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
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["Parm", "NodeSpec", "type_name", "read_parms", "add_spare_parms", "flat_parms", "KINDS", "FOLDER_TYPES"]

#: The parameter kinds the table and the generators know. ``folder`` is not a value: it only groups its children on the
#: node (see ``Parm.folder_type``). ``dir`` is a string with a folder chooser. ``color`` is four floats (red, green,
#: blue, alpha) with a colour swatch.
KINDS = ("string", "file", "dir", "color", "toggle", "button", "int", "float", "menu", "multiparm", "folder")

#: The looks of a ``folder``: a group box (``simple``), a group box the artist can fold (``collapsible``), or one tab.
#: Neighbouring ``tabs`` folders make one row of tabs.
FOLDER_TYPES = ("simple", "collapsible", "tabs")

TYPE_PREFIX = "a3ob_"

#: What a button, or a menu with an action, runs when it changes.
CALLBACK = "import a3ob_houdini.entry as e; e.button(%r, %r, kwargs)"

#: The script of a menu whose items are made at the moment the menu opens (``Parm.menu_action``).
#: Houdini evaluates it as one Python expression (``return`` is a syntax error there), so it imports inline.
MENU_SCRIPT = "__import__('a3ob_houdini.entry', fromlist=['menu']).menu(%r, %r, kwargs)"


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
    #: For file parameters: ``read`` (the chooser picks an existing file) or ``write`` (it may name a new one).
    file_mode: str = "read"
    #: For buttons, and for menus that fill other parameters: the action name. It calls
    #: ``a3ob_houdini.entry.button(kind, action, kwargs)``.
    action: str = ""
    #: For menus: ``((token, label), ...)``. The default is a token and ``read_parms`` gives the token.
    items: Tuple[Tuple[str, str], ...] = ()
    #: For menus whose items depend on the scene (the LODs of the input): the name of a function in the node module's
    #: ``MENUS``. It is called as ``entry.menu(kind, name, kwargs)`` and returns ``[(token, label), ...]``. A menu
    #: with this needs no ``items``; the default is then any token the artist may type.
    menu_action: str = ""
    #: For int and float parameters: how many components (a colour has 4, a translate 3). The default is then a
    #: tuple. A float of size 3 is a vector: its Houdini names end in ``x``, ``y``, ``z``. Other sizes end in 1, 2, ...
    size: int = 1
    #: For int and float parameters: the allowed range. A given end is strict (Houdini clamps to it); an end left
    #: ``None`` is not strict, so the artist may type past the slider (0 to 10).
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    #: For multiparms: the parameters of one row. Their names end in ``#`` (``prop_name#``); the row number
    #: replaces it (``prop_name1``). The default of the multiparm itself is the number of rows.
    children: Tuple["Parm", ...] = ()
    #: For folders: ``simple``, ``collapsible`` or ``tabs`` (one tab; neighbouring tabs share a row). The children are
    #: ordinary top-level parameters: a folder only groups them, so ``read_parms`` and the cooks never see it.
    folder_type: str = "simple"
    #: For menus filled by ``menu_action``: let the artist pick several entries. Each pick adds or removes its token
    #: in the string (Houdini ``menutoggle``). The callback gives the entries; the parameter stays a string.
    menu_toggle: bool = False
    #: When the parameter is shown: ``((parm name, (value, ...)), ...)``. All pairs must hold (and), and a pair holds
    #: when the named parameter has any of its values (or). A menu value is its token; a toggle is ``"1"`` or
    #: ``"0"``. The generator writes Houdini ``hidewhen`` for it, the docs say it in words. Empty: always shown.
    show_when: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: When the parameter can be edited, in the same form as ``show_when`` (Houdini ``disablewhen``).
    enable_when: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: For menus: ``((token, text), ...)``: what each choice does, in plain words. A choice without a text is only
    #: listed by name. A menu whose help already reads ``Intro. Label: text; Label: text`` needs none of this.
    item_help: Tuple[Tuple[str, str], ...] = ()
    #: The parameter is never shown on the node (Houdini ``invisible``). For legacy parameters that old scenes still
    #: store, so they load; the cook reads them. The docs still list them: the help text says what they are for.
    hidden: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("parameter %r has unknown kind %r" % (self.name, self.kind))
        if self.folder_type not in FOLDER_TYPES:
            raise ValueError("parameter %r: folder_type must be one of %s" % (self.name, ", ".join(FOLDER_TYPES)))
        if self.kind == "folder" and not self.children:
            raise ValueError("folder %r needs children" % self.name)
        if self.kind == "folder" and (self.show_when or self.enable_when):
            raise ValueError("folder %r: put show_when and enable_when on its children" % self.name)
        if self.children and self.kind not in ("folder", "multiparm"):
            raise ValueError("parameter %r: only a folder or a multiparm has children" % self.name)
        if self.kind == "color":
            if self.size == 1:
                object.__setattr__(self, "size", 4)
            if self.size != 4:
                raise ValueError("colour %r has four components (red, green, blue, alpha)" % self.name)
            if self.default == "":
                object.__setattr__(self, "default", (1.0, 1.0, 1.0, 1.0))
        if self.menu_toggle and not (self.kind == "menu" and self.menu_action):
            raise ValueError("parameter %r: menu_toggle needs a menu with a menu_action" % self.name)
        if self.item_help and self.kind != "menu":
            raise ValueError("parameter %r: only a menu has item_help" % self.name)
        for token, _ in self.item_help:
            if token not in [item for item, _ in self.items]:
                raise ValueError("menu %r: item_help names %r, which is not a choice" % (self.name, token))
        for conditions in (self.show_when, self.enable_when):
            for name, values in conditions:
                if not name or not values:
                    raise ValueError("parameter %r: a condition needs a parameter name and values" % self.name)
        if self.kind == "button" and not self.action:
            raise ValueError("button %r needs an action name" % self.name)
        if self.action and self.kind not in ("button", "menu"):
            raise ValueError("parameter %r: only a button or a menu has an action" % self.name)
        if self.file_mode not in ("read", "write"):
            raise ValueError("parameter %r: file_mode must be read or write" % self.name)
        if self.menu_action and self.kind != "menu":
            raise ValueError("parameter %r: only a menu has a menu_action" % self.name)
        if self.kind == "menu" and self.menu_action:
            if self.items:
                raise ValueError("menu %r: give items or a menu_action, not both" % self.name)
        elif self.kind == "menu":
            if not self.items:
                raise ValueError("menu %r needs items" % self.name)
            if self.default not in [token for token, _ in self.items]:
                raise ValueError("menu %r: the default %r is not one of its tokens" % (self.name, self.default))
        if self.kind == "multiparm" and not self.children:
            raise ValueError("multiparm %r needs children" % self.name)
        if self.size != 1 and self.kind not in ("int", "float", "color"):
            raise ValueError("parameter %r: only int, float and color can have a size" % self.name)
        if self.size < 1:
            raise ValueError("parameter %r: size must be at least 1" % self.name)
        if self.size > 1 and (not isinstance(self.default, (tuple, list)) or len(self.default) != self.size):
            raise ValueError("parameter %r needs a default with %d numbers" % (self.name, self.size))
        if (self.minimum is not None or self.maximum is not None) and self.kind not in ("int", "float"):
            raise ValueError("parameter %r: only int and float can have a range" % self.name)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter %r: the minimum is above the maximum" % self.name)


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
    #: One plain sentence per input (same order as ``input_labels``) for the Help section and the reference. Inputs
    #: that only differ by "(input N)" in their labels share the first one's sentence. May be shorter than the labels.
    input_help: Tuple[str, ...] = ()
    #: Tab menu submenu.
    submenu: str = "A3OB"
    parm_index: Dict[str, Parm] = field(init=False, repr=False, compare=False, default_factory=dict)

    def __post_init__(self) -> None:
        flat = flat_parms(self.parms)
        names = [parm.name for parm in flat]
        folder_names = [parm.name for parm in _walk_folders(self.parms)]
        if len(set(names + folder_names)) != len(names + folder_names):
            raise ValueError("node %r has a duplicate parameter name" % self.kind)
        object.__setattr__(self, "parm_index", {parm.name: parm for parm in flat})
        _check_conditions(self.kind, self.parms, set(names))

    @property
    def type_name(self) -> str:
        return type_name(self.kind)


def type_name(kind: str) -> str:
    """The Houdini operator name for a node kind: ``a3ob_`` + kind."""
    return TYPE_PREFIX + kind


def flat_parms(parms) -> List[Parm]:
    """The parameters that hold values, in node order: folders are looked through and left out. A multiparm stays
    one entry (its row parameters are its children)."""
    found = []
    for parm in parms:
        if parm.kind == "folder":
            found.extend(flat_parms(parm.children))
        else:
            found.append(parm)
    return found


def _walk_folders(parms) -> List[Parm]:
    found = []
    for parm in parms:
        if parm.kind == "folder":
            found.append(parm)
            found.extend(_walk_folders(parm.children))
    return found


def _check_conditions(kind: str, parms, names) -> None:
    """Every name in a show_when or enable_when must be a parameter of the node (or of the same multiparm row)."""
    for parm in flat_parms(parms):
        row_names = {child.name for child in parm.children} if parm.kind == "multiparm" else set()
        for conditions in (parm.show_when, parm.enable_when):
            for name, _ in conditions:
                if name not in names:
                    raise ValueError("node %r: %r depends on %r, which is not a parameter" % (kind, parm.name, name))
        for child in parm.children if parm.kind == "multiparm" else ():
            for conditions in (child.show_when, child.enable_when):
                for name, _ in conditions:
                    if name not in names and name not in row_names:
                        raise ValueError("node %r: %r depends on %r, which is not a parameter" % (kind, child.name, name))


def read_parms(node, spec: NodeSpec) -> Dict[str, Any]:
    """Current values of every parameter (buttons and folders left out). A missing parameter gives the table's
    default. A parameter inside a folder is read like any other: folders only group them on the node.

    An int, float or colour with ``size`` above 1 gives a tuple. A multiparm gives a list with one dict per row,
    keyed by the children's names without the ``#``."""
    values = {}
    for parm in flat_parms(spec.parms):
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
    if parm.size > 1:
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


def _term(value: str) -> str:
    """A value in a Houdini condition: plain when it is a simple word or number, quoted otherwise."""
    return value if value and all(ch.isalnum() or ch in "_.-" for ch in value) else '"%s"' % value.replace('"', '\\"')


def hide_when_text(show_when) -> str:
    """The Houdini ``hidewhen`` condition that hides a parameter unless every pair of ``show_when`` holds.

    ``{ a != x a != y } { b != z }`` hides when (a is neither x nor y) or (b is not z): terms in one brace are and-ed,
    braces are or-ed. The same text, from ``enable_when``, is the ``disablewhen`` condition."""
    return " ".join("{ %s }" % " ".join("%s != %s" % (name, _term(value)) for value in values)
                    for name, values in show_when)


def _set_conditions(template, parm: Parm) -> None:
    import hou
    if parm.show_when:
        template.setConditional(hou.parmCondType.HideWhen, hide_when_text(parm.show_when))
    if parm.enable_when:
        template.setConditional(hou.parmCondType.DisableWhen, hide_when_text(parm.enable_when))


_FOLDER_TYPES = {"simple": "Simple", "collapsible": "Collapsible", "tabs": "Tabs"}


def _template(spec: NodeSpec, parm: Parm):
    import hou
    template = _template_unconditioned(spec, parm)
    _set_conditions(template, parm)
    if parm.hidden:
        template.hide(True)
    return template


def _template_unconditioned(spec: NodeSpec, parm: Parm):
    import hou
    if parm.kind == "folder":
        return hou.FolderParmTemplate(
            parm.name, parm.label, folder_type=getattr(hou.folderType, _FOLDER_TYPES[parm.folder_type]),
            parm_templates=[_template(spec, child) for child in parm.children])
    if parm.kind == "dir":
        template = hou.StringParmTemplate(parm.name, parm.label, 1, default_value=(str(parm.default),),
                                          string_type=hou.stringParmType.FileReference,
                                          file_type=hou.fileType.Directory)
        template.setHelp(parm.help)
        return template
    if parm.kind == "color":
        template = hou.FloatParmTemplate(
            parm.name, parm.label, 4, default_value=tuple(float(v) for v in parm.default), min=0.0, max=1.0,
            look=hou.parmLook.ColorSquare, naming_scheme=hou.parmNamingScheme.RGBA)
        template.setHelp(parm.help)
        return template
    if parm.kind in ("string", "file"):
        template = hou.StringParmTemplate(parm.name, parm.label, 1, default_value=(str(parm.default),))
        if parm.kind == "file":
            template.setStringType(hou.stringParmType.FileReference)
            tags = {"filechooser_mode": parm.file_mode}
            if parm.file_pattern:
                tags["filechooser_pattern"] = parm.file_pattern
            template.setTags(tags)
    elif parm.kind == "menu" and parm.menu_action:
        template = hou.StringParmTemplate(
            parm.name, parm.label, 1, default_value=(str(parm.default),),
            menu_type=hou.menuType.StringToggle if parm.menu_toggle else hou.menuType.Normal,
            item_generator_script=MENU_SCRIPT % (spec.kind, parm.menu_action),
            item_generator_script_language=hou.scriptLanguage.Python)
        if parm.action:  # a dynamic menu that also fills other parameters (Material Assign "Common" menus)
            template.setScriptCallback(CALLBACK % (spec.kind, parm.action))
            template.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    elif parm.kind == "menu":
        template = hou.StringParmTemplate(
            parm.name, parm.label, 1, default_value=(str(parm.default),),
            menu_items=tuple(token for token, _ in parm.items), menu_labels=tuple(label for _, label in parm.items))
        if parm.action:
            template.setScriptCallback(CALLBACK % (spec.kind, parm.action))
            template.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    elif parm.kind == "toggle":
        template = hou.ToggleParmTemplate(parm.name, parm.label, default_value=bool(parm.default))
    elif parm.kind == "int":
        defaults = tuple(int(v) for v in parm.default) if parm.size > 1 else (int(parm.default),)
        template = hou.IntParmTemplate(
            parm.name, parm.label, parm.size, default_value=defaults,
            min=int(parm.minimum) if parm.minimum is not None else 0,
            max=int(parm.maximum) if parm.maximum is not None else 10,
            min_is_strict=parm.minimum is not None, max_is_strict=parm.maximum is not None)
    elif parm.kind == "float":
        defaults = tuple(float(v) for v in parm.default) if parm.size > 1 else (float(parm.default),)
        template = hou.FloatParmTemplate(
            parm.name, parm.label, parm.size, default_value=defaults,
            min=float(parm.minimum) if parm.minimum is not None else 0.0,
            max=float(parm.maximum) if parm.maximum is not None else 10.0,
            min_is_strict=parm.minimum is not None, max_is_strict=parm.maximum is not None,
            naming_scheme=hou.parmNamingScheme.XYZW if parm.size == 3 else hou.parmNamingScheme.Base1)
    elif parm.kind == "multiparm":
        template = hou.FolderParmTemplate(
            parm.name, parm.label, folder_type=hou.folderType.MultiparmBlock,
            parm_templates=[_template(spec, child) for child in parm.children])
    else:
        template = hou.ButtonParmTemplate(
            parm.name, parm.label, script_callback=CALLBACK % (spec.kind, parm.action), script_callback_language=hou.scriptLanguage.Python)
    template.setHelp(parm.help)
    return template

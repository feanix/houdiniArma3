# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Selection browser without a dialog (ux-tools.md section 7.3).

The shelf tool is a thin layer (``selection_browser_dialog``) on top of these functions, because ``hou.ui`` does not
exist in hython. Everything here works on a node or a ``hou.Geometry``, so it can be tested headless.

A3OB selections are point groups with encoded names (spec 7.3). ``list_groups`` shows the decoded name next to the
encoded one. The three actions insert one SOP node after the chosen node: Group, Group Delete or Group Rename. The nodes that
read the chosen node read the new one instead, so the export sees the change (D21). They never touch cooked
geometry.
"""

from dataclasses import dataclass
from typing import List

from a3ob_core.p3d import proxy

from . import names
from .errors import UserError
from .messages import format_problem

__all__ = ["GroupEntry", "list_groups", "describe", "has_packed_lods", "create_selection", "delete_selection",
           "rename_selection"]

TOOL = "Selection browser"

#: One row in the network editor: the gap between stacked nodes. The New P3D LAYOUT steps between stacked nodes are
#: 0.77, 0.78, 0.82 and 0.95; 0.8 sits in the middle.
ROW_STEP = 0.8

#: Group SOP "entity" menu: 0 primitive, 1 point. The Group Delete and Group Rename "grouptype" menus: 1 is points.
_GROUP_ENTITY_POINT = 1
_GROUP_TYPE_POINTS = 1


@dataclass
class GroupEntry:
    """One point group of a geometry."""

    #: The Houdini group name (what a group field takes).
    encoded: str
    #: The A3OB selection name.
    name: str
    count: int
    #: A point weight attribute ``a3ob_w_<encoded>`` exists.
    weighted: bool
    #: A primitive group of the same name exists.
    faces: bool
    #: The name is a proxy name (``proxy:path.index``).
    proxy: bool


def list_groups(geo, filter_text: str = "") -> List[GroupEntry]:
    """The point groups of ``geo`` by decoded name, sorted by name. ``filter_text`` keeps names that contain it
    (case does not matter; the encoded name is searched too)."""
    needle = filter_text.strip().lower()
    prim_groups = {group.name() for group in geo.primGroups()}
    entries = []
    for group in geo.pointGroups():
        encoded = group.name()
        name = names.decode_group_name(encoded)
        if needle and needle not in name.lower() and needle not in encoded.lower():
            continue
        entries.append(GroupEntry(
            encoded=encoded, name=name, count=len(group.points()),
            weighted=geo.findPointAttrib(names.point_weight_attrib(encoded)) is not None,
            faces=encoded in prim_groups, proxy=proxy.is_proxy_name(name)))
    entries.sort(key=lambda entry: entry.name)
    return entries


def describe(entry: GroupEntry) -> str:
    """One list line: ``-proxies  (xn__proxies_cka)  12 points, weighted, faces``."""
    tags = ["%d points" % entry.count]
    if entry.weighted:
        tags.append("weighted")
    if entry.faces:
        tags.append("faces")
    if entry.proxy:
        tags.append("proxy")
    shown = entry.name if entry.name else "(empty name)"
    if entry.encoded != entry.name:
        shown += "  (%s)" % entry.encoded
    return "%s  %s" % (shown, ", ".join(tags))


def has_packed_lods(geo) -> bool:
    """True when ``geo`` holds packed prims. P3D geometry keeps the groups inside them, out of reach of a Group node
    (Extract LOD opens one LOD)."""
    return any(prim.type().name() == "PackedGeometry" for prim in geo.prims())


def _existing(node) -> set:
    return {group.name() for group in node.geometry().pointGroups()}


def _check_name(name: str) -> str:
    """The encoded group name for a typed selection name, or a ``UserError``."""
    if not name:
        raise UserError(format_problem(TOOL, "the selection name is empty", "Type a name"))
    reason = names.check_encodable(name)
    if reason:
        raise UserError(format_problem(TOOL, "the name %r cannot be written to a P3D file (%s)" % (name, reason),
                                       "Choose another name"))
    return names.encode_group_name(name)


def _downstream(node) -> list:
    """Every node reached from ``node`` through outputs, ``node`` itself left out."""
    found, todo = {}, list(node.outputs())
    while todo:
        out = todo.pop()
        if out.path() not in found and out.path() != node.path():
            found[out.path()] = out
            todo.extend(out.outputs())
    return list(found.values())


def _add_after(node, type_name: str, label: str):
    """Create a node and splice it in after ``node`` (D21): the new node reads ``node``, and every node that read
    ``node`` (at its own input index) reads the new node. Display and render flags move to the new node. The new
    node sits one row below ``node``; everything downstream moves down one row. Without outputs the new node is a
    plain branch below ``node``."""
    network = node.parent()
    connections = [(c.outputNode(), c.inputIndex()) for c in node.outputConnections()]
    moved = _downstream(node)
    display, render = _flag(node, "isDisplayFlagSet"), _flag(node, "isRenderFlagSet")
    new = network.createNode(type_name, "a3ob_" + label)
    new.setFirstInput(node)
    for out, index in connections:
        out.setInput(index, new)
    if display:
        new.setDisplayFlag(True)
    if render:
        new.setRenderFlag(True)
    # Positions last: wiring can nudge a node (#23).
    import hou
    here = node.position()
    for out in moved:
        position = out.position()
        out.setPosition(hou.Vector2(position[0], position[1] - ROW_STEP))
    new.setPosition(hou.Vector2(here[0], here[1] - ROW_STEP))
    return new


def _flag(node, getter: str) -> bool:
    check = getattr(node, getter, None)
    return bool(check()) if check else False


def create_selection(node, name: str, expression: str = ""):
    """Add a Group node after ``node`` that makes the point group for the P3D selection ``name``.

    ``expression`` is a Houdini point number pattern (``0-9``, ``0 5 7``, ``!3``). Empty means all points. The new
    node's other group settings stay open for the artist (bounding box, normal, and so on). Returns the new node."""
    encoded = _check_name(name)
    if encoded in _existing(node):
        raise UserError(format_problem(TOOL, "the selection %r already exists" % name,
                                       "Choose another name, or delete the old one first"))
    new = _add_after(node, "group", "selection")
    new.parm("crname").set(encoded)
    new.parm("entity").set(_GROUP_ENTITY_POINT)
    new.parm("groupop").set(0)  # "Pattern": point numbers
    new.parm("pattern").set(expression.strip())
    return new


def delete_selection(node, name: str):
    """Add a Group Delete node after ``node`` that removes the point group of the selection ``name``."""
    encoded = _check_name(name)
    if encoded not in _existing(node):
        raise UserError(format_problem(TOOL, "the selection %r is not a point group of %s" % (name, node.path()),
                                       "Pick a name from the list"))
    new = _add_after(node, "groupdelete", "delete_selection")
    new.parm("grouptype1").set(_GROUP_TYPE_POINTS)
    new.parm("group1").set(encoded)
    return new


def rename_selection(node, name: str, new_name: str):
    """Add a Group Rename node after ``node`` that renames the point group of the selection ``name``."""
    encoded = _check_name(name)
    new_encoded = _check_name(new_name)
    existing = _existing(node)
    if encoded not in existing:
        raise UserError(format_problem(TOOL, "the selection %r is not a point group of %s" % (name, node.path()),
                                       "Pick a name from the list"))
    if new_encoded == encoded:
        raise UserError(format_problem(TOOL, "the new name is the same as the old one", "Type a different name"))
    if new_encoded in existing:
        raise UserError(format_problem(TOOL, "the selection %r already exists" % new_name,
                                       "Choose another name, or delete the old one first"))
    new = _add_after(node, "grouprename", "rename_selection")
    new.parm("grouptype1").set(_GROUP_TYPE_POINTS)
    new.parm("group1").set(encoded)
    new.parm("newname1").set(new_encoded)
    return new

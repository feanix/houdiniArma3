# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The menu of a group picker (``Parm`` kind ``group``): the groups of the first input, as the Group field of
Houdini's own SOPs lists them.

Houdini does the listing (``hou.SopNode.generateInputGroupMenu``, the call behind the stock Group menus), so the
entries, the order and the "already picked" marks are the standard ones. A group whose name is an encoded A3OB
selection name is listed by Houdini with the decoded P3D name as the label and the group name as the token, so the
dropdown is readable with no extra work. This module only picks the element type. Needs ``hou`` (at call time).
"""

__all__ = ["group_type_of", "menu_items"]

_GEOMETRY_TYPE = {"prims": "Primitives", "points": "Points"}


def group_type_of(node, parm) -> str:
    """``prims`` or ``points``: the fixed type of the picker, or the value of the Group Type menu it follows.
    A value the menu does not know (a typed one) counts as ``prims``, as the cooks do."""
    if parm.group_follow:
        follow = node.parm(parm.group_follow)
        value = follow.eval() if follow is not None else parm.group_type
        return "points" if value in ("points", "point") else "prims"
    return parm.group_type


def menu_items(node, parm, hou_parm=None):
    """``[(token, label), ...]`` for the picker ``parm`` (a ``params.Parm``) on ``node``. No input gives no items."""
    import hou
    kind = getattr(hou.geometryType, _GEOMETRY_TYPE[group_type_of(node, parm)])
    flat = node.generateInputGroupMenu(0, group_types=(kind,), parm=hou_parm)
    return list(zip(flat[0::2], flat[1::2]))

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The menu of a group picker (``Parm`` kind ``group``): the groups of the first input, as the Group field of
Houdini's own SOPs lists them.

Houdini does the listing (``hou.SopNode.generateInputGroupMenu``, the call behind the stock Group menus), so the
entries, the order and the "already picked" marks are the standard ones. A group whose name is an encoded A3OB
selection name is listed by Houdini with the decoded P3D name as the label and the group name as the token, so the
dropdown is readable with no extra work. This module picks the element types and takes the groups out that are not the
artist's own (task #90).

A picker of a fixed type ``points`` is a rigging group field (``rig_weights.group_mask`` reads it): it lists the point
groups and the primitive groups, each name once, and hides

- the bone groups: a name that is a joint of a skeleton on one of the other inputs (case ignored, the decoded name is
  compared), or that has a weight attribute ``a3ob_w_<group>`` on the points of the first input. With no skeleton
  connected only the weight attributes tell; a bone whose weights are all 1 has no such attribute and is listed until a
  skeleton is connected. Typing a bone group name by hand still works;
- the helper groups of A3OB: a name that starts with ``a3ob_`` (``a3ob_sharp``, the sharp edge group of the importer,
  is the one that exists now). The ``ComponentNN`` groups of the Components node are the artist's tool and stay.

A picker that follows a Group Type menu (Material Assign) lists what Houdini lists for that type, unchanged.
Needs ``hou`` (at call time).
"""

__all__ = ["group_type_of", "menu_items", "hidden_groups", "bone_names"]

_GEOMETRY_TYPE = {"prims": "Primitives", "points": "Points"}
HELPER_PREFIX = "a3ob_"


def group_type_of(node, parm) -> str:
    """``prims`` or ``points``: the fixed type of the picker, or the value of the Group Type menu it follows.
    A value the menu does not know (a typed one) counts as ``prims``, as the cooks do."""
    if parm.group_follow:
        follow = node.parm(parm.group_follow)
        value = follow.eval() if follow is not None else parm.group_type
        return "points" if value in ("points", "point") else "prims"
    return parm.group_type


def bone_names(node) -> set:
    """The joint names (lower case) of the skeletons on the inputs after the first. An input that is a Rig Fit output gives
    its fitted skeleton part; any other input counts as a skeleton when its points have a ``name`` attribute."""
    from . import rig_weights
    found = set()
    for index in range(1, len(node.inputs())):
        try:
            geo = node.inputGeometry(index)
            if geo is not None and geo.findPrimAttrib(rig_weights.PART_ATTRIB) is not None:
                geo = rig_weights.read_part(node.name(), geo, rig_weights.PART_SKELETON)
            if geo is not None and geo.findPointAttrib("name") is not None:
                found.update(point.stringAttribValue("name").lower() for point in geo.points())
        except Exception:  # an input that does not cook gives no names; the menu still works
            continue
    return found


def hidden_groups(node, geo) -> set:
    """The group names (as Houdini holds them) that the menu hides: bone groups and A3OB helper groups. ``geo`` is the
    first input's geometry (None: nothing is hidden)."""
    from . import names
    hidden = set()
    if geo is None:
        return hidden
    bones = bone_names(node)
    weighted = {a.name()[len(names.POINT_WEIGHT_PREFIX):] for a in geo.pointAttribs() if a.name().startswith(names.POINT_WEIGHT_PREFIX)}
    for group in list(geo.pointGroups()) + list(geo.primGroups()) + list(geo.edgeGroups()):
        token = group.name()
        if token.startswith(HELPER_PREFIX) or token in weighted or names.decode_group_name(token).lower() in bones:
            hidden.add(token)
    return hidden


def menu_items(node, parm, hou_parm=None):
    """``[(token, label), ...]`` for the picker ``parm`` (a ``params.Parm``) on ``node``. No input gives no items."""
    import hou
    kind = group_type_of(node, parm)
    if parm.group_follow:  # typed by a menu: what Houdini lists for that type
        flat = node.generateInputGroupMenu(0, group_types=(getattr(hou.geometryType, _GEOMETRY_TYPE[kind]),), parm=hou_parm)
        return list(zip(flat[0::2], flat[1::2]))
    flat = node.generateInputGroupMenu(0, group_types=(hou.geometryType.Points, hou.geometryType.Primitives), parm=hou_parm)
    hidden = hidden_groups(node, node.inputGeometry(0))
    seen, items = set(), []
    for token, label in zip(flat[0::2], flat[1::2]):
        if token in hidden or token in seen or token in ("", "_separator_"):  # (the separator Houdini puts between the types)
            continue
        seen.add(token)
        items.append((token, label))
    return items

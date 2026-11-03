# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Helpers for the nodes that edit a geometry they were given (LOD Properties, Material Assign, ...).

Needs a Houdini geometry at call time; importing the module does not import ``hou``. ``copy_input`` makes the node's output a copy of its input. The detail helpers replace a value or an
array in place, which ``p3d_import.hou_util`` does not need (the importer only ever writes into empty geometry).
"""

from typing import Sequence

from .errors import UserError
from .messages import format_problem

__all__ = ["copy_input", "read_detail", "has_detail", "set_detail", "set_detail_array", "remove_detail"]


def copy_input(node, node_name: str, index: int = 0):
    """Make the node's output a copy of input ``index`` and return the output geometry.

    The copy keeps everything: detail, point, vertex and primitive attributes (also 64-bit ones), groups, edge groups
    and packed primitives. Raises ``UserError`` when the input is not connected."""
    source = node.inputGeometry(index) if len(node.inputs()) > index and node.inputs()[index] is not None else None
    if source is None:
        raise UserError(format_problem(node_name, "no geometry is connected to input %d" % (index + 1),
                                       "Connect a LOD geometry (for example from Extract LOD)"))
    geo = node.geometry()
    geo.copy(source)
    return geo


def has_detail(geo, name: str) -> bool:
    return geo.findGlobalAttrib(name) is not None


def read_detail(geo, name: str, default=None):
    """The value of a detail attribute (arrays come back as tuples), or ``default`` when it does not exist."""
    attrib = geo.findGlobalAttrib(name)
    return default if attrib is None else geo.attribValue(attrib)


def set_detail(geo, name: str, value) -> None:
    """Set a detail int, float or string. Makes the attribute when it is missing."""
    import hou
    if geo.findGlobalAttrib(name) is None:
        geo.addAttrib(hou.attribType.Global, name, value)
    geo.setGlobalAttribValue(name, value)


def set_detail_array(geo, name: str, kind: str, values: Sequence) -> None:
    """Set a detail array (``kind`` is ``"int"``, ``"float"`` or ``"string"``). Makes the attribute when missing."""
    import hou
    if geo.findGlobalAttrib(name) is None:
        data = {"int": hou.attribData.Int, "float": hou.attribData.Float, "string": hou.attribData.String}[kind]
        geo.addArrayAttrib(hou.attribType.Global, name, data)
    geo.setGlobalAttribValue(name, list(values))


def remove_detail(geo, name: str) -> None:
    """Delete a detail attribute if it exists."""
    attrib = geo.findGlobalAttrib(name)
    if attrib is not None:
        attrib.destroy()

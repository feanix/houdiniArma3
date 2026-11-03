# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Small helpers for writing attributes on a ``hou.Geometry``.

Needs ``hou``. The importer and the later nodes use these so that every attribute is made the same way.
"""

from typing import Sequence

import hou

__all__ = ["add_detail", "add_detail_array", "with_float64_vertex_attribs"]


def add_detail(geo, name: str, value) -> None:
    """A detail attribute holding one int, float or string."""
    geo.addAttrib(hou.attribType.Global, name, value)
    geo.setGlobalAttribValue(name, value)


def add_detail_array(geo, name: str, kind: str, values: Sequence) -> None:
    """A detail attribute holding an array. ``kind`` is ``"int"``, ``"float"`` or ``"string"``."""
    data = {"int": hou.attribData.Int, "float": hou.attribData.Float, "string": hou.attribData.String}[kind]
    geo.addArrayAttrib(hou.attribType.Global, name, data)
    geo.setGlobalAttribValue(name, list(values))


def with_float64_vertex_attribs(geo, names: Sequence[str]):
    """Return a copy of ``geo`` that also has float64 vertex attributes (3 components) with the given names.

    HOM ``addAttrib`` only makes float32 (spec 4.3, U2). The Attribute Create verb can make 64-bit attributes on a
    standalone geometry (U7). The copy keeps point and prim order. With no names, ``geo`` is returned as is.
    """
    if not names:
        return geo
    zero = hou.Vector4(0, 0, 0, 0)
    rows = tuple({
        "enable#": 1, "name#": name, "class#": 3, "type#": 0, "size#": 3, "precision#": 3, "typeinfo#": 0,
        "existing#": 3, "writevalues#": 1, "value#v": zero, "default#v": zero,
    } for name in names)
    verb = hou.sopNodeTypeCategory().nodeVerb("attribcreate::2.0")
    verb.setParms({"numattr": rows})
    result = hou.Geometry()
    verb.execute(result, [geo])
    for name in names:
        result.findVertexAttrib(name).setOption("type", "texturecoord")
    return result

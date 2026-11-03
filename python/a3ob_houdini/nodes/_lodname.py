# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The display name of the LOD a geometry holds (ux-tools.md 3.2), for node messages. Not a node (the name starts
with an underscore, so ``nodes.KINDS`` skips it)."""

from typing import Optional

from a3ob_core.p3d.lod_types import LodType, format_lod_name

__all__ = ["lod_name", "lod_type"]


def lod_type(geo) -> Optional[int]:
    """The detail attribute ``a3ob_lod_type`` of a LOD geometry, or None when it has none."""
    if geo.findGlobalAttrib("a3ob_lod_type") is None:
        return None
    return geo.intAttribValue("a3ob_lod_type")


def lod_name(geo) -> Optional[str]:
    """``Geometry``, ``Resolution 1`` and so on, or None when the geometry is not marked as a LOD."""
    number = lod_type(geo)
    if number is None:
        return None
    resolution = geo.floatAttribValue("a3ob_lod_resolution") if geo.findGlobalAttrib("a3ob_lod_resolution") else 0.0
    try:
        return format_lod_name(LodType(number), resolution)
    except ValueError:
        return None

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Same rule as A3OB ``io/import_armature.py`` (``extract_pivot_coords``, ``read_pivots``); the code is our own.
"""The pivot positions of a pivots P3D.

A pivots file is an editable P3D whose Memory LOD has one named selection per bone. The pivot of a bone is the first
point of its selection (the lowest point number). Some selections have several points at one spot; the first one wins.
Bone names are case-insensitive; the result keeps the spelling of the file and ``plan_skeleton`` matches ignoring case.

The positions are returned as they are in the file, ``(x, y, z)``. Mapping them to Houdini axes is the caller's job.
"""

from typing import Dict, Tuple

from ..p3d import LodType, P3DModel, read_p3d_file
from .model_cfg import CfgError

__all__ = ["pivot_positions", "read_pivots_file"]


def pivot_positions(model: P3DModel) -> Dict[str, Tuple[float, float, float]]:
    """``{selection name as written: (x, y, z)}`` from the first Memory LOD. Raises ``CfgError`` without one."""
    memory = next((lod for lod in model.lods if lod.lod_type == LodType.MEMORY), None)
    if memory is None:
        raise CfgError("the pivots file has no Memory LOD. The bone pivots are the selections of that LOD")
    found: Dict[str, Tuple[float, float, float]] = {}
    for selection in memory.selections():
        members = selection.members()
        if members:
            found.setdefault(selection.name, memory.point(members[0]))
    return found


def read_pivots_file(path: str) -> Dict[str, Tuple[float, float, float]]:
    """Read a pivots P3D and return its pivots. Raises ``CfgError`` for a file that cannot be read."""
    try:
        model = read_p3d_file(path, strict=False)
    except OSError as ex:
        raise CfgError("the pivots file %s cannot be opened (%s)" % (path, ex.strerror or ex)) from None
    except ValueError as ex:  # P3DError is a ValueError (checked below)
        raise CfgError("the pivots file %s cannot be read: %s" % (path, ex)) from None
    return pivot_positions(model)

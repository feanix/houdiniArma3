# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The decisions of LOD collection (spec 3.2) that need no ``hou``.

``classify`` says what a geometry is from facts about it, and ``group_prims`` groups PackedFragment prims that share
one source detail into one LOD. ``collect`` (needs ``hou``) reads the facts and acts on the answers.
"""

import base64
import binascii
from typing import List, Optional, Sequence, Tuple

__all__ = ["LOD", "P3D", "classify", "group_prims", "decode_trailing"]

#: A geometry that is one LOD; a geometry of packed LOD prims.
LOD = "lod"
P3D = "p3d"


def classify(has_lod_type: bool, prim_count: int, packed_count: int, point_count: int,
             has_prim_lod_type: bool) -> Tuple[str, str]:
    """``(kind, problem)``: ``kind`` is ``LOD``, ``P3D`` or ``""``; ``problem`` is the error text when ``kind`` is empty.

    - ``has_lod_type``: the geometry has the detail attribute ``a3ob_lod_type`` (a LOD geometry).
    - ``packed_count``: how many of the ``prim_count`` prims are packed.
    - ``has_prim_lod_type``: ``a3ob_lod_type`` is a primitive attribute (several LODs unpacked at once).
    """
    if has_lod_type:
        return LOD, ""
    if has_prim_lod_type:
        return "", ("a3ob_lod_type is a primitive attribute, which is what unpacking several LODs at once gives. "
                    "Unpack one LOD at a time (Extract LOD does this)")
    if prim_count == 0:
        if point_count:
            return "", "the geometry has points but no LOD data (no a3ob_lod_type) and no packed LODs"
        return "", "the geometry is empty"
    if packed_count == prim_count:
        return P3D, ""
    if packed_count:
        return "", "the geometry mixes packed LODs with loose polygons or points. Unpack or blast them first"
    return "", "the geometry is not a LOD (it has no a3ob_lod_type) and holds no packed LODs"


def group_prims(infos: Sequence[Tuple[bool, Optional[int]]]) -> List[List[int]]:
    """Group prim indices into LODs (spec 3.2). ``infos[i]`` is ``(is_fragment, geometry id)``.

    PackedFragment prims with equal geometry id are one LOD, placed at the first fragment. Every other prim is a LOD of
    its own."""
    groups: List[List[int]] = []
    by_geometry = {}
    for index, (is_fragment, geometry_id) in enumerate(infos):
        if is_fragment:
            if geometry_id in by_geometry:
                by_geometry[geometry_id].append(index)
                continue
            group = by_geometry[geometry_id] = [index]
            groups.append(group)
        else:
            groups.append([index])
    return groups


def decode_trailing(text: str) -> Optional[bytes]:
    """The bytes of ``a3ob_file_trailing`` (base64), or None when it is not valid base64."""
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None

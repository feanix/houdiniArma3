# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The bone names are those of the Arma 3 man skeleton (a3ob_houdini/p3d_export/rig_bones.py); the grouping is our own.
"""The body regions of the Arma man skeleton, by bone name, for the digests that sort points by their dominant bone (task #81).

A region is a name the digests print; the rule is one function, so every digest means the same by ``arm_l``.
"""

from typing import Tuple

__all__ = ["REGIONS", "region_of", "BETWEEN", "NO_WEIGHT"]

#: Printed in this order.
REGIONS: Tuple[str, ...] = ("head", "torso", "arm_l", "hand_l", "arm_r", "hand_r", "leg_l", "foot_l", "leg_r", "foot_r")
BETWEEN = "between regions"
NO_WEIGHT = "no weight"


def region_of(bone: str) -> str:
    """The region of a bone name (ignoring case); ``""`` for a bone that is in no region (Camera, weapon, launcher, face)."""
    name = bone.lower()
    if name in ("head", "neck", "neck1"):
        return "head"
    if name == "pelvis" or name.startswith("spine"):
        return "torso"
    for side, prefix in (("l", "left"), ("r", "right")):
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if rest.startswith("hand"):
            return "hand_" + side
        if rest.startswith(("shoulder", "arm", "forearm")):
            return "arm_" + side
        if rest.startswith(("upleg", "leg")):
            return "leg_" + side
        if rest.startswith(("foot", "toebase")):
            return "foot_" + side
    return ""

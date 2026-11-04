# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Character rigging code that needs no Houdini (docs/design/character-rigging.md).

This is the one part of ``a3ob_core`` that may use numpy (owner decision D37, Q14): the rig nodes only run inside
Houdini, and hython has numpy. The skeleton code below does not need it yet.

- ``model_cfg``: a small ``model.cfg`` reader (``CfgSkeletons``: bones, ``skeletonInherit``, ``isDiscrete``).
- ``pivots``: the pivot positions of a pivots P3D (the Memory LOD selections).
- ``skeleton``: puts the two together into the joint list of the Rig Skeleton node.
"""

from .model_cfg import CfgClass, CfgError, CfgFile, Skeleton, parse_cfg, read_cfg_file, resolve_skeleton, skeleton_names
from .pivots import pivot_positions, read_pivots_file
from .skeleton import SKIP_PATTERNS, Joint, SkeletonPlan, check_bones, plan_skeleton

__all__ = [
    "CfgClass", "CfgError", "CfgFile", "Skeleton", "parse_cfg", "read_cfg_file", "resolve_skeleton", "skeleton_names",
    "pivot_positions", "read_pivots_file", "SKIP_PATTERNS", "Joint", "SkeletonPlan", "check_bones", "plan_skeleton",
]

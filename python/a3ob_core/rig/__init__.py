# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Character rigging code that needs no Houdini (docs/design/character-rigging.md).

This is the one part of ``a3ob_core`` that may use numpy (owner decision D37, Q14): the rig nodes only run inside
Houdini, and hython has numpy. The skeleton code below does not need it yet.

- ``model_cfg``: a small ``model.cfg`` reader (``CfgSkeletons``: bones, ``skeletonInherit``, ``isDiscrete``).
- ``pivots``: the pivot positions of a pivots P3D (the Memory LOD selections).
- ``skeleton``: puts the two together into the joint list of the Rig Skeleton node.
- ``weights``: bone weights as numpy matrices: name matching, the capture lists, and the export clean-up (task #62).
- ``transfer``: weight transfer by closest point and inpainting, with the rigid and non-rigid layers (task #65).
"""

from .model_cfg import CfgClass, CfgError, CfgFile, Skeleton, parse_cfg, read_cfg_file, resolve_skeleton, skeleton_names
from .pivots import pivot_positions, read_pivots_file
from .skeleton import SKIP_PATTERNS, Joint, SkeletonPlan, check_bones, plan_skeleton
from .transfer import (ORIGIN_COPIED, ORIGIN_FILLED, ORIGIN_NONE, ORIGIN_NONRIGID, ORIGIN_RIGID, Piece, Settings,
                       TransferResult, is_twist, transfer_layers, twist_targets)
from .weights import (CAPT_FLOATS, GRID, CleanStats, capture_lists, clean_weights, grid_units, inverse_rest_floats,
                      match_bone_names, matrix_from_capture)

__all__ = [
    "ORIGIN_COPIED", "ORIGIN_FILLED", "ORIGIN_NONE", "ORIGIN_NONRIGID", "ORIGIN_RIGID", "Piece", "Settings",
    "TransferResult", "is_twist", "transfer_layers", "twist_targets",
    "CAPT_FLOATS", "GRID", "CleanStats", "capture_lists", "clean_weights", "grid_units", "inverse_rest_floats",
    "match_bone_names", "matrix_from_capture",
    "CfgClass", "CfgError", "CfgFile", "Skeleton", "parse_cfg", "read_cfg_file", "resolve_skeleton", "skeleton_names",
    "pivot_positions", "read_pivots_file", "SKIP_PATTERNS", "Joint", "SkeletonPlan", "check_bones", "plan_skeleton",
]

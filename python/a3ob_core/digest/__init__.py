# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Digests of the rig data, with no Houdini (docs/design/digests.md, task #81).

A digest turns scene data into information: figures, counts and percentiles, each beside its reference value, on a page of
at most 25 lines of 80 columns. It never judges. The code here takes numpy arrays and small records; the Houdini side
(``a3ob_houdini.digest``) reads them from a scene with the project's own readers and calls these functions.

- ``page``: the page, the units, ``not recorded``, the exit codes.
- ``skeleton``: ``skeleton_page`` and ``compare_page`` (joint lengths and the body measures).
- ``deform``: ``deform_page`` (the edge-length ratio of mesh B against mesh A).
- ``weights``: ``weights_page`` (influences, sums, points per bone).
- ``feet``: ``feet_page`` (sole pitch, heel and toe) and ``wrap_distance_page`` (the wrapped body against the user's; task #82).
- ``pose``: ``fingers_page`` (bend per knuckle) and ``head_page`` (the turn of a bone between two meshes; task #83).
- ``landmarks``: ``landmarks_page`` (landmarks beside the mesh and the Arma sample).
- ``regions``: the body region of a bone.
"""

from . import deform, feet, landmarks, pose, regions, skeleton, weights
from .deform import deform_page, edge_ratios
from .feet import feet_page, wrap_distance_page
from .pose import fingers_page, head_page
from .landmarks import SampleSide, landmarks_page
from .page import (EXIT_ANSWERED, EXIT_COULD_NOT, EXIT_WRONG_QUESTION, MAX_COLUMNS, MAX_LINES, NR, CouldNotAnswer,
                   DigestError, Page, WrongQuestion)
from .skeleton import Skel, Source, compare_page, measure_values, skeleton_page
from .weights import weight_figures, weights_page

__all__ = [
    "feet", "feet_page", "wrap_distance_page", "pose", "fingers_page", "head_page", "deform", "landmarks", "regions", "skeleton", "weights", "deform_page", "edge_ratios", "SampleSide", "landmarks_page",
    "EXIT_ANSWERED", "EXIT_COULD_NOT", "EXIT_WRONG_QUESTION", "MAX_COLUMNS", "MAX_LINES", "NR", "CouldNotAnswer",
    "DigestError", "Page", "WrongQuestion", "Skel", "Source", "compare_page", "measure_values", "skeleton_page",
    "weight_figures", "weights_page",
]

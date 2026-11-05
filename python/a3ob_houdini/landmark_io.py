# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the landmark names are the ones of ``a3ob_core.rig.landmarks``.
"""What Rig Fit and Rig Landmarks share (docs/design/character-rigging.md 6.2, task #74, D42).

- The landmark positions travel on the mesh as detail attributes: ``a3ob_lm_chin``, ``a3ob_lm_shoulder_l`` and so on, one
  float vector (X, Y, Z) each (``write_landmarks``, ``read_landmarks``). A landmark that is not set has no attribute.
- The sample side of the auto-guess (``sample_side``): the skeleton and the sample body with its template. Rig Fit fits with
  it and Rig Landmarks guesses with it, so there is one copy of the guess (``a3ob_core.rig.guess_landmarks``).
"""

import dataclasses
from typing import Dict

import numpy as np

from a3ob_core.rig import BONE_OF, LANDMARK_NAMES, drop_small_pieces, guess_landmarks, make_template

from . import rig_weights
from .errors import UserError
from .messages import format_problem

try:
    import hou
except ImportError:  # the pure helpers are also read under plain Python
    hou = None

__all__ = ["ATTRIB_PREFIX", "MIN_SKIN_PIECE", "attrib_name", "write_landmarks", "read_landmarks", "SampleSide",
           "sample_side", "guess_for_mesh"]

ATTRIB_PREFIX = "a3ob_"
#: A piece of the sample body with fewer points than this is not skin (its proxy triangles).
MIN_SKIN_PIECE = 10


def attrib_name(landmark: str) -> str:
    """``lm_chin`` -> ``a3ob_lm_chin``."""
    return ATTRIB_PREFIX + landmark


def write_landmarks(geo, positions: Dict[str, tuple]) -> None:
    """One detail vector per landmark in ``positions`` (a landmark that is missing gets no attribute)."""
    for name in LANDMARK_NAMES:
        if name not in positions:
            continue
        value = tuple(float(v) for v in positions[name])
        if geo.findGlobalAttrib(attrib_name(name)) is None:
            geo.addAttrib(hou.attribType.Global, attrib_name(name), (0.0, 0.0, 0.0))
        geo.setGlobalAttribValue(attrib_name(name), value)


def read_landmarks(geo) -> Dict[str, np.ndarray]:
    """The landmarks that the geometry carries, ``{landmark name: position}``."""
    found = {}
    for name in LANDMARK_NAMES:
        attrib = geo.findGlobalAttrib(attrib_name(name))
        if attrib is not None and attrib.size() == 3:
            found[name] = np.array(geo.attribValue(attrib_name(name)), dtype=np.float64)
    return found


@dataclasses.dataclass
class SampleSide:
    skeleton: object
    #: lower-case joint name -> index
    index: Dict[str, int]
    rest: np.ndarray
    positions: np.ndarray
    tris: np.ndarray
    skin: np.ndarray
    found: object
    template: object


def sample_side(name: str, skeleton_geo, sample_geo) -> SampleSide:
    """Read the skeleton (input 2) and the sample body (input 3) and make the template of the guess. Raises
    ``UserError`` with a fix when an input is wrong. ``name`` is the node's name in the messages."""
    skeleton = rig_weights.read_skeleton(name, skeleton_geo)
    if sample_geo is None:
        raise UserError(format_problem(name, "an input is not connected", "Connect the skeleton to input 2 and the "
                                       "sample body to input 3"))
    if any(parent >= index for index, parent in enumerate(skeleton.parents)):
        raise UserError(format_problem(name, "a joint comes before its parent in the skeleton", "Use the output of Rig Skeleton"))
    index = {}
    for number, bone in enumerate(skeleton.names):
        index.setdefault(bone.lower(), number)
    missing = sorted({bone for bone in BONE_OF.values() if bone and bone.lower() not in index})
    if missing or "head" not in index:
        raise UserError(format_problem(name, "the skeleton has no joint %s" % ", ".join(missing + ([] if "head" in index else ["head"])),
                                       "Use the Arma man skeleton (OFP2_ManSkeleton)"))
    positions, tris = rig_weights.read_mesh(sample_geo)
    tris = drop_small_pieces(len(positions), tris, MIN_SKIN_PIECE)
    if not len(tris):
        raise UserError(format_problem(name, "the sample body has no polygons", "Use a body with polygons on input 3"))
    skin = positions[np.unique(tris)]
    found = rig_weights.read_selections(name, sample_geo, skeleton.names)
    if not found.groups:
        raise UserError(format_problem(name, "no selection of the sample body is named like a joint of the skeleton",
                                       "Connect a body with bone weights to input 3"))
    rest = np.array(skeleton.positions, dtype=np.float64)
    joints = {bone.lower(): rest[number] for number, bone in enumerate(skeleton.names)}
    try:
        template = make_template(joints, skin)
    except ValueError as problem:
        raise UserError(format_problem(name, str(problem))) from None
    return SampleSide(skeleton, index, rest, positions, tris, skin, found, template)


def guess_for_mesh(skin_in: np.ndarray, side: SampleSide):
    """``(positions, notes)``: the auto-guess of all 14 landmarks on the skin points of the user's mesh. This is the one
    call of the guess that Rig Fit and Rig Landmarks both make."""
    return guess_landmarks(skin_in, side.template)

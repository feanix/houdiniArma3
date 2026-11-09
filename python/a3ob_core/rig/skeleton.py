# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The bone checks follow A3OB ``utilities/validator.py`` (skeleton validation); the code is our own.
"""The joint list of the Rig Skeleton node: a ``model.cfg`` skeleton plus the pivots, with no Houdini.

Rules (docs/design/character-rigging.md 6.2, owner decisions D36 and D37 Q4):

- ``Face_*`` and ``Eye*`` bones are left out (the face is out of scope).
- A bone with no pivot in the pivots file is left out and reported (the sample has none for ``Camera``, ``weapon`` and
  ``launcher``).
- A pivot that belongs to no bone is ignored and reported (the sample's ``Hips``).
- A joint keeps the ``model.cfg`` spelling of its bone name. Parents come before children, as in the file.
"""

import dataclasses
import fnmatch
from typing import Dict, List, Sequence, Tuple

__all__ = ["SKIP_PATTERNS", "Joint", "SkeletonPlan", "check_bones", "plan_skeleton"]

#: Bones that are never rigged (D36): the 36 face bones and the eyes.
SKIP_PATTERNS = ("Face_*", "Eye*")

#: RTM files hold bone names in 32 bytes, so a name has at most 31 characters (A3OB ``data_rtm.py``).
MAX_NAME = 31


@dataclasses.dataclass
class Joint:
    name: str
    #: Index of the parent in ``SkeletonPlan.joints``, or -1 for a root. Always smaller than this joint's own index.
    parent: int
    #: The pivot as in the pivots file, ``(x, y, z)``.
    position: Tuple[float, float, float]


@dataclasses.dataclass
class SkeletonPlan:
    joints: List[Joint]
    #: Bones left out by the Face_* / Eye* rule.
    skipped: List[str]
    #: Bones left out because the pivots file has no pivot for them.
    missing: List[str]
    #: Pivot names that match no bone (ignored).
    unknown: List[str]
    #: Joints whose model.cfg parent was left out; they hang from the nearest joint above it (or become roots).
    reparented: List[str]
    #: Bones in the skeleton before anything was left out.
    bone_count: int


def check_bones(bones: Sequence[Tuple[str, str]]) -> List[str]:
    """Problems that make a skeleton unusable, as sentences (empty list: fine). Checks A3OB's skeleton rules: names are
    unique ignoring case, ASCII and at most 31 characters; a parent is listed before its children."""
    problems: List[str] = []
    position: Dict[str, int] = {}
    for index, (bone, _) in enumerate(bones):
        key = bone.lower()
        if not bone:
            problems.append("bone %d has an empty name" % (index + 1))
        elif key in position:
            problems.append("the bone %s is listed twice (names are the same ignoring case)" % bone)
        else:
            position[key] = index
        if not bone.isascii():
            problems.append("the bone name %s has characters that are not ASCII" % bone)
        if len(bone) > MAX_NAME:
            problems.append("the bone name %s has %d characters; RTM animation files allow %d" % (bone, len(bone), MAX_NAME))
    for index, (bone, parent) in enumerate(bones):
        if not parent:
            continue
        where = position.get(parent.lower())
        if where is None:
            problems.append("the parent %s of the bone %s is not a bone of this skeleton" % (parent, bone))
        elif where >= index:
            problems.append("the bone %s is listed before its parent %s" % (bone, parent))
    return problems


def _skipped(name: str, patterns: Sequence[str]) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, pattern.lower()) for pattern in patterns)


def plan_skeleton(bones: Sequence[Tuple[str, str]], pivots: Dict[str, Tuple[float, float, float]],
                  skip: Sequence[str] = SKIP_PATTERNS) -> SkeletonPlan:
    """The joints for the bones ``(name, parent)`` of a skeleton and the ``pivots`` of a pivots file (names as written).

    Call ``check_bones`` first: this assumes parents come before children."""
    by_name: Dict[str, Tuple[float, float, float]] = {}
    for name, position in pivots.items():
        by_name.setdefault(name.lower(), position)
    parent_of = {bone.lower(): parent.lower() for bone, parent in bones}
    plan = SkeletonPlan([], [], [], [], [], len(bones))
    placed: Dict[str, int] = {}  # lower-case bone name -> joint index
    for bone, parent in bones:
        if _skipped(bone, skip):
            plan.skipped.append(bone)
            continue
        position = by_name.get(bone.lower())
        if position is None:
            plan.missing.append(bone)
            continue
        above = parent.lower()
        while above and above not in placed:
            above = parent_of.get(above, "")
        if above != parent.lower():
            plan.reparented.append(bone)
        plan.joints.append(Joint(bone, placed[above] if above else -1, position))
        placed[bone.lower()] = len(plan.joints) - 1
    known = {bone.lower() for bone, _ in bones}
    plan.unknown = [name for name in pivots if name.lower() not in known]
    return plan

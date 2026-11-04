# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/data.py, v2.5.1 (9f2b8bc): the bone names of ``ofp2_manskeleton`` (the dict keys; the
# parents are left out). A3OB: MrClock (present add-on).
"""The bone names of the standard Arma 3 man skeleton, for the weight rules T2-WGT4, T2-WGTSUM and T2-WGTNONE
(docs/design/character-rigging.md 4.4, task #63). No ``hou`` here.

P3D Export and Validate have no skeleton input. So the rules decide that a selection is a bone selection when its name
matches one of these bones, ignoring case. The list is the same 103 bones as ``OFP2_ManSkeleton`` in the Arma 3 Samples
``model.cfg`` (a test compares them when the Samples are installed). A character with another skeleton is not checked.
"""

from typing import FrozenSet

__all__ = ["OFP2_MANSKELETON_BONES", "bone_name_set"]

OFP2_MANSKELETON_BONES = (
    "Pelvis", "Spine", "Spine1", "Spine2", "Spine3", "Camera", "weapon", "launcher", "neck", "neck1", "head",
    "Face_Hub", "Face_Jawbone", "Face_Jowl", "Face_chopRight", "Face_chopLeft", "Face_LipLowerMiddle",
    "Face_LipLowerLeft", "Face_LipLowerRight", "Face_Chin", "Face_Tongue", "Face_CornerRight",
    "Face_CheekSideRight", "Face_CornerLeft", "Face_CheekSideLeft", "Face_CheekFrontRight", "Face_CheekFrontLeft",
    "Face_CheekUpperRight", "Face_CheekUpperLeft", "Face_LipUpperMiddle", "Face_LipUpperRight", "Face_LipUpperLeft",
    "Face_NostrilRight", "Face_NostrilLeft", "Face_Forehead", "Face_BrowFrontRight", "Face_BrowFrontLeft",
    "Face_BrowMiddle", "Face_BrowSideRight", "Face_BrowSideLeft", "Face_Eyelids", "Face_EyelidUpperRight",
    "Face_EyelidUpperLeft", "Face_EyelidLowerRight", "Face_EyelidLowerLeft", "EyeLeft", "EyeRight", "LeftShoulder",
    "LeftArm", "LeftArmRoll", "LeftForeArm", "LeftForeArmRoll", "LeftHand", "LeftHandRing", "LeftHandRing1",
    "LeftHandRing2", "LeftHandRing3", "LeftHandPinky1", "LeftHandPinky2", "LeftHandPinky3", "LeftHandMiddle1",
    "LeftHandMiddle2", "LeftHandMiddle3", "LeftHandIndex1", "LeftHandIndex2", "LeftHandIndex3", "LeftHandThumb1",
    "LeftHandThumb2", "LeftHandThumb3", "RightShoulder", "RightArm", "RightArmRoll", "RightForeArm",
    "RightForeArmRoll", "RightHand", "RightHandRing", "RightHandRing1", "RightHandRing2", "RightHandRing3",
    "RightHandPinky1", "RightHandPinky2", "RightHandPinky3", "RightHandMiddle1", "RightHandMiddle2",
    "RightHandMiddle3", "RightHandIndex1", "RightHandIndex2", "RightHandIndex3", "RightHandThumb1",
    "RightHandThumb2", "RightHandThumb3", "LeftUpLeg", "LeftUpLegRoll", "LeftLeg", "LeftLegRoll", "LeftFoot",
    "LeftToeBase", "RightUpLeg", "RightUpLegRoll", "RightLeg", "RightLegRoll", "RightFoot", "RightToeBase",
)

_LOWER: FrozenSet[str] = frozenset(name.lower() for name in OFP2_MANSKELETON_BONES)


def bone_name_set() -> FrozenSet[str]:
    """The bone names in lower case."""
    return _LOWER

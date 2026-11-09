# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/data.py, v2.5.1 (9f2b8bc): the bone names of ``ofp2_manskeleton`` (the dict keys); the
# parents are the bone hierarchy of the same skeleton. A3OB: MrClock (present add-on).
"""The bone names of the standard Arma 3 man skeleton, for the weight rules T2-WGT4, T2-WGTSUM and T2-WGTNONE
(docs/design/character-rigging.md 4.4, task #63). No ``hou`` here.

P3D Export and Validate have no skeleton input. So the rules decide that a selection is a bone selection when its name
matches one of these bones, ignoring case. The list is the same 103 bones, with the same parents, as
``OFP2_ManSkeleton`` in the Arma 3 Samples ``model.cfg`` (a test compares them when the Samples are installed). The
Model Cfg node (task #95) writes the parents. A character with another skeleton is not checked.
"""

from typing import FrozenSet

__all__ = ["OFP2_MANSKELETON_BONES", "OFP2_MANSKELETON_PARENTS", "bone_name_set"]

#: ``bone>parent;`` for the 103 bones, in the order of the Samples file. The root has an empty parent.
_TABLE = (
    "Pelvis>;  Spine>Pelvis;  Spine1>Spine; "
    "Spine2>Spine1;  Spine3>Spine2;  Camera>Pelvis; "
    "weapon>Spine1;  launcher>Spine1;  neck>Spine3; "
    "neck1>neck;  head>neck1;  Face_Hub>head; "
    "Face_Jawbone>Face_Hub;  Face_Jowl>Face_Jawbone;  Face_chopRight>Face_Jawbone; "
    "Face_chopLeft>Face_Jawbone;  Face_LipLowerMiddle>Face_Jawbone;  Face_LipLowerLeft>Face_Jawbone; "
    "Face_LipLowerRight>Face_Jawbone;  Face_Chin>Face_Jawbone;  Face_Tongue>Face_Jawbone; "
    "Face_CornerRight>Face_Hub;  Face_CheekSideRight>Face_CornerRight;  Face_CornerLeft>Face_Hub; "
    "Face_CheekSideLeft>Face_CornerLeft;  Face_CheekFrontRight>Face_Hub;  Face_CheekFrontLeft>Face_Hub; "
    "Face_CheekUpperRight>Face_Hub;  Face_CheekUpperLeft>Face_Hub;  Face_LipUpperMiddle>Face_Hub; "
    "Face_LipUpperRight>Face_Hub;  Face_LipUpperLeft>Face_Hub;  Face_NostrilRight>Face_Hub; "
    "Face_NostrilLeft>Face_Hub;  Face_Forehead>Face_Hub;  Face_BrowFrontRight>Face_Forehead; "
    "Face_BrowFrontLeft>Face_Forehead;  Face_BrowMiddle>Face_Forehead;  Face_BrowSideRight>Face_Forehead; "
    "Face_BrowSideLeft>Face_Forehead;  Face_Eyelids>Face_Hub;  Face_EyelidUpperRight>Face_Hub; "
    "Face_EyelidUpperLeft>Face_Hub;  Face_EyelidLowerRight>Face_Hub;  Face_EyelidLowerLeft>Face_Hub; "
    "EyeLeft>Face_Hub;  EyeRight>Face_Hub;  LeftShoulder>Spine3; "
    "LeftArm>LeftShoulder;  LeftArmRoll>LeftArm;  LeftForeArm>LeftArmRoll; "
    "LeftForeArmRoll>LeftForeArm;  LeftHand>LeftForeArmRoll;  LeftHandRing>LeftHand; "
    "LeftHandRing1>LeftHandRing;  LeftHandRing2>LeftHandRing1;  LeftHandRing3>LeftHandRing2; "
    "LeftHandPinky1>LeftHandRing;  LeftHandPinky2>LeftHandPinky1;  LeftHandPinky3>LeftHandPinky2; "
    "LeftHandMiddle1>LeftHand;  LeftHandMiddle2>LeftHandMiddle1;  LeftHandMiddle3>LeftHandMiddle2; "
    "LeftHandIndex1>LeftHand;  LeftHandIndex2>LeftHandIndex1;  LeftHandIndex3>LeftHandIndex2; "
    "LeftHandThumb1>LeftHand;  LeftHandThumb2>LeftHandThumb1;  LeftHandThumb3>LeftHandThumb2; "
    "RightShoulder>Spine3;  RightArm>RightShoulder;  RightArmRoll>RightArm; "
    "RightForeArm>RightArmRoll;  RightForeArmRoll>RightForeArm;  RightHand>RightForeArmRoll; "
    "RightHandRing>RightHand;  RightHandRing1>RightHandRing;  RightHandRing2>RightHandRing1; "
    "RightHandRing3>RightHandRing2;  RightHandPinky1>RightHandRing;  RightHandPinky2>RightHandPinky1; "
    "RightHandPinky3>RightHandPinky2;  RightHandMiddle1>RightHand;  RightHandMiddle2>RightHandMiddle1; "
    "RightHandMiddle3>RightHandMiddle2;  RightHandIndex1>RightHand;  RightHandIndex2>RightHandIndex1; "
    "RightHandIndex3>RightHandIndex2;  RightHandThumb1>RightHand;  RightHandThumb2>RightHandThumb1; "
    "RightHandThumb3>RightHandThumb2;  LeftUpLeg>Pelvis;  LeftUpLegRoll>LeftUpLeg; "
    "LeftLeg>LeftUpLegRoll;  LeftLegRoll>LeftLeg;  LeftFoot>LeftLegRoll; "
    "LeftToeBase>LeftFoot;  RightUpLeg>Pelvis;  RightUpLegRoll>RightUpLeg; "
    "RightLeg>RightUpLegRoll;  RightLegRoll>RightLeg;  RightFoot>RightLegRoll; "
    "RightToeBase>RightFoot; "
)

OFP2_MANSKELETON_PARENTS = tuple(
    tuple(part.strip() for part in item.split(">")) for item in _TABLE.replace("\n", " ").split(";") if item.strip())
OFP2_MANSKELETON_BONES = tuple(bone for bone, _ in OFP2_MANSKELETON_PARENTS)

_LOWER: FrozenSet[str] = frozenset(name.lower() for name in OFP2_MANSKELETON_BONES)


def bone_name_set() -> FrozenSet[str]:
    """The bone names in lower case."""
    return _LOWER

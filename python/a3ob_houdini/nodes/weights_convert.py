# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Weights Convert node (docs/design/character-rigging.md 4.2, 4.3 and 6.2, task #62): P3D bone selections to KineFX
capture weights (``boneCapture``) for a skeleton, and back.

The work is in ``a3ob_core.rig.weights`` (the maths) and ``rig_weights`` (reading and writing the geometry). This module
reads the parameters and says what happened.
"""

from .. import rig_weights
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS"]

NAME = "Weights Convert"

SPEC = NodeSpec(
    kind="weights_convert",
    label="Weights Convert",
    summary="Converts bone weights between P3D selections (point groups) and KineFX capture weights for the skeleton.",
    inputs=(2, 2),
    input_labels=("Geometry", "Skeleton"),
    input_help=("The LOD geometry whose bone weights are converted (for example from Extract LOD).",
                "The skeleton, for example from Rig Skeleton. Its joint names decide which selections are bones."),
    parms=(
        Parm("mode", "Mode", "menu", "tocapture", "Which way to convert.", items=(
            ("tocapture", "Selections to Capture"),
            ("toselections", "Capture to Selections"),
        ), item_help=(
            ("tocapture", "Reads the bone selections (point groups, with their a3ob_w weights) and writes capture "
                          "weights (boneCapture) for the skeleton. Joint Deform can use them."),
            ("toselections", "Reads the capture weights and writes bone selections. A selection that is not a bone "
                             "is left alone."),
        )),
        Parm("case", "Selection Names", "menu", "cfg", "How a bone selection is named when it is written. Only used "
             "by Capture to Selections.", items=(
                 ("cfg", "model.cfg spelling"),
                 ("keep", "Keep existing spelling"),
             ), item_help=(
                 ("cfg", "Names the selection like the bone in the skeleton (for example head). A selection that "
                         "already exists under another spelling (Head) is renamed."),
                 ("keep", "A selection that already exists keeps its name. A new one is named like the bone."),
             ), show_when=(("mode", ("toselections",)),)),
    ),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    skeleton = rig_weights.read_skeleton(NAME, node.inputGeometry(1))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    if values["mode"] == "toselections":
        _to_selections(geo, skeleton, values["case"] == "keep", report)
    else:
        _to_capture(geo, skeleton, report)


def _to_capture(geo, skeleton, report: Report) -> None:
    found = rig_weights.read_selections(NAME, geo, skeleton.names)
    weights = found.weights
    used = int((weights.sum(axis=1) > 0).sum())
    missing = [skeleton.names[i] for i in range(len(skeleton.names)) if i not in found.groups]
    rig_weights.write_capture(geo, skeleton, weights)
    report.info("Capture weights for %d joints from %d bone selections. %d of %d points have a weight."
                % (len(skeleton.names), len(found.groups), used, len(weights)))
    if found.other:
        report.info("%s are not bones of the skeleton and were left alone." % list_elements("Selections", found.other))
    if missing:
        report.info("No selection for %s." % list_elements("joints", missing))
    if found.zero_members:
        report.info("%d members with weight 0 (byte 255 in the file) have no weight here and were ignored."
                    % found.zero_members)
    if not found.groups:
        report.warning(format_problem(NAME, "no selection of the geometry is named like a joint of the skeleton",
                                      "Check that input 2 is the skeleton of this character"))


def _to_selections(geo, skeleton, keep: bool, report: Report) -> None:
    weights, covered = rig_weights.read_capture(NAME, geo, skeleton.names)
    existing = rig_weights.read_selections(NAME, geo, skeleton.names)
    renamed = rig_weights.write_selections(geo, skeleton.names, weights, existing.groups, covered, keep)
    written = [i for i in covered if (weights[:, i] > 0).any() or i in existing.groups]
    report.info("%d bone selections written from the capture weights of %d points." % (len(written), len(weights)))
    if renamed:
        report.info("%d selections were renamed to the spelling of the skeleton." % renamed)
    if existing.other:
        report.info("%s are not bones and were left alone." % list_elements("Selections", existing.other))


BUTTONS = {}
MENUS = {}

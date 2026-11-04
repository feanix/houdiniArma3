# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the landmark names are the ones of ``a3ob_core.rig.landmarks``.
"""The Rig Landmarks node (docs/design/character-rigging.md 6.2, task #71): one point-group field per landmark that
Rig Fit reads. It writes the ``lm_*`` point groups on the user's mesh, so the artist selects the points in the viewport
(the select button of each field) and needs no Group node per landmark.

A field that is empty writes no group, and Rig Fit then guesses that landmark. A group that is already on the mesh and
whose field is empty is kept; a field that is set replaces it.
"""

from a3ob_core.rig import LANDMARKS

from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS", "label_of"]

NAME = "Rig Landmarks"


def label_of(landmark) -> str:
    """The plain-words label of a landmark: ``lm_shoulder_l`` is "Left Shoulder"."""
    words = landmark.name[len("lm_"):].split("_")
    side = {"l": "Left", "r": "Right"}.get(words[-1])
    return " ".join(([side] if side else []) + [w.capitalize() for w in words if not (side and w == words[-1])])


def _parm(landmark) -> Parm:
    return Parm(landmark.name, label_of(landmark), "group", "",
                "%s Select the points in the viewport with the arrow button. The average of the points is the landmark. "
                "Leave it empty to let Rig Fit guess this landmark." % landmark.text, group_type="points")


SPEC = NodeSpec(
    kind="rig_landmarks",
    label="Rig Landmarks",
    summary="Marks the landmarks of your character (chin, groin, shoulders, elbows, wrists, hips, knees, ankles) for "
            "Rig Fit, one field each.",
    inputs=(1, 1),
    input_labels=("Your mesh",),
    input_help=("Your character mesh, the one that goes into Rig Fit.",),
    parms=tuple(_parm(item) for item in LANDMARKS),
)


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    if node.inputGeometry(0) is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect your mesh"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    chosen = {}
    empty = []
    for item in LANDMARKS:
        text = values[item.name].strip()
        if not text:
            continue
        try:
            points = geo.globPoints(text)
        except hou.OperationFailed:
            raise UserError(format_problem(NAME, "the group %r of %s cannot be read" % (text, label_of(item)),
                                           "Use a point group name or point numbers")) from None
        if points:
            chosen[item.name] = points
        else:
            empty.append(item)
    for name, points in chosen.items():  # all fields are read before any group changes, so a field may name a group
        old = geo.findPointGroup(name)
        if old is not None:
            old.destroy()
        geo.createPointGroup(name).add(points)
    for item in empty:
        report.warning(format_problem(NAME, "%s: the field matches no points, so no group was made" % label_of(item),
                                      "Select the points again, or clear the field"))
    set_names = [label_of(item) for item in LANDMARKS if item.name in chosen]
    kept = [label_of(item) for item in LANDMARKS if item.name not in chosen and geo.findPointGroup(item.name) is not None]
    guessed = [label_of(item) for item in LANDMARKS if item.name not in chosen and label_of(item) not in kept]
    report.info("%d of %d landmarks set: %s." % (len(set_names), len(LANDMARKS), ", ".join(set_names) or "none"))
    if kept:
        report.info("Kept from the input (field empty): %s." % ", ".join(kept))
    if guessed:
        report.info("Left to the guess in Rig Fit: %s." % ", ".join(guessed))


BUTTONS = {}
MENUS = {}

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the landmark names are the ones of ``a3ob_core.rig.landmarks``.
"""The Rig Landmarks node (docs/design/character-rigging.md 6.2, tasks #71, #74 and #70, D42): the 18 landmarks that Rig Fit
reads, each one a position (X, Y, Z) that the artist places by hand.

The positions start from the auto-guess (the Guess Landmarks button runs the same guess code as Rig Fit), then they are
dragged as labelled markers in the viewport (the node's viewer state, ``rig_landmark_state``) or typed. A landmark can also
be set from the points selected in the viewport (their average; only the position is kept). With Mirror on, a right
landmark follows its left partner, mirrored about X = 0 (Left is +X).

The output is the input mesh, unchanged, with one detail vector per landmark that is set (``landmark_io``). A landmark that
is not set (all three numbers zero) gives no attribute, so Rig Fit guesses it, and the node warns that it needs the guess.
"""

import logging
import traceback

import numpy as np

from a3ob_core.rig import LANDMARK_PAIRS, LANDMARKS, follow_mirror, is_set, mirror_x

from .. import landmark_io, rig_weights
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "MENUS", "BUTTONS", "label_of", "short_name", "positions_of", "set_landmark", "sync_mirror",
           "guess_landmarks_on", "average_selected", "set_from_selection", "match_sides"]

NAME = "Rig Landmarks"
LOGGER = logging.getLogger("a3ob")
STATE_MODULE = "a3ob_houdini.rig_landmark_state"


def short_name(landmark_name: str) -> str:
    """The parameter name of a landmark: ``lm_shoulder_l`` is ``shoulder_l``."""
    return landmark_name[len("lm_"):]


def label_of(landmark) -> str:
    """The plain-words label of a landmark: ``lm_shoulder_l`` is "Left Shoulder"."""
    words = short_name(landmark.name).split("_")
    side = {"l": "Left", "r": "Right"}.get(words[-1])
    return " ".join(([side] if side else []) + [w.capitalize() for w in words if not (side and w == words[-1])])


#: ``short name -> partner short name`` for the six left/right pairs, in both directions.
PARTNER = {}
for _left, _right in LANDMARK_PAIRS:
    PARTNER[short_name(_left)] = short_name(_right)
    PARTNER[short_name(_right)] = short_name(_left)
RIGHT = frozenset(short_name(right) for _, right in LANDMARK_PAIRS)
BY_SHORT = {short_name(item.name): item for item in LANDMARKS}


def _parm(landmark) -> Parm:
    short = short_name(landmark.name)
    extra = ""
    if short in RIGHT:
        extra = " While Mirror is on this one follows the left side and cannot be edited."
    return Parm(short, label_of(landmark), "float", (0.0, 0.0, 0.0),
                "%s Its position (X, Y, Z). Drag its marker in the viewport (select the node, move the mouse over the "
                "viewport and press Enter), type the numbers, or use Guess Landmarks. All zero means not set: Rig Fit "
                "then guesses this landmark.%s" % (landmark.text, extra),
                size=3, action="moved", enable_when=(("mirror", ("0",)),) if short in RIGHT else ())


SPEC = NodeSpec(
    kind="rig_landmarks",
    label="Rig Landmarks",
    summary="Places the landmarks of your character (chin, groin, shoulders, elbows, wrists, hips, knees, ankles, middle "
            "fingers, thumbs) for Rig Fit, as positions that you drag in the viewport.",
    inputs=(1, 3),
    input_labels=("Your mesh", "Skeleton", "Sample body"),
    input_help=("Your character mesh, the one that goes into Rig Fit. It comes out unchanged.",
                "Optional. The Arma skeleton, for Guess Landmarks (the same one that goes into Rig Fit).",
                "Optional. The sample body with its bone weights, for Guess Landmarks (the same one that goes into Rig Fit)."),
    parms=(
        Parm("guess", "Guess Landmarks", "button", "",
             "Fills all 18 positions with the auto-guess of Rig Fit for your mesh. Connect the skeleton to input 2 and "
             "the sample body to input 3 first. It replaces the positions that are set.", action="guess"),
        Parm("mirror", "Mirror", "toggle", True,
             "Left is the +X side. On: the right landmarks follow the left ones, mirrored about the X = 0 plane, so "
             "dragging a left marker moves its right partner, and the right fields cannot be edited. Off: both sides "
             "are free.", action="mirror_changed"),
    ) + tuple(_parm(item) for item in LANDMARKS) + (
        Parm("leftToRight", "Match Left to Right", "button", "",
             "Once: puts each right landmark at the mirror image of its left partner (mirrored about X = 0, Left is "
             "+X). Chin and groin stay as they are, and a left landmark that is not set is skipped. Disabled while "
             "Mirror is on, because the right side already follows the left.", action="left_to_right",
             enable_when=(("mirror", ("0",)),)),
        Parm("rightToLeft", "Match Right to Left", "button", "",
             "Once: puts each left landmark at the mirror image of its right partner (mirrored about X = 0, Left is "
             "+X). Chin and groin stay as they are, and a right landmark that is not set is skipped. With Mirror on, "
             "the right side then follows the new left side.", action="right_to_left"),
        Parm("which", "Landmark", "menu", "chin", "The landmark that the two buttons below work on.",
             items=tuple((short_name(item.name), label_of(item)) for item in LANDMARKS)),
        Parm("guessone", "Guess This Landmark", "button", "",
             "Puts the landmark chosen above back to its auto-guess (needs inputs 2 and 3 like Guess Landmarks). With "
             "Mirror on, its partner follows.", action="guessone"),
        Parm("fromsel", "Set From Selected Points", "button", "",
             "Puts the landmark chosen above at the average of the points that are selected in the viewport. Only the "
             "position is kept, not the selection. Useful for the chin and the groin. With Mirror on, its partner "
             "follows.", action="fromsel"),
    ),
    viewer_state=STATE_MODULE,
)


# ---------------------------------------------------------------------------------------------------------------------
# Reading and writing the fields
# ---------------------------------------------------------------------------------------------------------------------

def positions_of(node) -> dict:
    """``{short name: (x, y, z)}`` of all fields as they stand."""
    return {short: tuple(float(v) for v in node.parmTuple(short).eval()) for short in BY_SHORT}


def _write(node, short: str, value) -> None:
    node.parmTuple(short).set(tuple(float(v) for v in value))


def mirrored_on(node) -> bool:
    return bool(node.evalParm("mirror"))


def set_landmark(node, short: str, value) -> None:
    """Set one field; with Mirror on its partner is set to the mirror image (a left or a right one)."""
    _write(node, short, value)
    if short in PARTNER and mirrored_on(node):
        _write(node, PARTNER[short], mirror_x(value))


def sync_mirror(node) -> None:
    """With Mirror on make every right field the mirror image of its left partner (the left one rules; when only the
    right one is set the left one follows it). Writes only a field that changes."""
    if not mirrored_on(node):
        return
    current = positions_of(node)
    for left, right in LANDMARK_PAIRS:
        a, b = short_name(left), short_name(right)
        new_left, new_right = follow_mirror(current[a], current[b])
        if new_left != current[a]:
            _write(node, a, new_left)
        if new_right != current[b]:
            _write(node, b, new_right)


# ---------------------------------------------------------------------------------------------------------------------
# Cook
# ---------------------------------------------------------------------------------------------------------------------

def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    if node.inputGeometry(0) is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect your mesh"))
    placed = {short: values[short] for short in BY_SHORT}
    if values["mirror"]:
        for left, right in LANDMARK_PAIRS:
            a, b = short_name(left), short_name(right)
            placed[a], placed[b] = follow_mirror(placed[a], placed[b])
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    landmark_io.write_landmarks(geo, {item.name: placed[short_name(item.name)] for item in LANDMARKS
                                      if is_set(placed[short_name(item.name)])})
    missing = [label_of(item) for item in LANDMARKS if not is_set(placed[short_name(item.name)])]
    report.info("%d of %d landmarks are set." % (len(LANDMARKS) - len(missing), len(LANDMARKS)))
    if missing:
        report.warning(format_problem(NAME, "%d landmark%s not set (%s)" % (
            len(missing), " is" if len(missing) == 1 else "s are", ", ".join(missing)),
            "Press Guess Landmarks (with the skeleton on input 2 and the sample body on input 3), or place them. Rig "
            "Fit guesses the landmarks that are not set"))


# ---------------------------------------------------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------------------------------------------------

def match_sides(node, from_left: bool) -> None:
    """Match Left to Right (``from_left``) or Match Right to Left: copy each landmark of one side to its partner, mirrored
    about X = 0. A source that is not set is skipped. Chin and groin have no partner and are not touched."""
    current = positions_of(node)
    for left, right in LANDMARK_PAIRS:
        a, b = short_name(left), short_name(right)
        source, target = (a, b) if from_left else (b, a)
        if is_set(current[source]):
            _write(node, target, mirror_x(current[source]))


def _guess(node, known=None):
    """The auto-guess for the mesh on input 1: ``{landmark name: position}``. ``known`` are landmarks already placed (the
    hand landmarks are guessed from a known wrist). Raises ``UserError``."""
    mesh = node.inputGeometry(0)
    if mesh is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect your mesh"))
    skeleton, sample = node.inputGeometry(1), node.inputGeometry(2)
    if skeleton is None or sample is None:
        raise UserError(format_problem(NAME, "the guess needs the skeleton and the sample body",
                                       "Connect the skeleton to input 2 and the sample body to input 3, the same ones "
                                       "that go into Rig Fit"))
    side = landmark_io.sample_side(NAME, skeleton, sample)
    positions, tris = rig_weights.read_mesh(mesh)
    if not len(tris):
        raise UserError(format_problem(NAME, "your mesh has no polygons", "Use a mesh with polygons on input 1"))
    guessed, _notes = landmark_io.guess_for_mesh(positions[np.unique(tris)], side, known)
    return guessed


def guess_landmarks_on(node, only: str = "") -> None:
    """Guess Landmarks (``only`` empty) or Guess This Landmark (``only`` is a short name). One landmark is guessed from the
    wrist and elbow that are set; all of them are guessed from the guess."""
    known = None
    if only:
        known = {"lm_" + short: np.array(value) for short, value in positions_of(node).items() if is_set(value)}
    guessed = _guess(node, known)
    if only:
        set_landmark(node, only, guessed["lm_" + only])
        return
    for name, value in guessed.items():
        _write(node, short_name(name), value)
    sync_mirror(node)


def average_selected(selection) -> tuple:
    """The average position of the points of a ``hou.GeometrySelection``. Raises ``UserError`` when nothing is selected."""
    points = []
    for geo_node, one in zip(selection.nodes(), selection.selections()):
        geo = geo_node.geometry()
        points.extend(tuple(point.position()) for point in one.points(geo))
    if not points:
        raise UserError(format_problem(NAME, "no points are selected", "Select points in the viewport (Select tool, "
                                       "point mode) on the geometry of this node, then press the button again"))
    return tuple(float(v) for v in np.mean(np.array(points, dtype=np.float64), axis=0))


def set_from_selection(node, selection=None) -> None:
    """Set From Selected Points: the landmark chosen in the menu goes to the average of the selected points."""
    if selection is None:
        viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer) if hou is not None and hou.isUIAvailable() else None
        if viewer is None:
            raise UserError(format_problem(NAME, "there is no viewport", "Open a Scene View and select points in it"))
        selection = viewer.currentGeometrySelection()
    set_landmark(node, node.evalParm("which"), average_selected(selection))


def _tell(ex: UserError) -> None:
    if hou is not None and hou.isUIAvailable():
        hou.ui.displayMessage(str(ex), title=NAME, severity=hou.severityType.Error)
    else:
        LOGGER.error("%s", ex)


def _button(action):
    def run(kwargs):
        try:
            action(kwargs["node"])
        except UserError as ex:
            _tell(ex)
    return run


def moved(kwargs) -> None:
    """A position field was edited: keep the mirror side in step. Never raises."""
    try:
        sync_mirror(kwargs["node"])
    except Exception:
        LOGGER.error("rig_landmarks moved failed:\n%s", traceback.format_exc())


BUTTONS = {
    "guess": _button(lambda node: guess_landmarks_on(node)),
    "guessone": _button(lambda node: guess_landmarks_on(node, node.evalParm("which"))),
    "fromsel": _button(lambda node: set_from_selection(node)),
    "left_to_right": _button(lambda node: match_sides(node, True)),
    "right_to_left": _button(lambda node: match_sides(node, False)),
    "mirror_changed": moved,
    "moved": moved,
}
MENUS = {}

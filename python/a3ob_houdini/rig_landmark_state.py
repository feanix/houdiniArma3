# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the landmark names are the ones of ``a3ob_core.rig.landmarks``.
"""The viewer state of the Rig Landmarks node (task #74, D42): select the node, move the mouse over the viewport and press
Enter, and every landmark that is set is a marker that you drag.

Mechanism: a Python viewer state with one dynamic ``translate`` handle per landmark, and one text drawable per landmark for
its label. Both are drawn on top of the mesh, so the markers stay visible over a solid mesh. The handles are not bound to
the fields one to one, because the mirror rule needs code: dragging a left marker also writes its right partner, and the
right handles are hidden while Mirror is on (their labels stay, at the mirrored place). The state is registered by the
asset (``hda_source``: ViewerStateModule and DefaultState sections), which calls ``create_template`` here.

The decisions are small functions that need no viewer (``marker_rows``, ``handle_position``, ``apply_handle_edit``); the
``State`` class only passes the viewer's events to them. Only a session with the Houdini window runs ``State``.
"""

from a3ob_core.rig import LANDMARKS, follow_mirror, is_set

from .nodes import rig_landmarks as lm

try:
    import hou
except ImportError:  # the helpers are also read under plain Python
    hou = None

__all__ = ["HANDLE_PREFIX", "create_template", "marker_rows", "handle_position", "apply_handle_edit", "State"]

HANDLE_PREFIX = "lm_"
#: Label colours (red, green, blue): left side, right side, middle line.
COLORS = {"left": (1.0, 0.45, 0.35), "right": (0.4, 0.65, 1.0), "middle": (1.0, 0.9, 0.3)}


def _side(short: str) -> str:
    return "right" if short in lm.RIGHT else ("left" if short in lm.PARTNER else "middle")


def marker_rows(positions: dict, mirror: bool) -> list:
    """What the viewport shows, one row per landmark: ``(short name, label, position, set, draggable, side)``.

    ``positions`` is ``{short name: (x, y, z)}`` (the fields). With Mirror on the right markers sit at the mirror image of
    the left ones and cannot be dragged (drag the left one); a landmark that is not set has no marker."""
    shown = dict(positions)
    if mirror:
        for left, right in lm.LANDMARK_PAIRS:
            a, b = lm.short_name(left), lm.short_name(right)
            shown[a], shown[b] = follow_mirror(shown[a], shown[b])
    rows = []
    for item in LANDMARKS:
        short = lm.short_name(item.name)
        value = shown[short]
        rows.append((short, lm.label_of(item), value, is_set(value), not (mirror and short in lm.RIGHT), _side(short)))
    return rows


def handle_position(node, short: str) -> tuple:
    """``(position, visible)`` for the handle of a landmark, from the node's fields."""
    for name, _label, value, is_placed, draggable, _side_name in marker_rows(lm.positions_of(node), lm.mirrored_on(node)):
        if name == short:
            return value, bool(is_placed and draggable)
    raise KeyError(short)


def apply_handle_edit(node, short: str, value) -> None:
    """The user dragged the handle of a landmark to ``value``: write the field (and, with Mirror on, its partner)."""
    lm.set_landmark(node, short, value)


class State(object):
    """The Rig Landmarks viewer state. Created by Houdini when the state is entered with the node selected."""

    def __init__(self, state_name, scene_viewer):
        self.state_name = state_name
        self.scene_viewer = scene_viewer
        self.node = None
        self.handles = {}
        self.labels = {item.name[len("lm_"):]: hou.TextDrawable(scene_viewer, "%s_%s" % (state_name, item.name))
                       for item in LANDMARKS}

    def onEnter(self, kwargs):
        self.node = kwargs["node"]
        self.handles = {short: hou.Handle(self.scene_viewer, HANDLE_PREFIX + short) for short in self.labels}
        for handle in self.handles.values():
            handle.update()
        for label in self.labels.values():
            label.show(True)

    def onExit(self, kwargs):
        for label in self.labels.values():
            label.show(False)

    def onInterrupt(self, kwargs):
        self.onExit(kwargs)

    def onResume(self, kwargs):
        self.onEnter(kwargs)

    def onStateToHandle(self, kwargs):
        """A field changed (or ``update``): move the handle to the field."""
        short = kwargs["handle"][len(HANDLE_PREFIX):]
        node = kwargs.get("node") or self.node
        value, visible = handle_position(node, short)
        parms = kwargs["parms"]
        parms["tx"], parms["ty"], parms["tz"] = value
        handle = self.handles.get(short)
        if handle is not None:
            handle.show(visible)

    def onBeginHandleToState(self, kwargs):
        self.scene_viewer.beginStateUndo("Move landmark")

    def onHandleToState(self, kwargs):
        """The user dragged a handle: write the field."""
        short = kwargs["handle"][len(HANDLE_PREFIX):]
        parms = kwargs["parms"]
        apply_handle_edit(kwargs.get("node") or self.node, short, (parms["tx"], parms["ty"], parms["tz"]))

    def onEndHandleToState(self, kwargs):
        self.scene_viewer.endStateUndo()
        for handle in self.handles.values():  # the right handles follow the left one that moved
            handle.update()

    def onDraw(self, kwargs):
        """Draw the label of every landmark that is set, next to its marker, on top of the mesh."""
        node = self.node
        if node is None:
            return
        draw_handle = kwargs["draw_handle"]
        world = node.creator().worldTransform() if hasattr(node, "creator") else hou.Matrix4(1)
        viewport = self.scene_viewer.curViewport()
        for short, label, value, is_placed, draggable, side in marker_rows(lm.positions_of(node), lm.mirrored_on(node)):
            if not is_placed:
                continue
            screen = viewport.mapToScreen(hou.Vector3(value) * world)
            self.labels[short].draw(draw_handle, {
                "text": label if draggable else label + " (mirrored)",
                "translate": hou.Vector3(screen[0] + 8.0, screen[1] + 8.0, 0.0),
                "color1": hou.Color(*COLORS[side])})


def create_template(state_name):
    """The ``hou.ViewerStateTemplate`` of the state (called from the asset's ViewerStateModule section)."""
    template = hou.ViewerStateTemplate(state_name, "Rig Landmarks", hou.sopNodeTypeCategory())
    template.bindFactory(State)
    for item in LANDMARKS:
        template.bindHandle("translate", HANDLE_PREFIX + item.name[len("lm_"):])
    return template

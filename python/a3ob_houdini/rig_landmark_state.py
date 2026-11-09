# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no skeleton fitting; the landmark names are the ones of ``a3ob_core.rig.landmarks``.
"""The viewer state of the Rig Landmarks node (task #74, D42): select the node, move the mouse over the viewport and press
Enter, and every landmark that is set is a marker that you drag. The groin and the two hips (task #85) are drawn as guide
shapes instead of spheres: a wedge (upside-down V, apex at the groin) and a short cylinder per thigh top (centre at the hip,
aimed at the knee), as wireframe so the mesh shows through, in the side colour.

Mechanism: a Python viewer state with one dynamic ``translate`` handle per landmark, and one text drawable per landmark for
its label, plus one sphere drawable per landmark (the label colour). All are drawn on top of the mesh, so the markers stay
visible over a solid mesh. While the state is active the mesh is shown as ghosted wireframe (the shading of the viewport's
display sets is switched and put back on exit). The handles are not bound to
the fields one to one, because the mirror rule needs code: dragging a left marker also writes its right partner, and the
right handles are hidden while Mirror is on (their labels stay, at the mirrored place). The state is registered by the
asset (``hda_source``: ViewerStateModule and DefaultState sections), which calls ``create_template`` here.

The decisions are small functions that need no viewer (``marker_rows``, ``handle_position``, ``apply_handle_edit``); the
``State`` class only passes the viewer's events to them. Only a session with the Houdini window runs ``State``.
"""

import math

from a3ob_core.rig import LANDMARKS, follow_mirror, is_set

from .nodes import rig_landmarks as lm

try:
    import hou
except ImportError:  # the helpers are also read under plain Python
    hou = None

__all__ = ["HANDLE_PREFIX", "create_template", "marker_rows", "sphere_markers", "shape_specs", "shape_markers","handle_position", "apply_handle_edit", "State"]

HANDLE_PREFIX = "lm_"
#: Label colours (red, green, blue): left side, right side, middle line.
COLORS = {"left": (1.0, 0.45, 0.35), "right": (0.4, 0.65, 1.0), "middle": (1.0, 0.9, 0.3)}


#: Radius of the marker sphere, in scene units (metres): about 1.75 cm, sensible for a character.
SPHERE_RADIUS = 0.0175
#: The Display Sets whose shading is switched to a see-through mode while the state is active (names of ``hou.displaySetType``).
GHOST_DISPLAY_SETS = ("DisplayModel", "CurrentModel")
#: The shading used for the mesh while the state is active (name in ``hou.glShadingType``): wireframe, hidden wires dimmed.
GHOST_SHADING = "WireGhost"


def marker_color(side: str) -> tuple:
    """The colour (red, green, blue) of the label and the sphere of a landmark on ``side``."""
    return COLORS[side]


#: The landmarks drawn as a guide shape (task #85) instead of a sphere: the crotch and the two hip joints.
SHAPED = ("groin", "hip_l", "hip_r")
#: Size rule. One length ``scale`` sets every shape: the distance between the two hip joints (they are about 18 to 25 cm
#: apart on a character, whatever its height). Without both hips it is 0.4 of the hip-to-knee distance, and without
#: that a plain 0.2 m, so the shapes have a sensible size before the knees are set.
DEFAULT_SCALE = 0.2
#: Hip cylinder: the wanted radius and length as shares of ``scale`` (the radius is cut down so the two never touch).
HIP_RADIUS_SHARE = 0.4
HIP_LENGTH_SHARE = 2.2
#: Groin wedge: half width, height and half depth as shares of ``scale``.
GROIN_HALF_WIDTH_SHARE = 0.4
GROIN_HEIGHT_SHARE = 0.7
GROIN_HALF_DEPTH_SHARE = 0.5
#: The two cylinders keep this share of their gap between them as radius each (below 0.5, so a gap is left).
HIP_GAP_SHARE = 0.45
HIP_MIN_RADIUS = 0.01
CYLINDER_SIDES = 12


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b, k=1.0):
    return (a[0] + k * b[0], a[1] + k * b[1], a[2] + k * b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a):
    return math.sqrt(_dot(a, a))


def _unit(a, fallback=(0.0, -1.0, 0.0)):
    size = _length(a)
    return fallback if size < 1e-9 else (a[0] / size, a[1] / size, a[2] / size)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def segment_distance(p0, p1, q0, q1) -> float:
    """The shortest distance between the segments ``p0-p1`` and ``q0-q1`` (closest points of two segments)."""
    d1, d2, r = _sub(p1, p0), _sub(q1, q0), _sub(p0, q0)
    a, e, f = _dot(d1, d1), _dot(d2, d2), _dot(d2, r)
    if a < 1e-12 and e < 1e-12:
        return _length(r)
    if a < 1e-12:
        s, t = 0.0, min(1.0, max(0.0, f / e))
    else:
        c = _dot(d1, r)
        if e < 1e-12:
            s, t = min(1.0, max(0.0, -c / a)), 0.0
        else:
            b = _dot(d1, d2)
            denom = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / denom)) if denom > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t, s = 1.0, min(1.0, max(0.0, (b - c) / a))
    return _length(_sub(_add(p0, d1, s), _add(q0, d2, t)))


def guide_scale(shown: dict) -> float:
    """The length that sizes the guide shapes. ``shown`` is ``{short name: position}`` for the landmarks that are set
    (Mirror already applied): the hip-to-hip distance; else 0.4 of the hip-to-knee distance; else ``DEFAULT_SCALE``."""
    if "hip_l" in shown and "hip_r" in shown:
        gap = _length(_sub(shown["hip_l"], shown["hip_r"]))
        if gap > 1e-4:
            return gap
    for side in ("l", "r"):
        if "hip_" + side in shown and "knee_" + side in shown:
            leg = _length(_sub(shown["hip_" + side], shown["knee_" + side]))
            if leg > 1e-4:
                return 0.4 * leg
    return DEFAULT_SCALE


def hip_axis(shown: dict, side: str) -> tuple:
    """The unit direction a hip cylinder is aimed in: hip to the knee of that side, straight down if the knee is not set."""
    if "knee_" + side in shown:
        return _unit(_sub(shown["knee_" + side], shown["hip_" + side]))
    return (0.0, -1.0, 0.0)


def hip_size(shown: dict) -> tuple:
    """``(radius, length)`` shared by both hip cylinders. The radius is cut so the two cylinders (as capsules around their
    axes) leave a gap: the distance between the two axis segments is measured and ``HIP_GAP_SHARE`` of it is the limit."""
    scale = guide_scale(shown)
    radius = HIP_RADIUS_SHARE * scale
    length = HIP_LENGTH_SHARE * radius
    if "hip_l" in shown and "hip_r" in shown:
        ends = []
        for side in ("l", "r"):
            half = tuple(0.5 * length * c for c in hip_axis(shown, side))
            ends.append((_sub(shown["hip_" + side], half), _add(shown["hip_" + side], half)))
        gap = segment_distance(ends[0][0], ends[0][1], ends[1][0], ends[1][1])
        radius = min(radius, HIP_GAP_SHARE * gap)
        radius = max(radius, HIP_MIN_RADIUS)
        length = HIP_LENGTH_SHARE * radius
    return radius, length


def shape_specs(positions: dict, mirror: bool) -> list:
    """The guide shapes to draw, one dict per landmark of ``SHAPED`` that is set: ``short``, ``kind`` (``"wedge"`` for the
    groin, ``"cylinder"`` for a hip), ``centre`` (the landmark: the apex of the wedge, the middle of the cylinder),
    ``axis`` (unit direction of the cylinder; ``None`` for the wedge, which always points down) ``size`` (wedge: half
    width, height, half depth; cylinder: radius, length) and ``color`` (the side colour)."""
    rows = marker_rows(positions, mirror)
    shown = {row[0]: row[2] for row in rows if row[3]}
    sides = {row[0]: row[5] for row in rows}
    specs = []
    scale = guide_scale(shown)
    radius, length = hip_size(shown)
    for short in SHAPED:
        if short not in shown:
            continue
        if short == "groin":
            specs.append({"short": short, "kind": "wedge", "centre": shown[short], "axis": None,
                          "size": (GROIN_HALF_WIDTH_SHARE * scale, GROIN_HEIGHT_SHARE * scale,
                                   GROIN_HALF_DEPTH_SHARE * scale), "color": marker_color(sides[short])})
        else:
            specs.append({"short": short, "kind": "cylinder", "centre": shown[short], "axis": hip_axis(shown, short[-1]),
                          "size": (radius, length), "color": marker_color(sides[short])})
    return specs


def wedge_faces(apex, half_width, height, half_depth) -> list:
    """An upside-down V (a triangular prism, ridge front to back): the apex edge at ``apex``, the base ``height`` below it,
    ``half_width`` to each side. Faces are lists of corners in scene units."""
    x, y, z = apex
    a_front, a_back = (x, y, z + half_depth), (x, y, z - half_depth)
    l_front, l_back = (x + half_width, y - height, z + half_depth), (x + half_width, y - height, z - half_depth)
    r_front, r_back = (x - half_width, y - height, z + half_depth), (x - half_width, y - height, z - half_depth)
    return [[a_front, l_front, r_front], [a_back, r_back, l_back], [a_front, a_back, l_back, l_front],
            [a_front, r_front, r_back, a_back], [l_front, l_back, r_back, r_front]]


def cylinder_faces(centre, axis, radius, length, sides=CYLINDER_SIDES) -> list:
    """A cylinder of ``radius`` and ``length`` around ``centre`` along ``axis`` (unit): the side quads and the two caps."""
    helper = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 0.0, 1.0)
    u = _unit(_cross(axis, helper))
    v = _cross(axis, u)
    top, bottom = _add(centre, axis, 0.5 * length), _add(centre, axis, -0.5 * length)
    ring = []
    for index in range(sides):
        angle = 2.0 * math.pi * index / sides
        offset = _add((0.0, 0.0, 0.0), u, radius * math.cos(angle))
        offset = _add(offset, v, radius * math.sin(angle))
        ring.append((_add(top, offset), _add(bottom, offset)))
    faces = [[ring[i][0], ring[i][1], ring[(i + 1) % sides][1], ring[(i + 1) % sides][0]] for i in range(sides)]
    return faces + [[end[0] for end in ring], [end[1] for end in reversed(ring)]]


def shape_markers(positions: dict, mirror: bool) -> list:
    """What the viewer draws for the groin and hips: one ``(short name, faces, colour)`` per shape. ``faces`` are polygons
    (lists of corners) in the node's space; the viewer draws their edges, so the mesh shows through (see ``State``)."""
    out = []
    for spec in shape_specs(positions, mirror):
        if spec["kind"] == "wedge":
            faces = wedge_faces(spec["centre"], *spec["size"])
        else:
            faces = cylinder_faces(spec["centre"], spec["axis"], *spec["size"])
        out.append((spec["short"], faces, spec["color"]))
    return out


def sphere_markers(positions: dict, mirror: bool) -> list:
    """The spheres to draw, one ``(short name, centre, radius, colour)`` per landmark that is set (none for the others).
    The groin and the hips have a guide shape (``shape_markers``) instead."""
    return [(short, value, SPHERE_RADIUS, marker_color(side))
            for short, _label, value, is_placed, _draggable, side in marker_rows(positions, mirror)
            if is_placed and short not in SHAPED]


def ghost_display(settings, shading, display_sets) -> list:
    """Switch the shading of each display set (``hou.GeometryViewportSettings.displaySet`` results) to ``shading``.

    Returns what was there, ``[(display set, old shading)]``, for ``restore_display``."""
    saved = []
    for display_set in display_sets:
        saved.append((display_set, display_set.shadedMode()))
        display_set.setShadedMode(shading)
    return saved


def restore_display(saved: list) -> None:
    """Put back the shading that ``ghost_display`` returned."""
    for display_set, shading in saved:
        display_set.setShadedMode(shading)


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
        self.spheres = {}
        self.shapes = {}
        self.saved_display = []

    def _make_spheres(self):
        """One sphere mesh, built once; one drawable per landmark shares it."""
        if self.spheres:
            return
        verb = hou.sopNodeTypeCategory().nodeVerb("sphere")
        verb.setParms({"type": 1, "rad": (1.0, 1.0, 1.0), "rows": 12, "cols": 16})
        geometry = hou.Geometry()
        verb.execute(geometry, [])
        self.spheres = {short: hou.GeometryDrawable(self.scene_viewer, hou.drawableGeometryType.Face,
                                                    "%s_sphere_%s" % (self.state_name, short), geometry=geometry)
                        for short in self.labels if short not in SHAPED}
        # The guide shapes are wireframe (edges only), so the mesh stays visible inside them; their geometry is rebuilt
        # at every draw from ``shape_markers``.
        self.shapes = {short: hou.GeometryDrawable(self.scene_viewer, hou.drawableGeometryType.Line,
                                                   "%s_shape_%s" % (self.state_name, short), geometry=hou.Geometry())
                       for short in SHAPED}

    def _shape_geometry(self, faces, world):
        geometry = hou.Geometry()
        for face in faces:
            polygon = geometry.createPolygon()
            for corner in face:
                point = geometry.createPoint()
                point.setPosition(hou.Vector3(corner) * world)
                polygon.addVertex(point)
        return geometry

    def _ghost_mesh(self):
        if self.saved_display:
            return
        settings = self.scene_viewer.curViewport().settings()
        sets = [settings.displaySet(getattr(hou.displaySetType, name)) for name in GHOST_DISPLAY_SETS]
        self.saved_display = ghost_display(settings, getattr(hou.glShadingType, GHOST_SHADING), sets)

    def _unghost_mesh(self):
        restore_display(self.saved_display)
        self.saved_display = []

    def onEnter(self, kwargs):
        self.node = kwargs["node"]
        self.handles = {short: hou.Handle(self.scene_viewer, HANDLE_PREFIX + short) for short in self.labels}
        for handle in self.handles.values():
            handle.update()
        for label in self.labels.values():
            label.show(True)
        self._make_spheres()
        for sphere in list(self.spheres.values()) + list(self.shapes.values()):
            sphere.show(True)
        self._ghost_mesh()

    def onExit(self, kwargs):
        for label in self.labels.values():
            label.show(False)
        for sphere in list(self.spheres.values()) + list(self.shapes.values()):
            sphere.show(False)
        self._unghost_mesh()

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
        for short, centre, radius, color in sphere_markers(lm.positions_of(node), lm.mirrored_on(node)):
            center = hou.Vector3(centre) * world
            self.spheres[short].draw(draw_handle, {
                "translate": center, "scale": hou.Vector3(radius, radius, radius),
                "color1": hou.Vector4(color[0], color[1], color[2], 1.0), "fade_factor": 1.0, "use_cd": False})
        guides = shape_markers(lm.positions_of(node), lm.mirrored_on(node))
        shown = [short for short, _faces, _color in guides]
        for short, faces, color in guides:
            drawable = self.shapes[short]
            drawable.setGeometry(self._shape_geometry(faces, world))
            drawable.draw(draw_handle, {"color1": hou.Vector4(color[0], color[1], color[2], 1.0), "fade_factor": 1.0,
                                        "use_cd": False})
        for short, drawable in self.shapes.items():  # a shape that is not set must not stay on screen
            if short not in shown:
                drawable.setGeometry(hou.Geometry())
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

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB v2.5.1 (9f2b8bc): utilities/structure.py:12-17 (clear_components), :19-30 (find_components) and
# :33-69 (component_convex_hull), with the two operators in ui/tool_utilities.py:73-87 (Component Convex Hull) and
# :93-112 (Find Components). The hull maths is in ``hull_math`` (A3OB calls Blender's convex_hull, structure.py:72-80).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""The Components node: makes the ``ComponentNN`` point groups of a Geometry LOD (spec section 10 and 17.1.1, T2-COMP).

Pieces are the polygons linked by shared points (``mass_math.closed_components``). They are numbered in the order of
their first polygon. Packed proxies are not polygons and are never touched.

Hull mode keeps the corner points of each hull with all their point attributes and point groups. The old polygons of
the piece go, and so do its other points. The new triangles have no UVs, normals or materials (new default values),
so give them a texture afterwards with Material Assign.
"""

import re

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from .. import hull_math, mass_math
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "is_component_name"]

NAME = "Components"
#: A3OB structure.py:13 (``re_component.match``: the name only has to start like this).
COMPONENT = re.compile(r"component(\d+)", re.IGNORECASE)
HULL_MINIMUM = 4  # A3OB structure.py:51: pieces with fewer points are proxies

SPEC = NodeSpec(
    kind="components",
    label="Components",
    summary="Makes the component groups (Component01, Component02, ...) of a Geometry LOD, one per connected piece.",
    inputs=(1, 1),
    input_labels=("LOD geometry",),
    input_help=("The Geometry LOD whose pieces become components.",),
    parms=(
        Parm("mode", "Mode", "menu", "find", "What the node does with each connected piece of polygons.", items=(
            ("find", "Find components"),
            ("hull", "Convex hull per piece"),
        ), item_help=(
            ("find", "Makes a point group for every closed piece. Pieces that are not closed get no group. The "
                     "geometry is not changed."),
            ("hull", "Replaces every piece of 4 or more points by its convex hull (closed, made of triangles) and "
                     "makes a point group for it. Smaller pieces, such as proxy triangles, are left as they are. The "
                     "hull keeps the corner points, with their point attributes and point groups (selections). "
                     "The other points of the piece are removed and leave their groups. The new triangles get "
                     "default values: no UVs, normals or materials, so use Material Assign afterwards."),
        )),
        Parm("keep", "Keep Existing Components", "toggle", False,
             "Off: every group called component followed by digits (any capitals) is removed first, as in Object "
             "Builder. On: those groups stay and the new groups are numbered after the highest one."),
    ),
)


def is_component_name(name: str) -> bool:
    """True for a group name the node removes (A3OB's rule: starts with ``component`` and a digit)."""
    return COMPONENT.match(name) is not None


def cook(node, report: Report) -> None:
    """Edit the copy of the input: clear the old components, then find or hull the pieces."""
    values = read_parms(node, SPEC)
    if node.inputGeometry(0) is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect a LOD geometry"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    first = _clear_or_count(geo, values["keep"])
    polygons = [prim for prim in geo.prims() if isinstance(prim, hou.Polygon)]
    corners = [tuple(point.number() for point in prim.points()) for prim in polygons]
    pieces = mass_math.closed_components(corners)
    if values["mode"] == "hull":
        made, left = _hull(geo, polygons, pieces, first)
        what, rest = "hulled", "%d piece%s with fewer than 4 points or no volume left as %s." % (
            left, "" if left == 1 else "s", "it is" if left == 1 else "they are")
    else:
        made = _find(geo, pieces, first)
        left = sum(1 for piece in pieces if not piece.closed)
        what, rest = "", "%d open piece%s skipped." % (left, "" if left == 1 else "s")
    _report(report, made, what, rest if left else "")


def _clear_or_count(geo, keep: bool) -> int:
    """Remove the old component groups (A3OB clear_components) and return the first number to use.
    With ``keep`` they stay and the first number is the highest present one plus 1."""
    highest = 0
    for group in geo.pointGroups():
        match = COMPONENT.match(group.name())
        if match is None:
            continue
        if keep:
            highest = max(highest, int(match.group(1)))
        else:
            group.destroy()
    return highest + 1


def _name(number: int) -> str:
    return "Component%02d" % number


def _find(geo, pieces, first: int):
    """A group for every closed piece. Returns the names made."""
    made = []
    for piece in pieces:
        if piece.closed:
            made.append(_group(geo, first + len(made), piece.points))
    return made


def _group(geo, number: int, point_numbers):
    group = geo.createPointGroup(_name(number))
    points = geo.points()
    group.add([points[n] for n in point_numbers])
    return group.name()


def _hull(geo, polygons, pieces, first: int):
    """Replace every piece by its hull. Returns ``(names made, pieces left alone)``."""
    points = geo.points()
    made = []
    left = 0
    drop_prims = []
    drop_points = []
    for piece in pieces:
        if len(piece.points) < HULL_MINIMUM:
            left += 1
            continue
        positions = [tuple(points[n].position()) for n in piece.points]
        triangles = hull_math.convex_hull(positions)
        if triangles is None:
            left += 1
            continue
        kept = {piece.points[i] for triangle in triangles for i in triangle}
        for triangle in triangles:
            prim = geo.createPolygon()
            for i in triangle:
                prim.addVertex(points[piece.points[i]])
        drop_prims.extend(polygons[n] for n in piece.polygons)
        drop_points.extend(points[n] for n in piece.points if n not in kept)
        made.append(_group(geo, first + len(made), sorted(kept)))
    if drop_prims:
        geo.deletePrims(drop_prims, keep_points=True)
    if drop_points:
        geo.deletePoints(drop_points)
    return made, left


def _report(report: Report, made, what: str, rest: str) -> None:
    if not made:
        report.warning(format_problem(NAME, "no component was made", "Check that the LOD has closed pieces of polygons"))
        if rest:
            report.info(rest)
        return
    span = made[0] if len(made) == 1 else "%s-%s" % (made[0], made[-1])
    text = "%d component%s%s (%s)." % (len(made), "" if len(made) == 1 else "s", " " + what if what else "", span)
    report.info(text + (" " + rest if rest else ""))


BUTTONS = {}

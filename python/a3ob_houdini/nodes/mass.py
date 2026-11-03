# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB ui/tool_mass.py and utilities/masses.py, v2.5.1 (9f2b8bc): the four mass buttons become modes of
# one node; the maths is in ``mass_math``. A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original
# ArmaToolbox).
"""The Mass node (ux-tools.md section 5.7): sets the point attribute ``a3ob_mass`` (spec section 10).

The mode "Weighted" distribution of A3OB is not offered (ux-tools.md: not in the first version). The centre of mass
is a node message, because the spec has no attribute for it.
"""

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from a3ob_core.p3d.lod_types import LodType

from .. import mass_math
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms
from ._lodname import lod_name, lod_type

__all__ = ["SPEC", "cook", "BUTTONS"]

NAME = "Mass"
ATTRIB = "a3ob_mass"
HOUDINI_ATTRIB = "mass"
GEOMETRY_TYPES = (LodType.GEOMETRY, LodType.GEOMETRY_BUOY, LodType.GEOMETRY_PHYSX)

SPEC = NodeSpec(
    kind="mass",
    label="Mass",
    summary="Sets the mass of each point of a Geometry LOD (the #Mass# data of the P3D file).",
    inputs=(1, 1),
    input_labels=("LOD geometry",),
    input_help=("The Geometry LOD whose points get a mass.",),
    parms=(
        Parm("mode", "Mode", "menu", "total", "What to do with the mass.", items=(
            ("each", "Set on each"),
            ("total", "Uniform total"),
            ("density", "From density"),
            ("clear", "Clear"),
            ("copyfrom", "Copy from mass"),
            ("copyto", "Copy to mass"),
        )),
        Parm("value", "Value", "float", 1.0,
             "Set on each: kilograms per point. Uniform total: kilograms shared by all the points. From density: "
             "kilograms per cubic metre. The value cannot be below 0; there is no upper limit (the slider shows "
             "0 to 10, and you can type more).", minimum=0),
        Parm("group", "Group", "string", "", "The points to change. Empty means all points."),
        Parm("visualize", "Visualize", "toggle", False,
             "Write the colour Cd: blue is the least mass, red the most, black no mass. The export ignores Cd."),
    ),
)


def cook(node, report: Report) -> None:
    """Copy the input, change ``a3ob_mass`` as the mode says, and report the totals."""
    values = read_parms(node, SPEC)
    source = node.inputGeometry(0)
    if source is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect a LOD geometry"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    count = len(geo.points())
    selected = _selected_points(geo, values["group"])
    positions = _positions(geo)
    mode = values["mode"]
    if mode == "clear":
        _clear(geo, selected, count)
    elif mode == "copyfrom":
        _copy_from_mass(geo, selected)
    elif mode == "copyto":
        _copy_to_mass(geo, selected, report)
    else:
        if not selected:
            report.warning(format_problem(NAME, "no point is in the group", "Check the Group parameter"))
        masses = _current(geo, count)
        if mode == "each":
            for number in selected:
                masses[number] = values["value"]
        elif mode == "total":
            for number in selected:
                masses[number] = values["value"] / len(selected)
        elif selected:  # with an empty selection the warning above is all there is to say
            _from_density(geo, positions, selected, masses, values["value"], report)
        _write(geo, ATTRIB, masses)
    _report(geo, positions, report)
    if values["visualize"]:
        _visualize(geo, count)


def _selected_points(geo, group: str):
    """Point numbers of the group (all points when it is empty), as a sorted list."""
    if not group.strip():
        return list(range(len(geo.points())))
    try:
        points = geo.globPoints(group)
    except hou.OperationFailed:
        raise UserError(format_problem(NAME, "the group %r cannot be read" % group,
                                       "Use a point group name or point numbers")) from None
    return sorted(point.number() for point in points)


def _positions(geo):
    flat = geo.pointFloatAttribValues("P")
    return [(flat[3 * i], flat[3 * i + 1], flat[3 * i + 2]) for i in range(len(flat) // 3)]


def _current(geo, count: int):
    """The present ``a3ob_mass`` values, or zeros when the attribute does not exist yet."""
    if geo.findPointAttrib(ATTRIB) is None:
        return [0.0] * count
    return list(geo.pointFloatAttribValues(ATTRIB))


def _write(geo, name: str, values) -> None:
    if geo.findPointAttrib(name) is None:
        geo.addAttrib(hou.attribType.Point, name, 0.0)
    if values:
        geo.setPointFloatAttribValues(name, values)


def _from_density(geo, positions, selected, masses, density: float, report: Report) -> None:
    """Each closed component gets ``volume * density``, shared equally among its points (A3OB, whole mesh)."""
    polygons = [tuple(point.number() for point in prim.points()) for prim in geo.prims()
                if isinstance(prim, hou.Polygon)]
    chosen = set(selected)
    ignored = False
    for component in mass_math.closed_components(polygons):
        if not component.closed or not chosen.issuperset(component.points):
            ignored = True
            continue
        volume = abs(mass_math.component_volume(positions, polygons, component.polygons))
        share = volume * density / len(component.points)
        for number in component.points:
            masses[number] = share
    if ignored:
        report.warning(format_problem(NAME, "non-closed or flat components were ignored",
                                      "Density needs closed shapes. Close them, or use another mode"))


def _clear(geo, selected, count: int) -> None:
    """Without a group the attribute is removed (A3OB "Clear All Masses"); with a group the points get 0."""
    attrib = geo.findPointAttrib(ATTRIB)
    if attrib is None:
        return
    if len(selected) == count:
        attrib.destroy()
        return
    masses = _current(geo, count)
    for number in selected:
        masses[number] = 0.0
    _write(geo, ATTRIB, masses)


def _copy_from_mass(geo, selected) -> None:
    source = geo.findPointAttrib(HOUDINI_ATTRIB)
    if source is None or source.dataType() != hou.attribData.Float or source.size() != 1:
        raise UserError(format_problem(NAME, "the geometry has no point attribute mass",
                                       "Copy from mass reads the Houdini attribute mass. Create it first"))
    count = len(geo.points())
    masses = _current(geo, count)
    values = geo.pointFloatAttribValues(HOUDINI_ATTRIB)
    for number in selected:
        masses[number] = values[number]
    _write(geo, ATTRIB, masses)


def _copy_to_mass(geo, selected, report: Report) -> None:
    if geo.findPointAttrib(ATTRIB) is None:
        raise UserError(format_problem(NAME, "the geometry has no point attribute a3ob_mass",
                                       "Set the mass first with another mode"))
    count = len(geo.points())
    target = geo.findPointAttrib(HOUDINI_ATTRIB)
    if target is not None and (target.dataType() != hou.attribData.Float or target.size() != 1):
        raise UserError(format_problem(NAME, "the point attribute mass is not a single float",
                                       "Rename or delete it, then copy again"))
    current = list(geo.pointFloatAttribValues(HOUDINI_ATTRIB)) if target is not None else [0.0] * count
    source = geo.pointFloatAttribValues(ATTRIB)
    for number in selected:
        current[number] = source[number]
    _write(geo, HOUDINI_ATTRIB, current)
    report.info("Copied a3ob_mass to mass on %d points." % len(selected))


def _report(geo, positions, report: Report) -> None:
    """The summary message, the centre of mass and the LOD type warning."""
    if geo.findPointAttrib(ATTRIB) is None:
        report.info("Mass removed.")
        return
    masses = list(geo.pointFloatAttribValues(ATTRIB))
    result = mass_math.stats(masses)
    if result.count == 0:
        report.info("No point has mass.")
    else:
        centre = mass_math.centre_of_mass(positions, masses)
        text = "Total %.1f kg on %d points." % (result.total, result.count)
        if centre is not None:
            text += " Centre of mass (%.2f, %.2f, %.2f)." % centre
        report.info(text)
        report.info("Per point: minimum %.3g kg, average %.3g kg, maximum %.3g kg." % (
            result.minimum, result.average, result.maximum))
    _warn_lod_type(geo, report)


def _warn_lod_type(geo, report: Report) -> None:
    """Spec section 10: mass on a LOD that is not a geometry type is a validator warning."""
    number = lod_type(geo)
    if number is None or number in GEOMETRY_TYPES:
        return
    report.warning(format_problem(
        lod_name(geo), "a3ob_mass is on a LOD that is not a Geometry LOD, and the mass is written to the file anyway",
        "Use a Geometry, Geometry Buoyancy or Geometry PhysX LOD, or clear the mass"))


def _visualize(geo, count: int) -> None:
    """Write ``Cd`` from the final masses on every point."""
    masses = list(geo.pointFloatAttribValues(ATTRIB)) if geo.findPointAttrib(ATTRIB) else [0.0] * count
    colors = mass_math.ramp_colors(masses)
    if geo.findPointAttrib("Cd") is None:
        geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
    if colors:
        geo.setPointFloatAttribValues("Cd", [c for color in colors for c in color])


BUTTONS = {}

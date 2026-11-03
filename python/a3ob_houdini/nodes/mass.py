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
LEGACY_UNSET = -1.0  # the default of the hidden "value" parameter: "an old scene did not store a number here"
OWN_DEFAULT = 1.0

SPEC = NodeSpec(
    kind="mass",
    label="Mass",
    summary="Sets the mass of each point of a Geometry LOD (the #Mass# data of the P3D file).",
    inputs=(1, 1),
    input_labels=("LOD geometry",),
    input_help=("The Geometry LOD whose points get a mass.",),
    parms=(
        Parm("mode", "Mode", "menu", "total", "How the mass is set. Only the number for the chosen way is shown.", items=(
            ("each", "Set on each"),
            ("total", "Uniform total"),
            ("density", "From density"),
            ("clear", "Clear"),
            ("copyfrom", "Copy from mass"),
            ("copyto", "Copy to mass"),
        ), item_help=(
            ("each", "Gives every point the same mass, set by Mass per Point."),
            ("total", "Sets the mass of the whole object (Total Mass) and shares it equally among the points."),
            ("density", "Works the mass out from the volume of each closed shape and the Density."),
            ("clear", "Removes the mass. With a group, only those points go back to 0."),
            ("copyfrom", "Takes the mass from the point attribute called mass, which you made yourself."),
            ("copyto", "Copies the mass into a point attribute called mass, so you can see it or use it elsewhere."),
        )),
        Parm("mass_each", "Mass per Point (kg)", "float", 1.0,
             "The mass in kilograms that every point gets. It cannot be below 0. The slider shows 0 to 10, and you "
             "can type a larger number.", minimum=0, show_when=(("mode", ("each",)),)),
        Parm("mass_total", "Total Mass (kg)", "float", 1.0,
             "The mass in kilograms of the whole object. It is shared equally among the points. It cannot be below "
             "0. The slider shows 0 to 10, and you can type a larger number.", minimum=0,
             show_when=(("mode", ("total",)),)),
        Parm("density", "Density (kg/m³)", "float", 1.0,
             "The mass of one cubic metre of the material, in kilograms. Each closed shape gets its volume times "
             "this number. It cannot be below 0. The slider shows 0 to 10, and you can type a larger number.",
             minimum=0, show_when=(("mode", ("density",)),)),
        Parm("value", "Value (old scenes)", "float", LEGACY_UNSET,
             "Not shown. Scenes saved before the three mass parameters existed stored the number here. While it is "
             "0 or more and the parameter of the current mode is still 1, the node uses it, so old scenes keep "
             "their mass. A new node leaves it at -1.", hidden=True),
        Parm("group", "Group", "string", "", "The points to change, as a group name or point numbers (for example 0-3). Empty means all points."),
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
                masses[number] = _amount(values, "mass_each")
        elif mode == "total":
            for number in selected:
                masses[number] = _amount(values, "mass_total") / len(selected)
        elif selected:  # with an empty selection the warning above is all there is to say
            _from_density(geo, positions, selected, masses, _amount(values, "density"), report)
        _write(geo, ATTRIB, masses)
    _report(geo, positions, report)
    if values["visualize"]:
        _visualize(geo, count)


def _amount(values: dict, name: str) -> float:
    """The number of the current mode. Only that mode's own parameter is read, except for the old shared "value":
    a scene saved before the split stores it, and it counts while the mode's own parameter is still at its default."""
    own = values[name]
    legacy = values["value"]
    if legacy >= 0 and own == OWN_DEFAULT:
        return legacy
    return own


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

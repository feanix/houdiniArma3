# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Material Assign node (ux-tools.md section 5.6): texture and material (rvmat) on a set of polygons.

It copies its input and writes only the primitive strings ``a3ob_texture`` and ``a3ob_material`` (spec 5 and 12), on
polygons. Houdini's ``shop_materialpath`` and ``material`` are never touched. File paths get ``\\`` for ``/``
(D14 Q-6); procedural and custom texture text is copied as typed.
"""

from .. import geo_edit, material_text, names, settings
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS"]

NAME = "Material Assign"

SPEC = NodeSpec(
    kind="material_assign",
    label="Material Assign",
    summary="Sets the texture and the material (rvmat) of the polygons in a group, or of all polygons.",
    inputs=(1, 1),
    input_labels=("LOD geometry",),
    input_help=("The LOD whose polygons get the texture and material.",),
    parms=(
        Parm("group", "Group", "string", "",
             "Which polygons to set. Empty means all polygons. Normal Houdini group syntax; a selection with an "
             "encoded name is easiest to pick in the Selection browser."),
        Parm("grouptype", "Group Type", "menu", "prims",
             "Primitives: the polygons in the group. Points: the polygons whose points are all in the group.",
             items=(("prims", "Primitives"), ("points", "Points"))),
        Parm("settex", "Set Texture", "toggle", True, "Off: leave a3ob_texture as it is."),
        Parm("texsource", "Texture Source", "menu", "file", "Where the texture text comes from.",
             items=(("file", "File"), ("color", "Color"), ("custom", "Custom"))),
        Parm("texture", "Texture File", "string", "", "For File. Slashes are written as backslashes."),
        Parm("color", "Color", "float", (1.0, 1.0, 1.0, 1.0), "For Color. Red, green, blue and alpha, 0 to 1.",
             size=4),
        Parm("colortype", "Color Type", "menu", "CO", "For Color. The kind of procedural texture.",
             items=tuple((token, "%s - %s" % (token, text)) for token, text in material_text.COLOR_TYPES)),
        Parm("raw", "Raw Text", "string", "", "For Custom. Copied exactly as typed."),
        Parm("commonproc", "Common Procedural", "menu", "none",
             "Fill Raw Text from the common procedural textures (and switch Texture Source to Custom). "
             "Add your own in the common data file of the Project root and presets tool.",
             action="fillproc", menu_action="commonproc"),
        Parm("setmat", "Set Material", "toggle", True, "Off: leave a3ob_material as it is."),
        Parm("material", "Material File", "string", "",
             "The rvmat path. Empty clears the material. Slashes are written as backslashes."),
        Parm("commonmat", "Add Common", "menu", "none",
             "Fill Material File from the common materials (and switch Set Material on). "
             "Add your own in the common data file of the Project root and presets tool.",
             action="fillmat", menu_action="commonmat"),
    ),
)


def _check_text(text: str, field: str) -> str:
    problem = names.check_encodable(text)
    if problem:
        raise UserError(format_problem(NAME, "%s has a character that a P3D file cannot hold (%s)" % (field, problem),
                                       "Use only characters of the Windows 1252 code page"))
    return text


def _texture_text(values) -> str:
    source = values["texsource"]
    if source == "file":
        return material_text.normalise_path(_check_text(values["texture"], "The texture"))
    if source == "color":
        color = values["color"]
        if any(not 0.0 <= channel <= 1.0 for channel in color):
            raise UserError(format_problem(NAME, "the colour %s has a channel outside 0 to 1" % (tuple(color),),
                                           "Use values from 0 to 1"))
        return material_text.color_texture(color, values["colortype"])
    if source == "custom":
        return _check_text(values["raw"], "The raw texture text")
    raise UserError(format_problem(NAME, "%r is not a texture source" % source, "Pick File, Color or Custom"))


def _polygons(geo, group: str, group_type: str):
    """The polygons to set, in primitive order."""
    import hou
    group = group.strip()
    try:
        if not group:
            chosen = geo.prims()
        elif group_type == "points":
            members = {point.number() for point in geo.globPoints(group)}
            chosen = [prim for prim in geo.prims() if prim.numVertices() and
                      all(point.number() in members for point in prim.points())]
        else:
            chosen = geo.globPrims(group)
    except hou.OperationFailed:
        raise UserError(format_problem(NAME, "%r is not a valid group" % group,
                                       "Use a group name or numbers like 0 2-5")) from None
    return [prim for prim in chosen if isinstance(prim, hou.Polygon)]


def _set_strings(geo, name: str, numbers, text: str) -> None:
    import hou
    if geo.findPrimAttrib(name) is None:
        geo.addAttrib(hou.attribType.Prim, name, "")
    current = list(geo.primStringAttribValues(name))
    for number in numbers:
        current[number] = text
    geo.setPrimStringAttribValues(name, current)


def cook(node, report: Report) -> None:
    """Copy the input and set the texture and material strings."""
    values = read_parms(node, SPEC)
    geo = geo_edit.copy_input(node, NAME)
    texture = _texture_text(values) if values["settex"] else None
    material = (material_text.normalise_path(_check_text(values["material"], "The material"))
                if values["setmat"] else None)
    if texture is not None and values["texsource"] == "file" and texture and not material_text.has_known_extension(texture):
        report.warning(format_problem(NAME, "the texture %r does not end in .paa, .tga or .png" % texture,
                                      "Check the path"))
    if material and not material_text.has_known_extension(material):
        report.warning(format_problem(NAME, "the material %r does not end in .rvmat" % material, "Check the path"))
    polygons = _polygons(geo, values["group"], values["grouptype"])
    if not polygons:
        report.warning(format_problem(NAME, "no polygons match" + (" the group %r" % values["group"].strip()
                                                                    if values["group"].strip() else ""),
                                      "Nothing was set"))
        return
    numbers = [prim.number() for prim in polygons]
    if texture is not None:
        _set_strings(geo, "a3ob_texture", numbers, texture)
    if material is not None:
        _set_strings(geo, "a3ob_material", numbers, material)
    report.info("Set on %d polygons." % len(polygons))


def _common_items(section: str):
    """Menu items from the settings module: the built-in lists plus the common data file. Never raises."""
    data, _ = settings.common_data(settings.load()[0])
    return [("none", "(choose)")] + [(key, key) for key in sorted(data[section], key=str.lower)]


def _fill(kwargs, section: str, target: str, switch: str, switch_to) -> None:
    """A Common menu was changed: copy the chosen entry into ``target``, then show "(choose)" again."""
    node = kwargs["node"]
    token = kwargs["parm"].eval()
    if token != "none":
        data, _ = settings.common_data(settings.load()[0])
        if token in data[section]:
            node.parm(target).set(data[section][token])
            node.parm(switch).set(switch_to)
    kwargs["parm"].set("none")


def fill_proc(kwargs) -> None:
    _fill(kwargs, "procedurals", "raw", "texsource", "custom")


def fill_mat(kwargs) -> None:
    _fill(kwargs, "materials", "material", "setmat", 1)


BUTTONS = {"fillproc": fill_proc, "fillmat": fill_mat}
MENUS = {"commonproc": lambda kwargs: _common_items("procedurals"), "commonmat": lambda kwargs: _common_items("materials")}

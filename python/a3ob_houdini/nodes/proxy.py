# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB ui/tool_proxies.py (create and align proxy), props/object.py and utilities/data.py ("common
# proxies"), v2.5.1 (9f2b8bc): a proxy is a packed prim here (spec section 13.3), not a Blender object. A3OB: MrClock
# (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""The Proxy node (ux-tools.md section 5.5): adds packed proxy prims to a LOD geometry.

A new proxy is the canonical triangle of spec 13.5 step 2 (corners C, B, A at ``(0,0,0)``, ``(0,2,0)``, ``(0,0,-1)``)
embedded in a ``PackedGeometry`` prim. Its transform places it. It looks exactly like a proxy the importer makes from a
file, except that it has no snap-guard arrays (it has no source values).
"""

import re

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from .. import names
from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms
from ._lodname import lod_name

__all__ = ["SPEC", "cook", "BUTTONS", "normalise_path"]

NAME = "Proxy"
MAX_INDEX = 999

#: A3OB "Paste Common Proxy" (``utilities/data.py``), without the ``P:`` drive and the ``.p3d`` ending.
COMMON = (
    ("weapon_optic", "Weapon: optic", r"\a3\data_f\proxies\weapon_slots\top"),
    ("weapon_pointer", "Weapon: pointer", r"\a3\data_f\proxies\weapon_slots\side"),
    ("weapon_suppressor", "Weapon: suppressor", r"\a3\data_f\proxies\weapon_slots\muzzle"),
    ("weapon_magazine", "Weapon: magazine", r"\a3\data_f\proxies\weapon_slots\magazineslot"),
    ("weapon_bipod", "Weapon: bipod", r"\a3\data_f_mark\proxies\weapon_slots\underbarrel"),
    ("driver_offroad", "Driver: offroad", r"\a3\data_f\proxies\driver_offroad\driver"),
    ("passenger_offroad", "Passenger: offroad", r"\a3\data_f\proxies\passenger_low01\cargo01"),
    ("gunner_hunter", "Gunner: hunter", r"\a3\data_f\proxies\gunner_hunter\gunner"),
    ("commander_hunter", "Commander: hunter", r"\a3\data_f\proxies\gunner_hunter\commander"),
    ("light_volume_car", "Light volume: car", r"\a3\data_f\volumelightcar"),
)

SPEC = NodeSpec(
    kind="proxy",
    label="Proxy",
    summary="Adds proxies (references to other .p3d files, such as weapon slots or seats) as packed primitives.",
    inputs=(1, 2),
    input_labels=("LOD geometry", "Points (one proxy per point)"),
    input_help=("The LOD that gets the proxy.",
                "Optional. Each point of this geometry becomes one proxy, placed and turned by that point."),
    parms=(
        Parm("path", "Path", "file", "",
             "The proxy model: pick a .p3d file, or type its game path, for example "
             "\\a3\\data_f\\proxies\\weapon_slots\\top. A picked file such as "
             "P:\\a3\\data_f\\proxies\\weapon_slots\\top.p3d gives the same name as typing it. Slashes become "
             "backslashes and the .p3d ending is removed. When you export with Relative Paths on, the path is mapped "
             "to the in-game path (see the Relative Paths tooltip of P3D Export).",
             file_pattern="*.p3d"),
        Parm("common", "Common Proxies", "menu", "none",
             "Fills Path with the game path of a frequently used proxy, such as a weapon slot or a seat.",
             action="fillcommon",
             items=(("none", "(choose)"),) + tuple((token, label) for token, label, _ in COMMON)),
        Parm("index", "Index", "int", 1,
             "The proxy number, 0 to 999. The game needs a different number for each proxy with the same model "
             "in one LOD.", minimum=0, maximum=MAX_INDEX),
        Parm("autoindex", "Number From Points", "toggle", True,
             "With a second input: the first proxy gets Index, the next one Index + 1, and so on. "
             "Off: all get Index."),
        Parm("t", "Translate", "float", (0.0, 0.0, 0.0),
             "Where the proxy goes, in metres. Used when there is no second input.", size=3),
        Parm("r", "Rotate", "float", (0.0, 0.0, 0.0),
             "How the proxy is turned, in degrees around X, Y and Z. A proxy has no scale.", size=3),
        Parm("useorient", "Orient From Points", "toggle", True,
             "With a second input: turn each proxy by the orient attribute of its point, if the point has one."),
    ),
)


def normalise_path(text: str) -> str:
    """Slashes become backslashes and a trailing ``.p3d`` goes (D14 Q-6, spec section 5 Paths)."""
    path = text.strip().replace("/", "\\")
    return re.sub(r"\.p3d$", "", path, flags=re.IGNORECASE)


def cook(node, report: Report) -> None:
    """Copy the input and add one packed proxy for each placement."""
    values = read_parms(node, SPEC)
    path = _checked_path(values["path"])
    source = node.inputGeometry(0)
    if source is None:
        raise UserError(format_problem(NAME, "there is no input", "Connect a LOD geometry"))
    placements = _placements(node, values, report)
    first = int(values["index"])
    numbered = len(placements) > 1 and bool(values["autoindex"]) and node.inputGeometry(1) is not None
    indices = [first + k if numbered else first for k in range(len(placements))]
    if indices and max(indices) > MAX_INDEX:
        raise UserError(format_problem(
            NAME, "the last proxy would get index %d, and the highest index is %d" % (max(indices), MAX_INDEX),
            "Lower Index or use fewer points"))
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    taken = _used(geo)
    _add_attribs(geo)
    for matrix, index in zip(placements, indices):
        _add_proxy(geo, path, index, matrix)
    _warn_duplicates(geo, taken, path, indices, report)
    if indices:
        report.info(_summary(path, indices))


def _checked_path(text: str) -> str:
    path = normalise_path(text)
    if not path:
        raise UserError(format_problem(NAME, "the Path is empty", "Type the model path of the proxy, "
                                       "for example \\a3\\data_f\\proxies\\weapon_slots\\top"))
    reason = names.check_encodable(path)
    if reason:
        raise UserError(format_problem(NAME, "the Path cannot be written to a P3D file (%s)" % reason,
                                       "Use only characters of the Windows-1252 code page"))
    return path


def _placements(node, values, report: Report):
    """One ``hou.Matrix4`` per proxy: from the second input's points, or from Translate and Rotate."""
    points_geo = node.inputGeometry(1)
    if points_geo is None:
        rotation = hou.hmath.buildRotate(tuple(values["r"]), "xyz")
        return [rotation * hou.hmath.buildTranslate(tuple(values["t"]))]
    points = points_geo.points()
    if not points:
        report.warning(format_problem(NAME, "the second input has no points", "No proxy was added"))
        return []
    orient = points_geo.findPointAttrib("orient") if values["useorient"] else None
    if orient is not None and (orient.dataType() != hou.attribData.Float or orient.size() != 4):
        orient = None
    matrices = []
    for point in points:
        matrix = hou.Matrix4(1)
        if orient is not None:
            matrix = hou.Matrix4(_rotation(point.attribValue("orient")))
        matrix = matrix * hou.hmath.buildTranslate(tuple(point.position()))
        matrices.append(matrix)
    return matrices


def _rotation(orient):
    """The rotation matrix of an ``orient`` quaternion (Houdini's row-vector convention), made orthonormal."""
    length = sum(component * component for component in orient) ** 0.5
    if length == 0:
        return hou.Matrix3(1)
    return hou.Quaternion(*(component / length for component in orient)).extractRotationMatrix3()


def _add_attribs(geo) -> None:
    """The outer prim attributes of spec 13.3 (the snap-guard arrays are not added: no source values)."""
    if geo.findPrimAttrib("a3ob_proxy_path") is None:
        geo.addAttrib(hou.attribType.Prim, "a3ob_proxy_path", "")
    if geo.findPrimAttrib("a3ob_proxy_index") is None:
        geo.addAttrib(hou.attribType.Prim, "a3ob_proxy_index", 0)


def _add_proxy(geo, path: str, index: int, matrix) -> None:
    prim = geo.createPacked("PackedGeometry")
    prim.setEmbeddedGeometry(_canonical_triangle(path, index).freeze())
    prim.setTransform(matrix)
    prim.setAttribValue("a3ob_proxy_path", path)
    prim.setAttribValue("a3ob_proxy_index", index)


def _canonical_triangle(path: str, index: int) -> "hou.Geometry":
    """Spec 13.5 step 2 in proxy-local space, with the attribute scheme the importer gives an embedded proxy."""
    from ..p3d_import import hou_util
    inner = hou.Geometry()
    inner.createPoints([(0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, -1.0)])
    inner.createPolygons([(0, 2, 1)])  # the file corners C, B, A through the corner map of spec 4.3
    inner = hou_util.with_float64_vertex_attribs(inner, ["uv"])
    inner.addAttrib(hou.attribType.Point, "a3ob_vertex_flags", 0)
    inner.addAttrib(hou.attribType.Vertex, "N", (0.0, 0.0, 0.0), transform_as_normal=True)
    inner.setVertexFloatAttribValues("N", [-1.0, 0.0, 0.0] * 3)
    inner.addAttrib(hou.attribType.Prim, "a3ob_face_flags", 0)
    inner.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
    inner.addAttrib(hou.attribType.Prim, "a3ob_material", "")
    inner.addAttrib(hou.attribType.Prim, "a3ob_proxy_path", "")
    inner.addAttrib(hou.attribType.Prim, "a3ob_proxy_index", 0)
    prim = inner.prims()[0]
    prim.setAttribValue("a3ob_proxy_path", path)
    prim.setAttribValue("a3ob_proxy_index", index)
    return inner


def _used(geo):
    """The ``(path, index)`` pairs already in the geometry (packed proxies and inline proxy polygons), lower case."""
    path_attrib = geo.findPrimAttrib("a3ob_proxy_path")
    if path_attrib is None or geo.findPrimAttrib("a3ob_proxy_index") is None:
        return set()
    found = set()
    for prim in geo.prims():
        text = prim.attribValue("a3ob_proxy_path")
        if text:
            found.add((text.lower(), prim.attribValue("a3ob_proxy_index")))
    return found


def _warn_duplicates(geo, taken, path: str, indices, report: Report) -> None:
    """The game needs a unique path and index pair in one LOD."""
    clashes = []
    seen = set(taken)
    for index in indices:
        key = (path.lower(), index)
        if key in seen and index not in clashes:
            clashes.append(index)
        seen.add(key)
    if clashes:
        report.warning(format_problem(
            lod_name(geo) or NAME, "the proxy %s is used more than once (index %s)" % (
                path, ", ".join(str(index) for index in clashes)),
            "The game needs a different path or index for each proxy. Change Index"))


def _summary(path: str, indices) -> str:
    if len(indices) == 1:
        return "1 proxy added: %s %d." % (path, indices[0])
    if indices[0] == indices[-1]:
        return "%d proxies added: %s %d." % (len(indices), path, indices[0])
    return "%d proxies added: %s %d to %d." % (len(indices), path, indices[0], indices[-1])


def fill_common(kwargs) -> None:
    """The Common Proxies menu: copy the chosen path into Path, then show "(choose)" again."""
    node = kwargs["node"]
    token = kwargs["parm"].eval()
    for key, _, path in COMMON:
        if key == token:
            node.parm("path").set(path)
            break
    kwargs["parm"].set("none")


BUTTONS = {"fillcommon": fill_common}

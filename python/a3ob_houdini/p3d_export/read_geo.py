# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Read a LOD geometry into plain Python (``data``). Needs ``hou``.

``read_details`` reads the detail attributes (spec 3.3), ``read_mesh`` the points, polygons, groups and attributes
(spec 5 to 9), and ``read_proxies`` the packed proxy prims (spec 13.3). Houdini-side errors that stop the export
(open polygons, other prim types) are reported here; every decision after that is made on the plain data.
"""

from typing import Dict, List, Optional, Sequence, Tuple

import hou

from .. import messages, names
from .data import Details, GroupData, MeshData, Poly, ProxyData
from .mesh_build import FACE_UV, uv_set_id

__all__ = ["read_details", "read_mesh", "read_proxies", "SHARP_GROUP"]

SHARP_GROUP = "a3ob_sharp"


# -- Detail attributes ---------------------------------------------------------------------------------------------

def _detail(geo, name: str, default):
    if geo.findGlobalAttrib(name) is None:
        return default
    return geo.attribValue(name)


def _detail_list(geo, name: str) -> list:
    value = _detail(geo, name, ())
    return list(value) if isinstance(value, (tuple, list)) else []


def read_details(geo) -> Details:
    """The detail attributes of a LOD geometry (spec 3.3)."""
    details = Details()
    details.lod_type = int(_detail(geo, "a3ob_lod_type", 0))
    details.resolution = float(_detail(geo, "a3ob_lod_resolution", 1.0))
    if geo.findGlobalAttrib("a3ob_lod_signature") is not None:
        details.signature = int(geo.attribValue("a3ob_lod_signature"))
    details.flags = int(_detail(geo, "a3ob_lod_flags", 0))
    details.eof_active = int(_detail(geo, "a3ob_eof_active", 1))
    details.copy_types = [int(v) for v in _detail_list(geo, "a3ob_lod_copy_types")]
    details.copy_resolutions = [float(v) for v in _detail_list(geo, "a3ob_lod_copy_resolutions")]
    details.prop_names = _detail_list(geo, "a3ob_prop_names")
    details.prop_values = _detail_list(geo, "a3ob_prop_values")
    details.prop_raw = _detail_list(geo, "a3ob_prop_raw")
    details.tagg_order = _detail_list(geo, "a3ob_tagg_order")
    details.tagg_active = [int(v) for v in _detail_list(geo, "a3ob_tagg_active")]
    details.unknown_names = _detail_list(geo, "a3ob_tagg_unknown_names")
    details.unknown_active = [int(v) for v in _detail_list(geo, "a3ob_tagg_unknown_active")]
    details.unknown_data = _detail_list(geo, "a3ob_tagg_unknown_data")
    counts = _detail_list(geo, "a3ob_source_counts")
    details.source_counts = tuple(int(v) for v in counts) if len(counts) == 3 else None
    details.sharp_extra = [int(v) for v in _detail_list(geo, "a3ob_sharp_extra")]
    layout = _detail(geo, "a3ob_layout", "")
    details.layout = layout if isinstance(layout, str) else ""
    return details


# -- Mesh --------------------------------------------------------------------------------------------------------

def _chunks(flat: Sequence[float], size: int) -> List[Tuple[float, ...]]:
    return [tuple(flat[i:i + size]) for i in range(0, len(flat) - size + 1, size)]


def _prim_values(geo, name: str, default, kind: str) -> Optional[list]:
    """A primitive attribute as one value per prim, or None when the attribute does not exist."""
    if geo.findPrimAttrib(name) is None:
        return None
    if kind == "int":
        return list(geo.primIntAttribValues(name))
    if kind == "float":
        return list(geo.primFloatAttribValues(name))
    return list(geo.primStringAttribValues(name))


def read_mesh(geo, label: str, report: messages.Report) -> MeshData:
    """Points, polygons, groups and attributes of ``geo`` (spec 5 to 9). Errors go to ``report``."""
    mesh = MeshData()
    mesh.points = [tuple(p) for p in _chunks(geo.pointFloatAttribValues("P"), 3)]
    if geo.findPointAttrib("a3ob_vertex_flags") is not None:
        mesh.point_flags = list(geo.pointIntAttribValues("a3ob_vertex_flags"))
    if geo.findPointAttrib("a3ob_mass") is not None:
        mesh.masses = list(geo.pointFloatAttribValues("a3ob_mass"))
    prim_to_poly = _read_polys(geo, mesh, label, report)
    _read_pack_points(geo, mesh, label, report)
    _read_sharp(geo, mesh)
    _read_groups(geo, mesh, prim_to_poly, label, report)
    return mesh


def _vertex_attribs(geo, mesh: MeshData, label: str, report: messages.Report) -> Dict[str, list]:
    """The UV attributes (flat float lists) and ``N``. UV sets go to ``mesh.uv_names`` in ascending id."""
    found: Dict[str, list] = {}
    sets: List[Tuple[int, str]] = []
    unused: List[str] = []
    for attrib in geo.vertexAttribs():
        name = attrib.name()
        if attrib.isArrayType() or attrib.dataType() != hou.attribData.Float or attrib.size() < 2:
            continue
        if name == FACE_UV:
            found[name] = (attrib.size(), list(geo.vertexFloatAttribValues(name)))
        elif uv_set_id(name) is not None:
            sets.append((uv_set_id(name), name))
            found[name] = (attrib.size(), list(geo.vertexFloatAttribValues(name)))
        elif name.startswith("uv") and name[2:].isdigit():
            unused.append(name)
    if unused:
        report.warning(messages.format_problem(
            label, "the vertex attributes %s are not UV sets and are not written" % ", ".join(unused),
            "UV set k is named uv for k = 0 and uv<k+1> for k >= 1 (uv2, uv3, ...)"))
    mesh.uv_names = [name for _, name in sorted(sets)]
    if geo.findVertexAttrib("N") is not None and geo.findVertexAttrib("N").size() == 3:
        found["N"] = (3, list(geo.vertexFloatAttribValues("N")))
    return found


def _read_polys(geo, mesh: MeshData, label: str, report: messages.Report) -> Dict[int, int]:
    """Spec 5: closed polygons become ``Poly``; other prims are errors (packed prims are read elsewhere)."""
    vertex_data = _vertex_attribs(geo, mesh, label, report)
    point_normals = list(_chunks(geo.pointFloatAttribValues("N"), 3)) if (
        geo.findPointAttrib("N") is not None and geo.findPointAttrib("N").size() == 3) else None
    prim_normals = list(_chunks(geo.primFloatAttribValues("N"), 3)) if (
        geo.findPrimAttrib("N") is not None and geo.findPrimAttrib("N").size() == 3) else None
    flags = _prim_values(geo, "a3ob_face_flags", 0, "int")
    textures = _prim_values(geo, "a3ob_texture", "", "string")
    materials = _prim_values(geo, "a3ob_material", "", "string")
    paths = _prim_values(geo, "a3ob_proxy_path", "", "string")
    indices = _prim_values(geo, "a3ob_proxy_index", 0, "int")
    uids = _prim_values(geo, "a3ob_lod_uid", 0, "int")
    prim_to_poly: Dict[int, int] = {}
    open_polys: List[int] = []
    other: List[int] = []
    for prim in geo.prims():
        number = prim.number()
        if isinstance(prim, hou.PackedPrim):
            continue
        if not isinstance(prim, hou.Polygon):
            other.append(number)
            continue
        if not prim.isClosed():
            open_polys.append(number)
            continue
        vertices = prim.vertices()
        points = tuple(v.point().number() for v in vertices)
        linear = [v.linearNumber() for v in vertices]
        poly = Poly(number, points)
        if "N" in vertex_data:
            flat = vertex_data["N"][1]
            poly.normals = [tuple(flat[3 * k:3 * k + 3]) for k in linear]
        elif point_normals is not None:
            poly.normals = [point_normals[p] for p in points]
        elif prim_normals is not None:
            poly.normals = [prim_normals[number]] * len(points)
        for name, (size, flat) in vertex_data.items():
            if name != "N":
                poly.uvs[name] = [(flat[size * k], flat[size * k + 1]) for k in linear]
        if flags is not None:
            poly.flags = flags[number]
        if textures is not None:
            poly.texture = textures[number]
        if materials is not None:
            poly.material = materials[number]
        if paths is not None and paths[number]:
            poly.proxy = (paths[number], indices[number] if indices is not None else 0)
        if uids is not None:
            poly.uid = uids[number]
        prim_to_poly[number] = len(mesh.polys)
        mesh.polys.append(poly)
    if open_polys:
        report.error(messages.format_problem(
            label, "%d open polygons (%s)" % (len(open_polys), messages.list_elements("prims", open_polys)),
            "P3D faces are closed. Close them or delete them"))
    if other:
        mesh.has_other_prims = True
        report.error(messages.format_problem(
            label, "%d primitives are not polygons (%s)" % (len(other), messages.list_elements("prims", other)),
            "Delete them, or convert them to polygons"))
    elif open_polys:
        mesh.has_other_prims = True
    return prim_to_poly


def _read_pack_points(geo, mesh: MeshData, label: str, report: messages.Report) -> None:
    """Spec 5: pack points are used only by packed prims. A point shared with a polygon is exported (and warned)."""
    used = {p for poly in mesh.polys for p in poly.points}
    packed: Dict[int, int] = {}
    for prim in geo.prims():
        if isinstance(prim, hou.PackedPrim):
            for vertex in prim.vertices():
                packed.setdefault(vertex.point().number(), prim.number())
    mesh.pack_points = {p for p in packed if p not in used}
    shared = [(p, prim) for p, prim in packed.items() if p in used]
    for point, prim in shared[:5]:
        report.warning(messages.format_problem(
            label, "point %d is shared by proxy prim %d and polygons" % (point, prim),
            "It is written as an ordinary mesh point. Fuse can do this"))
    if len(shared) > 5:
        report.warning(messages.format_problem(label, "%d more points are shared by proxy prims and polygons" % (len(shared) - 5)))


def _read_sharp(geo, mesh: MeshData) -> None:
    group = geo.findEdgeGroup(SHARP_GROUP)
    if group is None:
        return
    pairs = []
    for edge in group.edges():
        a, b = edge.points()
        pairs.append((a.number(), b.number()))
    mesh.sharp = pairs


def _read_groups(geo, mesh: MeshData, prim_to_poly: Dict[int, int], label: str, report: messages.Report) -> None:
    """Spec 7.2: every point group is read with its weights and its partner prim group. Which ones are selections is
    decided later (the selection mask needs the options)."""
    point_names = set()
    for group in geo.pointGroups():
        encoded = group.name()
        point_names.add(encoded)
        data = GroupData(encoded, names.decode_group_name(encoded), [p.number() for p in group.points()])
        if geo.findPointAttrib(names.point_weight_attrib(encoded)) is not None:
            data.weights = list(geo.pointFloatAttribValues(names.point_weight_attrib(encoded)))
        prim_group = geo.findPrimGroup(encoded)
        if prim_group is not None:
            data.faces = sorted(prim_to_poly[p.number()] for p in prim_group.prims() if p.number() in prim_to_poly)
            attrib = names.prim_weight_attrib(encoded)
            if geo.findPrimAttrib(attrib) is not None:
                values = geo.primFloatAttribValues(attrib)
                data.face_weights = [values[poly.number] for poly in mesh.polys]
        mesh.groups.append(data)
    prim_names = {group.name() for group in geo.primGroups()}
    stray = [attrib.name() for attrib in geo.pointAttribs()
             if attrib.name().startswith(names.POINT_WEIGHT_PREFIX)
             and attrib.name()[len(names.POINT_WEIGHT_PREFIX):] not in point_names]
    stray += [attrib.name() for attrib in geo.primAttribs()
              if attrib.name().startswith(names.PRIM_WEIGHT_PREFIX)
              and attrib.name()[len(names.PRIM_WEIGHT_PREFIX):] not in prim_names]
    if stray:
        report.warning(messages.format_problem(
            label, "the weight attributes %s have no group and are ignored" % ", ".join(stray[:5]),
            "Create the group of the same name, or delete the attributes"))


# -- Packed proxies ------------------------------------------------------------------------------------------------

def read_proxies(geo, label: str, report: messages.Report) -> List[ProxyData]:
    """The packed prims of a LOD geometry, as proxies (spec 13.3)."""
    result: List[ProxyData] = []
    has_path = geo.findPrimAttrib("a3ob_proxy_path") is not None
    has_index = geo.findPrimAttrib("a3ob_proxy_index") is not None
    has_src_p = geo.findPrimAttrib("a3ob_proxy_src_P") is not None
    has_src_n = geo.findPrimAttrib("a3ob_proxy_src_N") is not None
    for prim in geo.prims():
        if not isinstance(prim, hou.PackedPrim):
            continue
        if not isinstance(prim, hou.PackedGeometry):
            report.error(messages.format_problem(
                label, "packed prim %d is a %s, which cannot be a proxy" % (prim.number(), type(prim).__name__),
                "Unpack it, or delete it"))
            continue
        embedded = prim.getEmbeddedGeometry()
        inner_path, inner_index = _embedded_reference(embedded)
        mesh = read_mesh(embedded, label, messages.Report())
        result.append(ProxyData(
            prim.number(),
            prim.attribValue("a3ob_proxy_path") if has_path else "",
            int(prim.attribValue("a3ob_proxy_index")) if has_index else 0,
            inner_path, inner_index, tuple(prim.fullTransform().asTuple()), mesh,
            tuple(prim.attribValue("a3ob_proxy_src_P")) if has_src_p else None,
            tuple(prim.attribValue("a3ob_proxy_src_N")) if has_src_n else None))
    return result


def _embedded_reference(embedded) -> Tuple[str, int]:
    """The proxy path and index on the embedded triangle (spec 13.3), or an empty path."""
    prims = embedded.prims()
    if not prims or embedded.findPrimAttrib("a3ob_proxy_path") is None:
        return "", 0
    index = int(prims[0].attribValue("a3ob_proxy_index")) if embedded.findPrimAttrib("a3ob_proxy_index") else 0
    return prims[0].attribValue("a3ob_proxy_path"), index

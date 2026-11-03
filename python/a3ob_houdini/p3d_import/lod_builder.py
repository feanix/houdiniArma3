# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/import_p3d.py, v2.5.1 (9f2b8bc): the mesh and selection import is rewritten for Houdini
# geometry and follows the Houdini data model (docs/design/houdini-data-model.md) where that changes A3OB (axis map,
# raw flags, float64 UVs, preserved weights, blobs). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden
# (original ArmaToolbox).
"""Build one LOD geometry from an ``a3ob_core`` LOD, following the plan (spec section 16, steps 2 to 9).

One small function per step. ``build_lod_geometry`` is the only entry point; it returns an ordinary unpacked geometry
with the LOD's detail attributes. Proxy triangles are built the same way (``_build_subset``) in their own local space
and embedded into packed prims at the end.

Needs ``hou``.
"""

import base64
from typing import Dict, List, Optional, Sequence

import hou

from a3ob_core import binary, p3d

from .. import names
from . import hou_util, proxies
from .plan import LodPlan, ProxyPlan, houdini_corner_order, uv_attrib_name

__all__ = ["ImportOptions", "build_lod_geometry"]

SHARP_GROUP = "a3ob_sharp"
DEFAULT_VERTEX_FLAGS = 0x02000000


class ImportOptions:
    """Switches of the importer. ``facegroups`` is spec 7.2 (OQ-6): always make prim groups for selections."""

    def __init__(self, facegroups: bool = False):
        self.facegroups = facegroups


class Subset:
    """The points and faces (file indices) that go into one geometry. Point numbers follow ``points`` order."""

    def __init__(self, points: Sequence[int], faces: Sequence[int]):
        self.points = list(points)
        self.faces = list(faces)
        self.local: Dict[int, int] = {p: i for i, p in enumerate(self.points)}


def build_lod_geometry(plan: LodPlan, uid: int, options: ImportOptions) -> "hou.Geometry":
    """The LOD geometry for a plan: mesh, attributes, groups, details and (last) the packed proxies."""
    subset = Subset(plan.mesh_points, plan.mesh_faces)
    geo = _build_subset(plan, subset, None, uid, None, options)
    _write_sharp_edges(geo, plan, subset)
    _write_details(geo, plan)
    _write_packed_proxies(geo, plan, options)
    return geo


# -- One subset: the mesh, or one proxy triangle ---------------------------------------------------------------------

def _build_subset(plan: LodPlan, subset: Subset, frame, uid: Optional[int], own_proxy: Optional[ProxyPlan],
                  options: ImportOptions) -> "hou.Geometry":
    corners = _houdini_corners(plan, subset)
    geo = _make_topology(plan, subset, frame)
    geo = hou_util.with_float64_vertex_attribs(geo, _uv_names(plan))
    _write_point_attribs(geo, plan, subset)
    _write_normals(geo, plan, corners, frame)
    _write_uvs(geo, plan, corners)
    _write_face_attribs(geo, plan, subset, uid, own_proxy)
    _write_selections(geo, plan, subset, options, skip_empty=own_proxy is not None)
    return geo


def _houdini_corners(plan: LodPlan, subset: Subset) -> List[int]:
    """The file corner index behind every Houdini vertex of the subset, in Houdini vertex order (spec 4.3)."""
    sides_of = plan.lod.face_sides
    corners: List[int] = []
    for face in subset.faces:
        start = plan.corner_start[face]
        corners.extend(start + k for k in houdini_corner_order(sides_of[face]))
    return corners


def _make_topology(plan: LodPlan, subset: Subset, frame) -> "hou.Geometry":
    """Steps 2 and 3: points (axis map applied) and polygons (corner map applied)."""
    lod = plan.lod
    flat = lod.points
    positions = []
    for index in subset.points:
        base = 3 * index
        position = (flat[base], flat[base + 1], -flat[base + 2])
        positions.append(proxies.to_local_point(frame, position) if frame else position)
    polygons = []
    for face in subset.faces:
        sides = lod.face_sides[face]
        file_points = lod.face_vertices[4 * face:4 * face + sides]
        polygons.append(tuple(subset.local[file_points[k]] for k in houdini_corner_order(sides)))
    geo = hou.Geometry()
    if positions:
        geo.createPoints(positions)
    if all(len(polygon) >= 3 for polygon in polygons):
        if polygons:
            geo.createPolygons(polygons)
    else:
        _create_short_polygons(geo, polygons)
    return geo


def _create_short_polygons(geo, polygons) -> None:
    """Spec 5: a face with 0 to 2 sides still becomes a closed polygon. ``createPolygons`` refuses fewer than 3
    points, so every polygon is made one by one (the order of prims and vertices stays the same)."""
    points = geo.points()
    for polygon in polygons:
        prim = geo.createPolygon()
        for number in polygon:
            prim.addVertex(points[number])


def _uv_names(plan: LodPlan) -> List[str]:
    names_ = [uv_attrib_name(set_id) for set_id in sorted(plan.uv_sets)]
    if plan.needs_face_uv:
        names_.append("a3ob_face_uv")
    return names_


def _write_point_attribs(geo, plan: LodPlan, subset: Subset) -> None:
    """Step 4 (points): raw vertex flags, and mass when the LOD has it."""
    lod = plan.lod
    geo.addAttrib(hou.attribType.Point, "a3ob_vertex_flags", names.to_int32(DEFAULT_VERTEX_FLAGS))
    geo.setPointIntAttribValues("a3ob_vertex_flags", [names.to_int32(lod.point_flags[i]) for i in subset.points])
    if plan.mass is not None:
        geo.addAttrib(hou.attribType.Point, "a3ob_mass", 0.0)
        geo.setPointFloatAttribValues("a3ob_mass", [plan.mass.masses[i] for i in subset.points])


def _write_normals(geo, plan: LodPlan, corners: List[int], frame) -> None:
    """Step 5: vertex ``N = (-nx, -ny, nz)``, not renormalised (spec 4.3)."""
    lod = plan.lod
    normal_of_corner = lod.face_corner_normal_indices()
    table = lod.normals
    values: List[float] = []
    for corner in corners:
        base = 3 * normal_of_corner[corner]
        normal = (-table[base], -table[base + 1], table[base + 2])
        values.extend(proxies.to_local_normal(frame, normal) if frame else normal)
    geo.addAttrib(hou.attribType.Vertex, "N", (0.0, 0.0, 0.0), transform_as_normal=True)
    geo.setVertexFloatAttribValues("N", values)


def _write_uvs(geo, plan: LodPlan, corners: List[int]) -> None:
    """Step 5: float64 ``uv`` sets with ``v' = 1 - v``, and the face-record UVs if they carry extra data (spec 8)."""
    for set_id in sorted(plan.uv_sets):
        _write_uv_set(geo, uv_attrib_name(set_id), plan.uv_sets[set_id].uvs, corners)
    if plan.needs_face_uv:
        _write_uv_set(geo, "a3ob_face_uv", plan.lod.corner_uvs(), corners)


def _write_uv_set(geo, attrib: str, uvs, corners: List[int]) -> None:
    values: List[float] = []
    for corner in corners:
        values.extend((uvs[2 * corner], 1.0 - uvs[2 * corner + 1], 0.0))
    geo.setVertexFloatAttribValues(attrib, values)


def _write_face_attribs(geo, plan: LodPlan, subset: Subset, uid: Optional[int], own_proxy: Optional[ProxyPlan]) -> None:
    """Step 4 (faces): raw flags, texture and material strings, the LOD uid and inline proxy references."""
    lod = plan.lod
    faces = subset.faces
    geo.addAttrib(hou.attribType.Prim, "a3ob_face_flags", 0)
    geo.setPrimIntAttribValues("a3ob_face_flags", [names.to_int32(lod.face_flags[f]) for f in faces])
    geo.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
    geo.setPrimStringAttribValues("a3ob_texture", [lod.face_textures[f] for f in faces])
    geo.addAttrib(hou.attribType.Prim, "a3ob_material", "")
    geo.setPrimStringAttribValues("a3ob_material", [lod.face_materials[f] for f in faces])
    if uid is not None:
        geo.addAttrib(hou.attribType.Prim, "a3ob_lod_uid", 0)
        geo.setPrimIntAttribValues("a3ob_lod_uid", [uid] * len(faces))
    _write_proxy_references(geo, plan, subset, own_proxy)


def _write_proxy_references(geo, plan: LodPlan, subset: Subset, own_proxy: Optional[ProxyPlan]) -> None:
    """Spec 13.3 and 13.6: ``a3ob_proxy_path`` / ``a3ob_proxy_index`` on inline proxy faces and on proxy triangles."""
    paths = [""] * len(subset.faces)
    indices = [0] * len(subset.faces)
    used = False
    if own_proxy is not None:
        paths[0], indices[0], used = own_proxy.path, own_proxy.index, True
    else:
        for group in plan.groups:
            if group.inline_proxy is None:
                continue
            used = True
            for slot, face in enumerate(subset.faces):
                if group.tagg.face_weights[face]:
                    paths[slot], indices[slot] = group.inline_proxy
    if own_proxy is None and plan.proxies:
        used = True  # the outer packed prims need the attributes
    if used:
        _add_proxy_reference_attribs(geo)
        geo.setPrimStringAttribValues("a3ob_proxy_path", paths)
        geo.setPrimIntAttribValues("a3ob_proxy_index", indices)


def _add_proxy_reference_attribs(geo) -> None:
    geo.addAttrib(hou.attribType.Prim, "a3ob_proxy_path", "")
    geo.addAttrib(hou.attribType.Prim, "a3ob_proxy_index", 0)


# -- Selections (spec 7) ---------------------------------------------------------------------------------------------

def _write_selections(geo, plan: LodPlan, subset: Subset, options: ImportOptions, skip_empty: bool) -> None:
    """Step 6: a point group per selection, weights where they carry data, prim groups where the face bytes differ."""
    points = geo.points()
    prims = geo.prims()
    for group in plan.groups:
        members = _point_members(group.tagg, subset)
        face_bytes = [group.tagg.face_weights[f] for f in subset.faces]
        if skip_empty and not members and not any(face_bytes):
            continue
        encoded = names.encode_group_name(group.name)
        geo.createPointGroup(encoded).add([points[slot] for slot, _ in members])
        _write_point_weights(geo, encoded, members, len(points))
        derived = _derived_face_bytes(plan, group.tagg, subset)
        if options.facegroups or face_bytes != derived:
            _write_face_group(geo, encoded, prims, face_bytes)


def _point_members(tagg: p3d.SelectionTagg, subset: Subset) -> List:
    """``(local point number, weight byte)`` for the members among the subset's points."""
    weights = tagg.vertex_weights
    return [(slot, weights[index]) for slot, index in enumerate(subset.points) if weights[index]]


def _write_point_weights(geo, encoded: str, members, count: int) -> None:
    """``a3ob_w_<group>``, default 1.0, only when some member byte is not 1 (spec 7.2)."""
    if all(byte == 1 for _, byte in members):
        return
    attrib = names.point_weight_attrib(encoded)
    geo.addAttrib(hou.attribType.Point, attrib, 1.0)
    values = [1.0] * count
    for slot, byte in members:
        values[slot] = p3d.decode_weight(byte)
    geo.setPointFloatAttribValues(attrib, values)


def _derived_face_bytes(plan: LodPlan, tagg: p3d.SelectionTagg, subset: Subset) -> List[int]:
    """The rule that Object Builder's exporter uses: a face is a member when all its points are (spec 7.1)."""
    lod = plan.lod
    weights = tagg.vertex_weights
    derived = []
    for face in subset.faces:
        file_points = lod.face_vertices[4 * face:4 * face + lod.face_sides[face]]
        derived.append(1 if file_points and all(weights[v] for v in file_points) else 0)
    return derived


def _write_face_group(geo, encoded: str, prims, face_bytes: List[int]) -> None:
    """The prim group, and ``a3ob_fw_<group>`` (default 1.0) when a face byte is not 0 or 1."""
    geo.createPrimGroup(encoded).add([prims[slot] for slot, byte in enumerate(face_bytes) if byte])
    if all(byte in (0, 1) for byte in face_bytes):
        return
    attrib = names.prim_weight_attrib(encoded)
    geo.addAttrib(hou.attribType.Prim, attrib, 1.0)
    geo.setPrimFloatAttribValues(attrib, [p3d.decode_weight(byte) if byte else 1.0 for byte in face_bytes])


# -- Sharp edges and details (mesh only) -------------------------------------------------------------------------

def _write_sharp_edges(geo, plan: LodPlan, subset: Subset) -> None:
    """Step 7 (spec 9.2): real polygon edges go into the edge group, anything else into ``a3ob_sharp_extra``."""
    if plan.sharp is None:
        return
    points = geo.points()
    group = geo.createEdgeGroup(SHARP_GROUP)
    extras: List[int] = []
    for a, b in plan.sharp.pairs():
        edge = None
        if a != b and a in subset.local and b in subset.local:
            edge = geo.findEdge(points[subset.local[a]], points[subset.local[b]])
        if edge is None or group.contains(edge):  # not an edge, or the same edge again (either direction)
            extras.extend((a, b))
        else:
            group.add(edge)
    if extras:
        hou_util.add_detail_array(geo, "a3ob_sharp_extra", "int", extras)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _canonical_property(tagg: p3d.PropertyTagg) -> bytes:
    size = p3d.model.PROPERTY_FIELD_SIZE
    return (binary.encode_asciiz(tagg.key).ljust(size, b"\x00") + binary.encode_asciiz(tagg.value).ljust(size, b"\x00"))


def _write_details(geo, plan: LodPlan) -> None:
    """Step 8: the detail attributes of spec 3.3."""
    lod = plan.lod
    hou_util.add_detail(geo, "a3ob_spec_version", 1)
    hou_util.add_detail(geo, "a3ob_lod_type", int(lod.lod_type))
    hou_util.add_detail(geo, "a3ob_lod_resolution", float(lod.resolution))
    hou_util.add_detail(geo, "a3ob_lod_signature", names.float_bits(lod.resolution_signature))
    hou_util.add_detail(geo, "a3ob_lod_flags", names.to_int32(lod.flags))
    if lod.eof_active != 1:
        hou_util.add_detail(geo, "a3ob_eof_active", lod.eof_active)
    _write_property_details(geo, plan)
    _write_order_details(geo, plan)
    _write_blob_details(geo, plan)
    hou_util.add_detail_array(geo, "a3ob_source_counts", "int", [lod.point_count, lod.face_count, lod.corner_count])


def _write_property_details(geo, plan: LodPlan) -> None:
    """Spec 11.1: parallel name and value arrays, and the raw payload when it has bytes after a NUL."""
    if not plan.properties:
        return
    hou_util.add_detail_array(geo, "a3ob_prop_names", "string", [tagg.key for tagg in plan.properties])
    hou_util.add_detail_array(geo, "a3ob_prop_values", "string", [tagg.value for tagg in plan.properties])
    raw = [_b64(tagg.payload()) if tagg.payload() != _canonical_property(tagg) else "" for tagg in plan.properties]
    if any(raw):
        hou_util.add_detail_array(geo, "a3ob_prop_raw", "string", raw)


def _write_order_details(geo, plan: LodPlan) -> None:
    """Spec 11.3: the file order of the TAGGs, and their raw active bytes where they are not all 1."""
    if not plan.order:
        return
    hou_util.add_detail_array(geo, "a3ob_tagg_order", "string", plan.order)
    if plan.needs_order_active:
        hou_util.add_detail_array(geo, "a3ob_tagg_active", "int", plan.order_active)


def _write_blob_details(geo, plan: LodPlan) -> None:
    """Spec 11.2: TAGGs without a Houdini form, as three parallel arrays (name, raw active byte, base64 payload)."""
    if not plan.blobs:
        return
    hou_util.add_detail_array(geo, "a3ob_tagg_unknown_names", "string", [blob.name for blob in plan.blobs])
    hou_util.add_detail_array(geo, "a3ob_tagg_unknown_active", "int", [blob.active for blob in plan.blobs])
    hou_util.add_detail_array(geo, "a3ob_tagg_unknown_data", "string", [_b64(blob.payload) for blob in plan.blobs])


# -- Packed proxies (spec 13.3), last -----------------------------------------------------------------------------

def _write_packed_proxies(geo, plan: LodPlan, options: ImportOptions) -> None:
    """Step 9: one PackedGeometry prim per proxy, in ascending proxy face order, after all polygons and points."""
    if not plan.proxies:
        return
    for name in ("a3ob_proxy_src_P", "a3ob_proxy_src_N"):
        geo.addArrayAttrib(hou.attribType.Prim, name, hou.attribData.Float)
    for proxy in plan.proxies:
        _write_packed_proxy(geo, plan, proxy, options)


def _write_packed_proxy(geo, plan: LodPlan, proxy: ProxyPlan, options: ImportOptions) -> None:
    lod = plan.lod
    frame = proxies.houdini_frame(lod, proxy)
    inner = _build_subset(plan, Subset(proxy.points, [proxy.face]), frame, None, proxy, options)
    prim = geo.createPacked("PackedGeometry")
    prim.setEmbeddedGeometry(inner.freeze())
    prim.setTransform(hou.Matrix4(frame.matrix_rows()))
    prim.setAttribValue("a3ob_proxy_path", proxy.path)
    prim.setAttribValue("a3ob_proxy_index", proxy.index)
    prim.setAttribValue("a3ob_proxy_src_P", [c for v in proxy.points for c in lod.point(v)])
    base = 4 * proxy.face
    prim.setAttribValue("a3ob_proxy_src_N", [c for k in range(3) for c in lod.normal(lod.face_normals[base + k])])

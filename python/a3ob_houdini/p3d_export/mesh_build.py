# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): the face, vertex, normal and UV collection is rewritten for
# Houdini data and follows the Houdini data model (docs/design/houdini-data-model.md) where that changes A3OB (axis
# map, raw flags, float64 UVs, first-appearance normal table). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren"
# Frieden (original ArmaToolbox).
"""Spec 16 step 3.1: the arrays that ``LOD.set_mesh`` takes, from the Houdini mesh and the rebuilt proxies. No ``hou``.

Order of the file mesh (spec 6): the exported mesh points in point-number order (pack points skipped), then three
points per packed proxy; the polygons in prim order (inline proxies in place), then one triangle per packed proxy.
"""

import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import messages
from . import normals as normals_mod
from .data import LodData, MeshData, Poly
from .options import ExportOptions
from .proxy_rebuild import FileProxy, file_corner_order

__all__ = ["DEFAULT_VERTEX_FLAGS", "FileMesh", "build_file_mesh", "uv_set_id", "FACE_UV"]

DEFAULT_VERTEX_FLAGS = 0x02000000
FACE_UV = "a3ob_face_uv"
_UV_NAME = re.compile(r"uv(\d*)")


def uv_set_id(attrib: str) -> Optional[int]:
    """Spec 8: ``uv`` is set 0 and ``uv<k+1>`` is set k (k >= 1). Anything else (``uv0``, ``uv1``, ``uv01``) is None."""
    match = _UV_NAME.fullmatch(attrib)
    if match is None:
        return None
    digits = match.group(1)
    if digits == "":
        return 0
    value = int(digits)
    if str(value) != digits or value < 2:
        return None
    return value - 1


@dataclass
class FileMesh:
    """Everything ``LOD.set_mesh`` needs, plus the maps the TAGG steps need."""

    points: List[float] = field(default_factory=list)
    point_flags: List[int] = field(default_factory=list)
    polygons: List[List[int]] = field(default_factory=list)
    normal_table: List[float] = field(default_factory=list)
    normal_indices: List[int] = field(default_factory=list)
    record_uvs: List[float] = field(default_factory=list)
    face_flags: List[int] = field(default_factory=list)
    textures: List[str] = field(default_factory=list)
    materials: List[str] = field(default_factory=list)
    #: UV set id -> (u, v) per corner, flat, in file face and corner order.
    uv_sets: Dict[int, List[float]] = field(default_factory=dict)
    masses: Optional[List[float]] = None
    #: Mesh point number -> file point index.
    point_map: Dict[int, int] = field(default_factory=dict)
    #: Indices into ``MeshData.polys`` of the mesh faces, in file face order (they start at file face 0).
    mesh_faces: List[int] = field(default_factory=list)
    #: File index of the first point and the face of each packed proxy, parallel to the proxy list.
    proxy_points: List[int] = field(default_factory=list)
    proxy_faces: List[int] = field(default_factory=list)

    @property
    def corner_count(self) -> int:
        return sum(len(polygon) for polygon in self.polygons)


class _NormalTable:
    """Spec 9.1: first-appearance order, de-duplicated by the exact float32 value."""

    def __init__(self) -> None:
        self.seen: Dict[bytes, int] = {}
        self.flat: List[float] = []

    def index(self, normal) -> int:
        try:
            key = struct.pack("<3f", *normal)
        except (OverflowError, struct.error):
            key = repr(tuple(normal)).encode()
        found = self.seen.get(key)
        if found is None:
            found = self.seen[key] = len(self.seen)
            self.flat.extend(normal)
        return found


def _file_normal(normal) -> Tuple[float, float, float]:
    """Spec 4.3: ``(-Nx, -Ny, Nz)``."""
    return (-normal[0], -normal[1], normal[2])


def _file_uv(uv) -> Tuple[float, float]:
    """Spec 4.3: ``(u, 1 - v)``."""
    return (uv[0], 1.0 - uv[1])


def kept_polygons(data: LodData, report: messages.Report) -> List[int]:
    """Indices of the polygons that are written. Faces with 0 to 2 sides are skipped with a warning (spec 5)."""
    mesh = data.mesh
    kept: List[int] = []
    short: List[int] = []
    long_: List[int] = []
    for number, poly in enumerate(mesh.polys):
        sides = len(poly.points)
        if sides < 3:
            short.append(poly.number)
        elif sides > 4:
            long_.append(poly.number)
        else:
            kept.append(number)
    if short:
        report.warning(messages.format_problem(
            data.label, "%d polygons have fewer than 3 points and are skipped (%s)" % (
                len(short), messages.list_elements("prims", short)),
            "The faces after them move up, so data that depends on the face count is dropped"))
    if long_:
        report.error(messages.format_problem(
            data.label, "%d polygons have more than 4 points (%s)" % (len(long_), messages.list_elements("prims", long_)),
            "P3D faces have 3 or 4 points. Triangulate or Divide them"))
    return kept


def sort_by_section(kept: List[int], polys: List[Poly]) -> List[int]:
    """Sort sections option: faces with the same (texture, material) pair become one run. Sections keep the order in
    which their pair first appears, and faces keep their order inside a section."""
    order: Dict[Tuple[str, str], int] = {}
    for number in kept:
        order.setdefault((polys[number].texture, polys[number].material), len(order))
    return sorted(kept, key=lambda number: order[(polys[number].texture, polys[number].material)])


def _polygon_normals(mesh: MeshData, kept: List[int]) -> Dict[int, List]:
    """The Houdini normal of every vertex of every kept polygon, computed where the geometry has none."""
    missing = [number for number in kept if mesh.polys[number].normals is None]
    computed = normals_mod.compute_missing_normals(mesh, missing) if missing else {}
    return {number: (mesh.polys[number].normals if mesh.polys[number].normals is not None else computed[number])
            for number in kept}


def build_file_mesh(data: LodData, proxies: List[FileProxy], options: ExportOptions,
                    report: messages.Report) -> FileMesh:
    """Spec 16 step 3.1: points, polygons, the normals table, UVs and face data."""
    mesh = data.mesh
    out = FileMesh()
    kept = kept_polygons(data, report)
    if options.sort_sections:
        kept = sort_by_section(kept, mesh.polys)
    out.mesh_faces = kept
    _add_mesh_points(mesh, out)
    set_ids = [uv_set_id(name) for name in mesh.uv_names]
    for set_id in set_ids:
        out.uv_sets[set_id] = []
    table = _NormalTable()
    houdini_normals = _polygon_normals(mesh, kept)
    for number in kept:
        _add_mesh_face(data, mesh.polys[number], houdini_normals[number], out, table, set_ids)
    for proxy in proxies:
        _add_proxy(proxy, out, table, set_ids, mesh.uv_names)
    out.normal_table = table.flat
    return out


def _add_mesh_points(mesh: MeshData, out: FileMesh) -> None:
    if mesh.masses is not None:
        out.masses = []
    for number, position in enumerate(mesh.points):
        if number in mesh.pack_points:
            continue
        out.point_map[number] = len(out.point_flags)
        out.points.extend((position[0], position[1], -position[2]))
        flags = mesh.point_flags[number] if mesh.point_flags is not None else DEFAULT_VERTEX_FLAGS
        out.point_flags.append(flags & 0xFFFFFFFF)
        if out.masses is not None:
            out.masses.append(mesh.masses[number])


def _record_uv_name(uvs) -> Optional[str]:
    """Spec 8: the face records carry ``a3ob_face_uv`` if it exists, else ``uv``, else zeros."""
    if FACE_UV in uvs:
        return FACE_UV
    return "uv" if "uv" in uvs else None


def _add_mesh_face(data: LodData, poly: Poly, normals, out: FileMesh, table: _NormalTable, set_ids) -> None:
    mirrored = data.mirrored
    mesh = data.mesh
    out.polygons.append([out.point_map[p] for p in file_corner_order(poly.points, mirrored)])
    out.normal_indices.extend(table.index(_file_normal(n)) for n in file_corner_order(normals, mirrored))
    record = _record_uv_name(poly.uvs)
    if record is None:
        out.record_uvs.extend([0.0] * (2 * len(poly.points)))
    else:
        for uv in file_corner_order(poly.uvs[record], mirrored):
            out.record_uvs.extend(_file_uv(uv))
    for name, set_id in zip(mesh.uv_names, set_ids):
        values = poly.uvs.get(name)
        if values is None:
            out.uv_sets[set_id].extend([0.0] * (2 * len(poly.points)))
        else:
            for uv in file_corner_order(values, mirrored):
                out.uv_sets[set_id].extend(_file_uv(uv))
    out.face_flags.append(poly.flags & 0xFFFFFFFF)
    out.textures.append(poly.texture)
    out.materials.append(poly.material)


def _add_proxy(proxy: FileProxy, out: FileMesh, table: _NormalTable, set_ids, uv_names) -> None:
    """Spec 13.5 step 3: three points and one triangle, appended after the mesh."""
    base = len(out.point_flags)
    out.proxy_points.append(base)
    out.proxy_faces.append(len(out.polygons))
    for position, flags, mass in zip(proxy.points, proxy.point_flags, proxy.masses):
        out.points.extend(position)
        out.point_flags.append(flags)
        if out.masses is not None:
            out.masses.append(mass)
    out.polygons.append([base + i for i in proxy.corners])
    out.normal_indices.extend(table.index(n) for n in proxy.normals)
    record = _record_uv_name(proxy.uvs)
    if record is None:
        out.record_uvs.extend([0.0] * 6)
    else:
        for uv in proxy.uvs[record]:
            out.record_uvs.extend(_file_uv(uv))
    for name, set_id in zip(uv_names, set_ids):
        values = proxy.uvs.get(name)
        if values is None:
            out.uv_sets[set_id].extend([0.0] * 6)
        else:
            for uv in values:
                out.uv_sets[set_id].extend(_file_uv(uv))
    out.face_flags.append(proxy.flags)
    out.textures.append(proxy.texture)
    out.materials.append(proxy.material)

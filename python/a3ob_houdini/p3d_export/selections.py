# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): selections are rebuilt from point groups, weight attributes,
# prim groups and packed proxies (spec 7 and 13.5) instead of Blender vertex groups; "generate components" follows
# A3OB's ``generate_components`` (line 294). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original
# ArmaToolbox).
"""Selection bytes for the file (spec 7.2 and 13.5). No ``hou`` here.

A selection is rebuilt from every place it lives: the mesh point group, the embedded groups of the packed proxies, the
proxies' own ``proxy:<path>.<index>`` selection and the prim attributes of inline proxies. The result is one
``SelectionEntry`` per name, with one byte per file point and one per file face.
"""

import re
from typing import Dict, Iterable, List, Sequence, Tuple

from a3ob_core import p3d

from .. import messages
from .data import GroupData, LodData, MeshData
from .mesh_build import FileMesh
from .options import ExportOptions, matches_mask
from .proxy_rebuild import FileProxy

__all__ = ["SelectionEntry", "weight_byte", "collect_selections", "add_auto_components", "GEOMETRY_TYPES"]

#: Geometry, Geometry Buoyancy, Geometry PhysX (A3OB ``lod_geometries``).
GEOMETRY_TYPES = (6, 7, 8)
_COMPONENT = re.compile(r"component\d+", re.IGNORECASE)


def weight_byte(weight: float) -> int:
    """Spec 7.2: ``clamp(round(255 - 254 w), 1, 255)``. Weight 1 is byte 1, weight 0 is byte 255 (still a member)."""
    if weight != weight:
        raise ValueError("selection weight is NaN")
    return max(1, min(255, int(round(255 - 254 * weight))))


class SelectionEntry:
    """One selection: its name and one weight byte per file point and per file face."""

    def __init__(self, name: str, point_count: int, face_count: int):
        self.name = name
        self.vertex = bytearray(point_count)
        self.face = bytearray(face_count)


def _is_selection(group: GroupData, options: ExportOptions, hint_names) -> bool:
    """Spec 7.2: the mask decides, but a name in ``a3ob_tagg_order`` is always a selection."""
    return group.name in hint_names or matches_mask(options.selection_mask, group.encoded)


def _fill_group(entry: SelectionEntry, group: GroupData, mesh: MeshData, point_map: Dict[int, int],
                faces: Sequence[Tuple[int, int]], report: messages.Report, label: str) -> None:
    """Write the bytes of one group into ``entry``. ``faces`` is ``(polygon index, file face index)``."""
    try:
        for point in group.members:
            index = point_map.get(point)
            if index is None:
                continue
            entry.vertex[index] = weight_byte(group.weights[point] if group.weights is not None else 1.0)
        member_faces = set(group.faces) if group.faces is not None else None
        for number, face in faces:
            if member_faces is not None:
                if number not in member_faces:
                    continue
                weight = group.face_weights[number] if group.face_weights is not None else 1.0
                entry.face[face] = weight_byte(weight)
            elif _all_members(entry, mesh, number, point_map):
                entry.face[face] = 1
    except ValueError:
        report.error(messages.format_problem(label, "the selection %r has a weight that is not a number" % group.name,
                                             "Fix the weight attribute of the group"))


def _all_members(entry: SelectionEntry, mesh: MeshData, number: int, point_map: Dict[int, int]) -> bool:
    """The derived face rule of spec 7.2: a face is a member when all its points are."""
    points = mesh.polys[number].points
    return bool(points) and all(p in point_map and entry.vertex[point_map[p]] for p in points)


def collect_selections(data: LodData, file_mesh: FileMesh, proxies: Sequence[FileProxy], options: ExportOptions,
                       report: messages.Report) -> List[SelectionEntry]:
    """All selections of the LOD in canonical order: mesh groups, groups found only in proxies, proxy selections."""
    label = data.label
    mesh = data.mesh
    hint_names = set(data.details.tagg_order)
    entries: Dict[str, SelectionEntry] = {}
    point_count = len(file_mesh.point_flags)
    face_count = len(file_mesh.polygons)

    def entry_for(name: str) -> SelectionEntry:
        if name not in entries:
            entries[name] = SelectionEntry(name, point_count, face_count)
        return entries[name]

    mesh_faces = [(number, face) for face, number in enumerate(file_mesh.mesh_faces)]
    for group in mesh.groups:
        if _is_selection(group, options, hint_names):
            _fill_group(entry_for(group.name), group, mesh, file_mesh.point_map, mesh_faces, report, label)
    for proxy, base, face in zip(proxies, file_mesh.proxy_points, file_mesh.proxy_faces):
        if proxy.mesh is None:
            continue
        point_map = {0: base, 1: base + 1, 2: base + 2}
        for group in proxy.mesh.groups:
            if _is_selection(group, options, hint_names):
                _fill_group(entry_for(group.name), group, proxy.mesh, point_map, [(0, face)], report, label)
    _add_packed_proxy_selections(proxies, file_mesh, entry_for, report, label)
    _add_inline_proxy_selections(data, file_mesh, entry_for, report)
    return list(entries.values())


def _add_packed_proxy_selections(proxies, file_mesh: FileMesh, entry_for, report, label) -> None:
    """Spec 13.5 step 4: byte 1 on the three points and the face of every packed proxy."""
    seen = set()
    for proxy, base, face in zip(proxies, file_mesh.proxy_points, file_mesh.proxy_faces):
        name = proxy.name
        if name in seen:
            report.error(messages.format_problem(
                label, "two packed proxies are both called %s" % name, "Give one of them another index"))
            continue
        seen.add(name)
        entry = entry_for(name)
        for k in range(3):
            entry.vertex[base + k] = 1
        entry.face[face] = 1


def _add_inline_proxy_selections(data: LodData, file_mesh: FileMesh, entry_for, report) -> None:
    """Spec 13.5 "Inline proxies": polygons with a proxy path add byte 1 where the group of that name has none."""
    not_triangle: List[int] = []
    for face, number in enumerate(file_mesh.mesh_faces):
        poly = data.mesh.polys[number]
        if poly.proxy is None or poly.proxy[0] == "":
            continue
        try:
            name = p3d.format_proxy_name(*poly.proxy)
        except ValueError:
            report.error(messages.format_problem(
                data.label, "polygon %d has the negative proxy index %d" % (poly.number, poly.proxy[1]),
                "Use an index from 0 to 999"))
            continue
        if len(poly.points) != 3:
            not_triangle.append(poly.number)
        entry = entry_for(name)
        for point in poly.points:
            index = file_mesh.point_map[point]
            if not entry.vertex[index]:
                entry.vertex[index] = 1
        if not entry.face[face]:
            entry.face[face] = 1
    if not_triangle:
        report.warning(messages.format_problem(
            data.label, "inline proxy polygons that are not triangles (%s)" % messages.list_elements("prims", not_triangle),
            "A proxy must be one triangle"))


def add_auto_components(data: LodData, file_mesh: FileMesh, entries: List[SelectionEntry],
                        report: messages.Report) -> int:
    """Auto-components option (A3OB "generate components"): for a Geometry-type LOD with no ``component<digits>``
    selection, one selection ``Component01``... per connected piece of the mesh. Returns how many were made."""
    if data.details.lod_type not in GEOMETRY_TYPES:
        return 0
    if any(_COMPONENT.match(entry.name) for entry in entries):
        return 0
    mesh_polygons = file_mesh.polygons[:len(file_mesh.mesh_faces)]
    parent: Dict[int, int] = {}

    def find(a: int) -> int:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for polygon in mesh_polygons:
        for point in polygon[1:]:
            parent[find(point)] = find(polygon[0])
    pieces: Dict[int, List[int]] = {}
    for point in sorted(parent):
        pieces.setdefault(find(point), []).append(point)
    count = len(file_mesh.point_flags)
    faces = len(file_mesh.polygons)
    for number, points in enumerate(sorted(pieces.values(), key=lambda members: members[0]), start=1):
        entry = SelectionEntry("Component%02d" % number, count, faces)
        for point in points:
            entry.vertex[point] = 1
        wanted = set(points)
        for face, polygon in enumerate(mesh_polygons):
            if all(p in wanted for p in polygon):
                entry.face[face] = 1
        entries.append(entry)
    return len(pieces)

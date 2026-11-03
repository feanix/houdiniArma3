# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): A3OB joins separate proxy objects; here the proxy triangle is
# rebuilt from a packed prim's embedded geometry and transform (spec 13.5). A3OB: MrClock (present add-on),
# Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""Rebuild the triangle of a packed proxy in file space (spec 13.5, steps 1 and 2). No ``hou`` here.

``rebuild_proxy`` returns a ``FileProxy``: three points in embedded point order, one triangle in file corner order, its
normals and its other face data. The selection bytes of the embedded groups are not done here (``selections``).
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from a3ob_core import p3d

from .. import messages
from . import transforms
from .data import MeshData, ProxyData, Vec

__all__ = ["FileProxy", "rebuild_proxy", "SNAP_TOLERANCE", "CANONICAL_TRIANGLE", "file_corner_order"]

#: Spec 13.5 step 1: how close the rebuilt values must be to the stored source values to be replaced by them.
SNAP_TOLERANCE = 1e-5
#: Spec 13.5 step 2: the canonical proxy triangle in Houdini proxy-local space: C, B, A.
CANONICAL_TRIANGLE = ((0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, -1.0))


def file_corner_order(values, mirrored: bool = False) -> list:
    """Spec 4.3: Houdini vertex ``k`` is file corner ``(-k) mod n``, the same map back. A mirror reverses the Houdini
    order first (spec 3.2)."""
    seq = list(values)
    if mirrored:
        seq.reverse()
    count = len(seq)
    return [seq[(-j) % count] for j in range(count)]


@dataclass
class FileProxy:
    """One proxy triangle in file space."""

    path: str
    index: int
    #: The three points (file axes), in embedded point order.
    points: List[Vec]
    point_flags: List[int]
    masses: List[float]
    #: Indices into ``points`` in file corner order.
    corners: Tuple[int, int, int]
    #: Corner normals in file corner order (file axes).
    normals: List[Vec]
    #: UV set name -> (u, v) per file corner, as Houdini holds them (the face-record UV under ``a3ob_face_uv``).
    uvs: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    flags: int = 0
    texture: str = ""
    material: str = ""
    #: The embedded mesh whose groups still apply, or None for the canonical fallback.
    mesh: Optional[MeshData] = None

    @property
    def name(self) -> str:
        return p3d.format_proxy_name(self.path, self.index)


def _sub(a, b) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normalized(v) -> Vec:
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length == 0.0:
        return (0.0, 1.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _reference(proxy: ProxyData) -> Tuple[str, int, bool]:
    """Spec 13.3 "Proxy's own selection": outer path and index win; the embedded ones fill in."""
    if proxy.outer_path:
        return proxy.outer_path, proxy.outer_index, bool(proxy.inner_path) and (
            (proxy.inner_path, proxy.inner_index) != (proxy.outer_path, proxy.outer_index))
    return proxy.inner_path, proxy.inner_index, False


def rebuild_proxy(proxy: ProxyData, label: str, report: messages.Report) -> Optional[FileProxy]:
    """The file-space triangle of a packed proxy, or None (with an error in ``report``)."""
    path, index, mismatch = _reference(proxy)
    where = "packed prim %d" % proxy.prim_number
    if not path:
        report.error(messages.format_problem(label, "%s is neither a LOD nor a proxy (it has no proxy path)" % where,
                                             "Set a3ob_proxy_path on it, or unpack or delete it"))
        return None
    if index < 0:
        report.error(messages.format_problem(label, "%s has the negative proxy index %d" % (where, index),
                                             "Use an index from 0 to 999"))
        return None
    if mismatch:
        report.warning(messages.format_problem(
            label, "%s: the proxy path or index on the prim and inside it differ. The prim's values are used" % where))
    transform = proxy.transform
    mirrored = transforms.det3(transform) < 0.0
    if proxy.valid_triangle:
        result = _from_embedded(proxy, path, index, mirrored)
    else:
        report.warning(messages.format_problem(
            label, "%s (proxy %s) is not exactly one triangle on three points" % (where, p3d.format_proxy_name(path, index)),
            "A plain proxy triangle is written in its place"))
        result = _canonical(path, index, transform, mirrored)
    return result


def _to_file(position) -> Vec:
    return transforms.map_axes(position)


def _to_file_normal(normal) -> Vec:
    return (-normal[0], -normal[1], normal[2])


def _from_embedded(proxy: ProxyData, path: str, index: int, mirrored: bool) -> FileProxy:
    mesh = proxy.mesh
    poly = mesh.polys[0]
    m = proxy.transform
    n_matrix = transforms.normal_matrix(m)
    points = [_to_file(transforms.transform_point(m, p)) for p in mesh.points]
    if poly.normals is not None:
        houdini_normals = [transforms.transform_normal(n_matrix, n) for n in poly.normals]
    else:
        houdini_normals = None
    order = list(poly.points)
    corners_h = order[::-1] if mirrored else order
    corners = tuple(corners_h[(-j) % 3] for j in range(3))
    if houdini_normals is None:
        normals = [_flat_normal(points, corners)] * 3
    else:
        normals_h = houdini_normals[::-1] if mirrored else houdini_normals
        normals = [_to_file_normal(normals_h[(-j) % 3]) for j in range(3)]
    points, normals = _snap(proxy, points, normals)
    uvs = {}
    for name, values in poly.uvs.items():
        seq = values[::-1] if mirrored else values
        uvs[name] = [seq[(-j) % 3] for j in range(3)]
    flags = mesh.point_flags if mesh.point_flags is not None else [0, 0, 0]
    masses = mesh.masses if mesh.masses is not None else [0.0, 0.0, 0.0]
    return FileProxy(path, index, points, [f & 0xFFFFFFFF for f in flags], list(masses), corners, normals, uvs,
                     poly.flags & 0xFFFFFFFF, poly.texture, poly.material, mesh)


def _flat_normal(points, corners) -> Vec:
    """A file-space face normal: Object Builder's normals follow the cross product of the file corner order (F3)."""
    a, b, c = (points[i] for i in corners)
    return _normalized(_cross(_sub(b, a), _sub(c, a)))


def _within(values, stored: Optional[Tuple[float, ...]]) -> bool:
    return stored is not None and len(stored) == len(values) and all(
        abs(a - b) <= SNAP_TOLERANCE for a, b in zip(values, stored))


def _snap(proxy: ProxyData, points, normals):
    """Spec 13.5 step 1, snap guard: a proxy that did not move is written with exactly the values it was read with."""
    flat_p = [c for p in points for c in p]
    flat_n = [c for n in normals for c in n]
    if _within(flat_p, proxy.src_p):
        src = proxy.src_p
        points = [tuple(src[3 * i:3 * i + 3]) for i in range(3)]
    if _within(flat_n, proxy.src_n):
        src = proxy.src_n
        normals = [tuple(src[3 * i:3 * i + 3]) for i in range(3)]
    return points, normals


def _canonical(path: str, index: int, transform, mirrored: bool) -> FileProxy:
    """Spec 13.5 step 2: the plain triangle ``C, B, A``, moved by the prim's transform."""
    world = [_to_file(transforms.transform_point(transform, p)) for p in CANONICAL_TRIANGLE]
    corners = (0, 2, 1) if mirrored else (0, 1, 2)
    normal = _flat_normal(world, corners)
    return FileProxy(path, index, world, [0, 0, 0], [0.0, 0.0, 0.0], corners, [normal] * 3)

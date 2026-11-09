# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Vertex normals for a mesh that has no ``N`` (spec 9.1, "Export without N"). No ``hou`` here.

Area-weighted vertex normals, split along ``a3ob_sharp`` edges: two polygons share a normal at a point only if the edge
that joins them (and ends at that point) is not sharp. Houdini's Normal SOP cannot do this (it has no hard-edge input).

The normals are in Houdini axes. A Houdini polygon whose algebraic cross product points along +Z has the normal -Z
(spec F5), so the face normal here is the negated sum of cross products (Newell's method).
"""

from typing import Dict, List, Sequence, Set, Tuple

from .data import MeshData, Vec

__all__ = ["face_normal", "compute_missing_normals"]


def face_normal(positions: Sequence[Vec]) -> Vec:
    """The area-weighted Houdini normal of a polygon (not normalised)."""
    x = y = z = 0.0
    count = len(positions)
    for k in range(count):
        a = positions[k]
        b = positions[(k + 1) % count]
        x += a[1] * b[2] - a[2] * b[1]
        y += a[2] * b[0] - a[0] * b[2]
        z += a[0] * b[1] - a[1] * b[0]
    return (-x, -y, -z)


def _normalized(v: Vec) -> Vec:
    length = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    if length == 0.0:
        return (0.0, 1.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


class _Groups:
    """Union-find over (polygon, corner) nodes."""

    def __init__(self) -> None:
        self.parent: Dict[Tuple[int, int], Tuple[int, int]] = {}

    def find(self, node):
        parent = self.parent
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, a, b) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def compute_missing_normals(mesh: MeshData, wanted: Sequence[int]) -> Dict[int, List[Vec]]:
    """Normals for the polygons in ``wanted`` (indices into ``mesh.polys``), one per vertex, in Houdini vertex order."""
    polys = mesh.polys
    sharp: Set[Tuple[int, int]] = {(min(a, b), max(a, b)) for a, b in (mesh.sharp or ())}
    edge_users: Dict[Tuple[int, int], List[int]] = {}
    for number, poly in enumerate(polys):
        count = len(poly.points)
        for k in range(count):
            a, b = poly.points[k], poly.points[(k + 1) % count]
            edge_users.setdefault((min(a, b), max(a, b)), []).append(number)
    groups = _Groups()
    for number, poly in enumerate(polys):
        for point in poly.points:
            groups.find((number, point))
    for edge, users in edge_users.items():
        if edge in sharp or len(users) < 2:
            continue
        for point in edge:
            first = users[0]
            for other in users[1:]:
                groups.union((first, point), (other, point))
    sums: Dict[Tuple[int, int], List[float]] = {}
    for number, poly in enumerate(polys):
        area = face_normal([mesh.points[p] for p in poly.points])
        for point in poly.points:
            total = sums.setdefault(groups.find((number, point)), [0.0, 0.0, 0.0])
            total[0] += area[0]
            total[1] += area[1]
            total[2] += area[2]
    result: Dict[int, List[Vec]] = {}
    for number in wanted:
        poly = polys[number]
        result[number] = [_normalized(tuple(sums[groups.find((number, point))])) for point in poly.points]
    return result

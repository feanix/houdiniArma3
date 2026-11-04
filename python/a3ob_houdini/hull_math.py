# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""A 3D convex hull of points, in plain Python (no ``hou``, so CPython tests can run it).

The Components node needs the hull of one piece of a LOD. This Houdini build has no Convex Hull SOP verb, and Blender's
``bpy.ops.mesh.convex_hull`` (A3OB utilities/structure.py:77) is not available, so the hull is made here: the simple
incremental method. Start with a tetrahedron, then for each other point remove the faces that see it and close the hole.
"""

from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["convex_hull"]

Vec = Tuple[float, float, float]
Face = Tuple[int, int, int]

#: Distance from a plane, in units of the size of the point cloud, below which a point counts as on the plane.
RELATIVE_TOLERANCE = 1e-9


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a: Vec) -> float:
    return _dot(a, a) ** 0.5


def _seed(points: Sequence[Vec], tolerance: float) -> Optional[Tuple[int, int, int, int]]:
    """Four points that are not in one plane, or None when there are none."""
    first = 0
    second = max(range(len(points)), key=lambda i: _length(_sub(points[i], points[first])))
    if _length(_sub(points[second], points[first])) <= tolerance:
        return None
    edge = _sub(points[second], points[first])
    third = max(range(len(points)), key=lambda i: _length(_cross(edge, _sub(points[i], points[first]))))
    normal = _cross(edge, _sub(points[third], points[first]))
    if _length(normal) <= tolerance * _length(edge):
        return None
    fourth = max(range(len(points)), key=lambda i: abs(_dot(normal, _sub(points[i], points[first]))))
    if abs(_dot(normal, _sub(points[fourth], points[first]))) <= tolerance * _length(normal):
        return None
    return first, second, third, fourth


def convex_hull(points: Sequence[Vec]) -> Optional[List[Face]]:
    """The triangles of the convex hull, as point numbers, wound counter-clockwise seen from outside (the outward
    normal of Houdini). Points inside a face or on an edge may be left out. Returns None when there are fewer than
    4 points or all points lie in one plane (no volume, so no hull).
    """
    if len(points) < 4:
        return None
    low = [min(p[k] for p in points) for k in range(3)]
    high = [max(p[k] for p in points) for k in range(3)]
    tolerance = RELATIVE_TOLERANCE * max(max(high[k] - low[k] for k in range(3)), 1e-300)
    seed = _seed(points, tolerance)
    if seed is None:
        return None
    a, b, c, d = seed
    inside = tuple(sum(points[i][k] for i in seed) / 4.0 for k in range(3))
    faces: Dict[Face, Tuple[Vec, float]] = {}  # face -> (unit normal, plane offset)

    def add(face: Face) -> None:
        p0, p1, p2 = (points[i] for i in face)
        normal = _cross(_sub(p1, p0), _sub(p2, p0))
        size = _length(normal)
        if size == 0.0:
            return
        normal = (normal[0] / size, normal[1] / size, normal[2] / size)
        faces[face] = (normal, _dot(normal, p0))

    for face in ((a, b, c), (a, b, d), (a, c, d), (b, c, d)):
        p0, p1, p2 = (points[i] for i in face)
        if _dot(_cross(_sub(p1, p0), _sub(p2, p0)), _sub(inside, p0)) > 0:  # the centre is on the normal side: flip
            face = (face[0], face[2], face[1])
        add(face)
    for index in range(len(points)):
        if index in seed:
            continue
        point = points[index]
        visible = [face for face, (normal, offset) in faces.items() if _dot(normal, point) - offset > tolerance]
        if not visible:
            continue
        edges = set()
        for face in visible:
            edges.update(((face[0], face[1]), (face[1], face[2]), (face[2], face[0])))
        for face in visible:
            del faces[face]
        for first, second in sorted(edges):
            if (second, first) not in edges:  # a border edge of the hole
                add((first, second, index))
    return sorted(faces)

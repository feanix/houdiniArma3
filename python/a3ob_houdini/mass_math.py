# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/masses.py and utilities/generic.py, v2.5.1 (9f2b8bc): closed components, signed volume,
# factor scaling, the colour ramp and the centre of gravity are A3OB's rules, rewritten without Blender.
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""The arithmetic of the Mass node. Plain Python: no ``hou``, so CPython tests can run it.

Inputs are plain lists: ``positions`` is one ``(x, y, z)`` per point and ``polygons`` is one tuple of point numbers
per polygon.
"""

import math
from bisect import bisect_right
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "Component", "closed_components", "component_volume", "scale_factors", "ramp_colors", "centre_of_mass", "stats",
]

Vec = Tuple[float, float, float]


class Component(NamedTuple):
    """A group of polygons that share points. ``points`` is sorted; ``closed`` is A3OB's "contiguous" test."""

    points: Tuple[int, ...]
    polygons: Tuple[int, ...]
    closed: bool


def closed_components(polygons: Sequence[Sequence[int]]) -> List[Component]:
    """Split the polygons into components (linked by shared points). A3OB ignores a component that is not closed.

    Closed means: at least 4 triangles once the polygons are fanned, and every edge is used by exactly two polygons
    in opposite directions.
    """
    parent: Dict[int, int] = {}

    def find(point: int) -> int:
        parent.setdefault(point, point)
        while parent[point] != point:
            parent[point] = parent[parent[point]]
            point = parent[point]
        return point

    for polygon in polygons:
        if len(polygon) == 0:
            continue
        root = find(polygon[0])
        for point in polygon[1:]:
            other = find(point)
            if other != root:
                parent[other] = root
    members: Dict[int, List[int]] = {}
    for number, polygon in enumerate(polygons):
        if len(polygon) >= 1:
            members.setdefault(find(polygon[0]), []).append(number)
    result = []
    for numbers in members.values():
        points = sorted({point for number in numbers for point in polygons[number]})
        result.append(Component(tuple(points), tuple(numbers), _is_closed(polygons, numbers)))
    result.sort(key=lambda component: component.polygons[0])
    return result


def _is_closed(polygons: Sequence[Sequence[int]], numbers: Sequence[int]) -> bool:
    triangles = sum(max(len(polygons[number]) - 2, 0) for number in numbers)
    if triangles < 4:
        return False
    directed: Dict[Tuple[int, int], int] = {}
    for number in numbers:
        polygon = polygons[number]
        for k, point in enumerate(polygon):
            edge = (point, polygon[(k + 1) % len(polygon)])
            directed[edge] = directed.get(edge, 0) + 1
    for (a, b), count in directed.items():
        if count != 1 or directed.get((b, a), 0) != 1:
            return False
    return True


def component_volume(positions: Sequence[Vec], polygons: Sequence[Sequence[int]], numbers: Sequence[int]) -> float:
    """The signed volume: the sum of tetrahedra between each fanned triangle and the origin (A3OB ``calculate_volume``).

    Valid only for a closed component. The sign follows the winding: positive when the faces look outward.
    """
    parts = []
    for number in numbers:
        polygon = polygons[number]
        for k in range(1, len(polygon) - 1):
            a, b, c = positions[polygon[0]], positions[polygon[k]], positions[polygon[k + 1]]
            cross = (b[1] * c[2] - b[2] * c[1], b[2] * c[0] - b[0] * c[2], b[0] * c[1] - b[1] * c[0])
            parts.append((a[0] * cross[0] + a[1] * cross[1] + a[2] * cross[2]) / 6.0)
    return math.fsum(parts)


def scale_factors(values: Sequence[float]) -> List[float]:
    """Non-zero values go linearly to 0.001 .. 1; zero stays 0 (A3OB ``scale_factors``)."""
    nonzero = [value for value in values if value != 0]
    if not nonzero:
        return list(values)
    low, high = min(nonzero), max(nonzero)
    if high == low:
        return [value / high for value in values]
    coefficient = 0.999 / (high - low)
    return [max((value - low) * coefficient + 0.001, 0.0) for value in values]


#: A3OB's ramp (``props/scene.py``): no mass (black), then blue, cyan, green, yellow, red.
_COLORS: Tuple[Vec, ...] = ((0, 0, 0), (0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0), (1, 1, 0), (1, 0, 0), (1, 0, 0))
_STOPS = (0, 0.001, 0.25, 0.5, 0.75, 1, 100)


def ramp_colors(masses: Sequence[float]) -> List[Vec]:
    """One colour per mass on A3OB's ramp: blue is the least mass, red the most, black no mass."""
    colors = []
    for factor in scale_factors(masses):
        slot = bisect_right(_STOPS, factor)
        rate = (factor - _STOPS[slot - 1]) / (_STOPS[slot] - _STOPS[slot - 1])
        first, second = _COLORS[slot], _COLORS[slot + 1]
        colors.append(tuple(first[k] + (second[k] - first[k]) * rate for k in range(3)))
    return colors


def ramp_stops() -> List[Tuple[float, Vec]]:
    """The same ramp as ``ramp_colors`` as ``(position, colour)`` stops from 0 to 1, for a node visualizer."""
    return [(float(_STOPS[k]), _COLORS[k + 1]) for k in range(6)]


def ramp_range(masses: Sequence[float]) -> Tuple[float, float]:
    """The values at position 0 and 1 of ``ramp_stops`` that give the colours of ``ramp_colors``.

    Zero mass lands at or below position 0 (black). With no mass at all the range is (0, 1). One ramp cannot keep
    zero black and also keep a mass below 0.1 percent of the largest one blue, so such a mass shows nearly black.
    """
    nonzero = [value for value in masses if value != 0]
    if not nonzero:
        return 0.0, 1.0
    low, high = min(nonzero), max(nonzero)
    if high == low:
        return 0.0, high
    start = low - 0.001 * (high - low) / 0.999
    return (max(start, 0.0) if low > 0 else start), high


def centre_of_mass(positions: Sequence[Vec], masses: Sequence[float]) -> Optional[Vec]:
    """The mass-weighted mean of the points with mass above 0, or None when there are none (A3OB)."""
    total = math.fsum(mass for mass in masses if mass > 0)
    if total <= 0:
        return None
    return tuple(math.fsum(positions[i][k] * mass for i, mass in enumerate(masses) if mass > 0) / total
                 for k in range(3))


class Stats(NamedTuple):
    total: float
    minimum: float
    average: float
    maximum: float
    count: int


def stats(masses: Sequence[float]) -> Stats:
    """Total, minimum, average and maximum of the non-zero masses, and how many there are."""
    nonzero = [mass for mass in masses if mass != 0]
    if not nonzero:
        return Stats(0.0, 0.0, 0.0, 0.0, 0)
    total = math.fsum(nonzero)
    return Stats(total, min(nonzero), total / len(nonzero), max(nonzero), len(nonzero))

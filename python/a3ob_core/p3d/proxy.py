# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/proxy.py (find_axis_vertices, get_transform_rotation, create_proxy) and the proxy
# selection name handling in io/data_p3d.py (P3D_TAGG.is_proxy, P3D_LOD.proxies_to_placeholders), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - pure Python (no mathutils); works in P3D file coordinates (Y up) instead of Blender coordinates (Z up);
# - the proxy name must match "proxy:<path>.<digits>" in full (A3OB accepted trailing text), case-insensitive prefix;
# - no "@proxy_N" placeholders: they only worked around Blender's 63-character vertex group name limit;
# - degenerate triangles raise ValueError.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Proxy selection names and the proxy triangle frame.

A proxy is a named selection ``proxy:<path>.<index>`` (e.g. ``proxy:\\a3\\data_f\\proxies\\backpack.001``) whose
members form one triangle. The triangle encodes the proxy transform
(https://mrcmodding.gitbook.io/home/documents/proxy-coordinates):

- the vertex with the largest angle (the right angle) is the origin;
- the short leg points along the proxy's +Z (forward) and the long leg along its +Y (up);
- +X completes the frame: ``X = long x short``, and Y is re-orthogonalised as ``Y = Z x X``.

All vectors here are in P3D file coordinates (x, y, z) with Y up, as stored in the file. A3OB states the same rule in
Blender coordinates (Y and Z swapped): "short leg = +Y, long leg = +Z".
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple, Optional, Sequence, Tuple

PROXY_PREFIX = "proxy:"

_PROXY_NAME = re.compile(r"proxy:(.*)\.(\d+)", re.IGNORECASE | re.DOTALL)

Vector = Tuple[float, float, float]


def parse_proxy_name(name: str) -> Optional[Tuple[str, int]]:
    """Split ``proxy:<path>.<index>`` into ``(path, index)``; return None if ``name`` is not a proxy selection."""
    match = _PROXY_NAME.fullmatch(name)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def is_proxy_name(name: str) -> bool:
    return parse_proxy_name(name) is not None


def format_proxy_name(path: str, index: int) -> str:
    """Build a proxy selection name. The index is written with at least 3 digits, like Object Builder does."""
    index = int(index)
    if index < 0:
        raise ValueError("proxy index cannot be negative: %d" % index)
    return "%s%s.%03d" % (PROXY_PREFIX, path, index)


# --------------------------------------------------------------------------------------------------------------------
# Vector helpers
# --------------------------------------------------------------------------------------------------------------------

def _sub(a: Sequence[float], b: Sequence[float]) -> Vector:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Sequence[float], b: Sequence[float]) -> Vector:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Sequence[float], s: float) -> Vector:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> Vector:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normalized(a: Sequence[float]) -> Vector:
    length = math.sqrt(_dot(a, a))
    if length == 0.0:
        raise ValueError("degenerate proxy triangle")
    return (a[0] / length, a[1] / length, a[2] / length)


def _angle(a: Sequence[float], b: Sequence[float]) -> float:
    length = math.sqrt(_dot(a, a) * _dot(b, b))
    if length == 0.0:
        raise ValueError("degenerate proxy triangle")
    return math.acos(max(-1.0, min(1.0, _dot(a, b) / length)))


# --------------------------------------------------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------------------------------------------------

class ProxyFrame(NamedTuple):
    """Proxy transform in P3D file coordinates: an origin and three orthonormal axes."""

    origin: Vector
    x_axis: Vector
    y_axis: Vector  # up: along the long leg
    z_axis: Vector  # forward: along the short leg

    def matrix_rows(self) -> Tuple[Tuple[float, ...], ...]:
        """4x4 matrix for row vectors (``p' = p * M``): rows are X, Y, Z and the origin (Houdini's convention)."""
        return (
            (*self.x_axis, 0.0),
            (*self.y_axis, 0.0),
            (*self.z_axis, 0.0),
            (*self.origin, 1.0),
        )


def find_axis_vertices(a: Sequence[float], b: Sequence[float], c: Sequence[float]):
    """Return ``(origin, short_leg_end, long_leg_end)``: the triangle vertices sorted by their angle, largest first.

    Ties keep the input order, as in A3OB (``list.sort`` is stable with ``reverse=True``).
    """
    verts = [
        (a, _angle(_sub(b, a), _sub(c, a))),
        (b, _angle(_sub(a, b), _sub(c, b))),
        (c, _angle(_sub(a, c), _sub(b, c))),
    ]
    verts.sort(reverse=True, key=lambda item: item[1])
    return verts[0][0], verts[1][0], verts[2][0]


def proxy_frame(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> ProxyFrame:
    """Compute the proxy frame from the three triangle points (any order)."""
    origin, short_end, long_end = find_axis_vertices(a, b, c)
    short_leg = _sub(short_end, origin)
    long_leg = _sub(long_end, origin)
    z_axis = _normalized(short_leg)
    x_axis = _normalized(_cross(long_leg, short_leg))
    y_axis = _normalized(_cross(z_axis, x_axis))
    return ProxyFrame(tuple(float(v) for v in origin), x_axis, y_axis, z_axis)


def proxy_triangle(frame: ProxyFrame) -> Tuple[Vector, Vector, Vector]:
    """The canonical proxy triangle for a frame: origin, origin + 2*Y (long leg), origin + 1*Z (short leg).

    Matches A3OB's ``create_proxy`` triangle ((0,0,0), (0,0,2), (0,1,0) in Blender coordinates).
    """
    origin = tuple(frame.origin)
    return (origin, _add(origin, _scale(frame.y_axis, 2.0)), _add(origin, _scale(frame.z_axis, 1.0)))

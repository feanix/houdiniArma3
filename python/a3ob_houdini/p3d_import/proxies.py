# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/proxy.py, v2.5.1 (9f2b8bc): the frame rule is A3OB's, computed by a3ob_core's
# ``proxy_frame`` in file space and mapped here to a right-handed Houdini frame. A3OB: MrClock (present add-on),
# Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""Packed proxies (spec section 13).

A packed proxy is a PackedGeometry prim whose embedded geometry is the proxy triangle in proxy-local space and whose
transform is the triangle's frame in Houdini axes. This module computes the frame (pure Python) and writes the packed
prims (needs ``hou``).
"""

from typing import NamedTuple, Tuple

from a3ob_core import p3d

from .plan import ProxyPlan

__all__ = ["HoudiniFrame", "houdini_frame", "to_local_point", "to_local_normal"]

Vec = Tuple[float, float, float]


class HoudiniFrame(NamedTuple):
    """A proper rotation plus a translation, in Houdini axes (spec 13.4). ``local = (world - origin) . axes``."""

    x: Vec
    y: Vec
    z: Vec
    origin: Vec

    def matrix_rows(self) -> Tuple[Tuple[float, ...], ...]:
        """The packed transform for ``setTransform`` (row-vector convention)."""
        return ((*self.x, 0.0), (*self.y, 0.0), (*self.z, 0.0), (*self.origin, 1.0))


def _map(v) -> Vec:
    """Spec 4.3: the axis map ``M = diag(1, 1, -1)``."""
    return (v[0], v[1], -v[2])


def houdini_frame(lod, proxy: ProxyPlan) -> HoudiniFrame:
    """The packed transform of a proxy (spec 13.4). Do not use M1's ``matrix_rows``: it is left-handed here.

    ``X_h = M f.x``, ``Y_h = M f.y``, ``Z_h = -(M f.z)``, ``C_h = M f.origin`` with ``M = diag(1, 1, -1)``.
    """
    frame = p3d.proxy_frame(*(lod.point(v) for v in proxy.corners))
    z = _map(frame.z_axis)
    return HoudiniFrame(_map(frame.x_axis), _map(frame.y_axis), (-z[0], -z[1], -z[2]), _map(frame.origin))


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def to_local_point(frame: HoudiniFrame, world: Vec) -> Vec:
    """Embedded (proxy-local) position of a Houdini position."""
    d = (world[0] - frame.origin[0], world[1] - frame.origin[1], world[2] - frame.origin[2])
    return (_dot(d, frame.x), _dot(d, frame.y), _dot(d, frame.z))


def to_local_normal(frame: HoudiniFrame, world: Vec) -> Vec:
    """Embedded (proxy-local) normal of a Houdini normal: the rotation only."""
    return (_dot(world, frame.x), _dot(world, frame.y), _dot(world, frame.z))

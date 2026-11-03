# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py (P3D_LOD_Resolution) and utilities/data.py (LOD, lod_info), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - one table (LOD_TYPES) holds the type ids, names, descriptions and signature rules; A3OB duplicated the enum;
# - signatures are decoded numerically on the float32 value, not by matching "%.3e" strings;
# - resolutions are floats (A3OB rounded them to int, Unknown LODs included); decode returns the shortest decimal
#   that encodes back to the same float32 signature;
# - negative signatures decode as Unknown (A3OB: Visual); encode rejects resolutions outside the type's range;
# - fixed the "Visual resolutiion" typo.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""LOD types and the LOD signature ("resolution") float of P3D files.

Each LOD ends with a float32 signature that encodes its type and, for some types, a resolution:

- Visual: ``resolution`` (0 <= signature < 1000)
- View - Cargo: ``1200 + resolution``; Shadow Volume: ``10000 + resolution``; Edit: ``20000 + resolution``
- View - Cargo Geometry: ``8e15 + resolution * 1e13``; Shadow Volume - Cargo View: ``1.8e16 + resolution * 1e13``
- every other known type: a fixed value (e.g. Geometry = 1e13, Memory = 1e15)
- anything else: Unknown, whose "resolution" is the raw signature.
"""

from __future__ import annotations

import enum
import math
import struct
from typing import NamedTuple, Optional, Tuple


class LodType(enum.IntEnum):
    """LOD types. The values match A3OB's LOD indices."""

    VISUAL = 0
    VIEW_GUNNER = 1
    VIEW_PILOT = 2
    VIEW_CARGO = 3
    SHADOW = 4
    EDIT = 5
    GEOMETRY = 6
    GEOMETRY_BUOY = 7
    GEOMETRY_PHYSX = 8
    MEMORY = 9
    LANDCONTACT = 10
    ROADWAY = 11
    PATHS = 12
    HITPOINTS = 13
    VIEW_GEOMETRY = 14
    FIRE_GEOMETRY = 15
    VIEW_CARGO_GEOMETRY = 16
    VIEW_CARGO_FIRE_GEOMETRY = 17
    VIEW_COMMANDER = 18
    VIEW_COMMANDER_GEOMETRY = 19
    VIEW_COMMANDER_FIRE_GEOMETRY = 20
    VIEW_PILOT_GEOMETRY = 21
    VIEW_PILOT_FIRE_GEOMETRY = 22
    VIEW_GUNNER_GEOMETRY = 23
    VIEW_GUNNER_FIRE_GEOMETRY = 24
    SUBPARTS = 25
    SHADOW_VIEW_CARGO = 26
    SHADOW_VIEW_PILOT = 27
    SHADOW_VIEW_GUNNER = 28
    WRECKAGE = 29
    UNDERGROUND = 30
    GROUNDLAYER = 31
    NAVIGATION = 32
    UNKNOWN = -1


class LodTypeInfo(NamedTuple):
    display_name: str
    description: str
    base: Optional[float]   # signature at resolution 0 (None: Unknown)
    scale: Optional[float]  # None: no resolution; else signature = base + resolution * scale
    limit: Optional[float]  # exclusive upper signature bound for types with a resolution


_T = LodType

#: The single LOD type table: names and descriptions from A3OB utilities/data.py, signatures from io/data_p3d.py.
LOD_TYPES = {
    _T.VISUAL: LodTypeInfo("Resolution", "Visual resolution", 0.0, 1.0, 1.0e3),
    _T.VIEW_GUNNER: LodTypeInfo("View - Gunner", "Gunner first person view", 1.0e3, None, None),
    _T.VIEW_PILOT: LodTypeInfo("View - Pilot", "First person view", 1.1e3, None, None),
    _T.VIEW_CARGO: LodTypeInfo("View - Cargo", "Passenger first person view", 1.2e3, 1.0, 1.3e3),
    _T.SHADOW: LodTypeInfo("Shadow Volume", "Shadow casting geometry", 1.0e4, 1.0, 1.2e4),
    _T.EDIT: LodTypeInfo("Edit", "Temporary layer", 2.0e4, 1.0, 3.0e4),
    _T.GEOMETRY: LodTypeInfo("Geometry", "Object collision geometry and occluders", 1.0e13, None, None),
    _T.GEOMETRY_BUOY: LodTypeInfo(
        "Geometry Buoyancy", "Buoyant object geometry (Displacement for VBS)", 2.0e13, None, None),
    _T.GEOMETRY_PHYSX: LodTypeInfo("Geometry PhysX", "PhysX object collision geometry", 4.0e13, None, None),
    _T.MEMORY: LodTypeInfo("Memory", "Hard points and animation axes", 1.0e15, None, None),
    _T.LANDCONTACT: LodTypeInfo("Land Contact", "Points of contact with ground", 2.0e15, None, None),
    _T.ROADWAY: LodTypeInfo("Roadway", "Walkable surfaces", 3.0e15, None, None),
    _T.PATHS: LodTypeInfo("Paths", "AI path finding mesh", 4.0e15, None, None),
    _T.HITPOINTS: LodTypeInfo("Hit-points", "Hit point cloud", 5.0e15, None, None),
    _T.VIEW_GEOMETRY: LodTypeInfo("View Geometry", "View occlusion for AI", 6.0e15, None, None),
    _T.FIRE_GEOMETRY: LodTypeInfo("Fire Geometry", "Hitbox geometry", 7.0e15, None, None),
    _T.VIEW_CARGO_GEOMETRY: LodTypeInfo(
        "View - Cargo Geometry", "Passenger view collision geometry and occluders", 8.0e15, 1.0e13, 9.0e15),
    _T.VIEW_CARGO_FIRE_GEOMETRY: LodTypeInfo(
        "View - Cargo Fire Geometry", "Passenger view hitbox geometry", 9.0e15, None, None),
    _T.VIEW_COMMANDER: LodTypeInfo("View - Commander", "Commander first person view", 1.0e16, None, None),
    _T.VIEW_COMMANDER_GEOMETRY: LodTypeInfo(
        "View - Commander Geometry", "Commander view collision geometry and occluders", 1.1e16, None, None),
    _T.VIEW_COMMANDER_FIRE_GEOMETRY: LodTypeInfo(
        "View - Commander Fire Geometry", "Commander hitbox geometry", 1.2e16, None, None),
    _T.VIEW_PILOT_GEOMETRY: LodTypeInfo(
        "View - Pilot Geometry", "First person collision geometry and occluders", 1.3e16, None, None),
    _T.VIEW_PILOT_FIRE_GEOMETRY: LodTypeInfo(
        "View - Pilot Fire Geometry", "First person hitbox geometry", 1.4e16, None, None),
    _T.VIEW_GUNNER_GEOMETRY: LodTypeInfo(
        "View - Gunner Geometry", "Gunner collision geometry and occluders", 1.5e16, None, None),
    _T.VIEW_GUNNER_FIRE_GEOMETRY: LodTypeInfo(
        "View - Gunner Fire Geometry", "Gunner view hitbox geometry", 1.6e16, None, None),
    _T.SUBPARTS: LodTypeInfo("Sub Parts", "Obsolete", 1.7e16, None, None),
    _T.SHADOW_VIEW_CARGO: LodTypeInfo(
        "Shadow Volume - Cargo View", "Passenger view shadow casting geometry", 1.8e16, 1.0e13, 1.9e16),
    _T.SHADOW_VIEW_PILOT: LodTypeInfo(
        "Shadow Volume - Pilot View", "First person view shadow casting geometry", 1.9e16, None, None),
    _T.SHADOW_VIEW_GUNNER: LodTypeInfo(
        "Shadow Volume - Gunner View", "Gunner view shadow casting geometry", 2.0e16, None, None),
    _T.WRECKAGE: LodTypeInfo("Wreckage", "Vehicle wreckage", 2.1e16, None, None),
    _T.UNDERGROUND: LodTypeInfo(
        "Underground (VBS)",
        "Underground volume for VBS (not supported in Arma 3 -> Geometry PhysX Old)", 3.0e13, None, None),
    _T.GROUNDLAYER: LodTypeInfo(
        "Groundlayer (VBS)",
        "Ground deformation data for VBS (not supported in Arma 3 -> Shadow Volume 3000)", 1.3e4, None, None),
    _T.NAVIGATION: LodTypeInfo(
        "Navigation (VBS)",
        "Navigation data for VBS (not supported in Arma 3 -> Geometry / Resolution 5e+013)", 5.0e13, None, None),
    _T.UNKNOWN: LodTypeInfo("Unknown", "Unknown model layer", None, None, None),
}

del _T

#: Relative tolerance for matching a fixed signature (float32 itself is accurate to about 6e-8).
FIXED_SIGNATURE_TOLERANCE = 1e-6

_F32 = struct.Struct("<f")


def f32(value: float) -> float:
    """Round a float to the nearest float32 value. Raises ``ValueError`` if it is out of float32 range."""
    try:
        return _F32.unpack(_F32.pack(value))[0]
    except (OverflowError, struct.error) as ex:
        raise ValueError("%r does not fit a float32: %s" % (value, ex)) from None


_FIXED = [(t, info) for t, info in LOD_TYPES.items() if info.base is not None and info.scale is None]
_RANGED = [(t, info) for t, info in LOD_TYPES.items() if info.scale is not None]


def has_resolution(lod_type) -> bool:
    """True for the types whose signature carries a resolution (and for Unknown, whose resolution is the signature)."""
    info = LOD_TYPES[LodType(lod_type)]
    return info.scale is not None or info.base is None


def _shortest(value: float, encodes_back) -> float:
    """The shortest decimal (by ``repr`` length, then by distance to ``value``) for which ``encodes_back`` holds.

    Candidates are roundings to 0-11 decimal places (so 1e-05 off a large base becomes 0.0) and to 1-17 significant
    digits (so small values shorten too: 1.234e-09, not 1.2340000132837758e-09).
    """
    candidates = {round(value, places) + 0.0 for places in range(12)}  # + 0.0 turns -0.0 into 0.0
    candidates.update(float("%.*g" % (digits, value)) + 0.0 for digits in range(1, 18))
    valid = [candidate for candidate in candidates if encodes_back(candidate)]
    if not valid:
        return value + 0.0
    return min(valid, key=lambda candidate: (len(repr(candidate)), abs(candidate - value)))


def decode_signature(signature: float) -> Tuple[LodType, float]:
    """Return ``(lod_type, resolution)`` for a LOD signature.

    The resolution is 0.0 for types without one. For Unknown LODs it is the signature itself. Resolutions are the
    shortest decimal that encodes back to the same float32 signature (e.g. 0.3, not 0.30000001192092896).
    """
    sig = float(signature)
    if not math.isfinite(sig):
        return LodType.UNKNOWN, sig
    target = f32(sig)

    for lod_type, info in _FIXED:
        if abs(sig - info.base) <= info.base * FIXED_SIGNATURE_TOLERANCE:
            return lod_type, 0.0

    for lod_type, info in _RANGED:
        lower = min(info.base, f32(info.base))
        if lower <= sig < info.limit:
            base, scale = info.base, info.scale
            raw = (sig - base) / scale
            return lod_type, _shortest(raw, lambda c: _f32_or_none(base + c * scale) == target)

    return LodType.UNKNOWN, _shortest(sig, lambda c: _f32_or_none(c) == target)


def _f32_or_none(value: float):
    try:
        return f32(value)
    except ValueError:
        return None


def encode_signature(lod_type, resolution: float = 0.0) -> float:
    """Return the float32 signature (as a Python float) for a LOD type and resolution.

    Raises ``ValueError`` if the type has no resolution and ``resolution`` is not 0, if the resolution falls outside
    the type's signature range, or if an Unknown signature collides with a known type.
    """
    lod_type = LodType(lod_type)
    info = LOD_TYPES[lod_type]
    resolution = float(resolution)

    if info.base is None:  # Unknown: the resolution is the signature
        signature = f32(resolution)
        decoded = decode_signature(signature)[0]
        if decoded != LodType.UNKNOWN:
            raise ValueError("signature %r is the signature of %s, not an Unknown LOD" % (signature, decoded.name))
        return signature

    if info.scale is None:
        if resolution != 0.0:
            raise ValueError("LOD type %s has no resolution (got %r)" % (lod_type.name, resolution))
        return f32(info.base)

    signature = f32(info.base + resolution * info.scale)
    decoded = decode_signature(signature)[0]
    if decoded != lod_type:
        raise ValueError(
            "resolution %r is out of range for LOD type %s (signature %r decodes as %s)"
            % (resolution, lod_type.name, signature, decoded.name)
        )
    return signature


def format_resolution(resolution: float) -> str:
    """Format a resolution without a trailing ``.0`` for whole numbers."""
    if float(resolution).is_integer():
        return "%d" % resolution
    return repr(float(resolution))


def lod_display_name(lod_type) -> str:
    return LOD_TYPES.get(LodType(lod_type), LOD_TYPES[LodType.UNKNOWN]).display_name


def format_lod_name(lod_type, resolution: float = 0.0) -> str:
    """Human-readable LOD name, e.g. ``"Resolution 1"``, ``"Geometry"``, ``"Unknown 123.5"``."""
    lod_type = LodType(lod_type)
    name = lod_display_name(lod_type)
    if has_resolution(lod_type):
        return "%s %s" % (name, format_resolution(resolution))
    return name

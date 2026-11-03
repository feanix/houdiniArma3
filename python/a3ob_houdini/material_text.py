# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB props/material.py (``to_p3d``) and utilities/data.py (``enum_texture_types``), v2.5.1 (9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original: plain functions instead of a Blender property group; paths only get their slashes
# turned into backslashes (D14 Q-6), no project-root handling.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""The text rules of the Material Assign node, without ``hou`` (ux-tools.md section 5.6, spec 5 "Paths" and 12)."""

from typing import Sequence

__all__ = ["COLOR_TYPES", "KNOWN_EXTENSIONS", "normalise_path", "color_texture", "has_known_extension"]

#: A3OB's procedural texture types (``utilities/data.py`` ``enum_texture_types``): ``(token, description)``.
COLOR_TYPES = (
    ("CO", "Color Value"),
    ("CA", "Texture with Alpha"),
    ("LCO", "Terrain Texture Layer Color"),
    ("SKY", "Sky texture"),
    ("NO", "Normal Map"),
    ("NS", "Normal map specular with Alpha"),
    ("NOF", "Normal map faded"),
    ("NON", "Normal map noise"),
    ("NOHQ", "Normal map High Quality"),
    ("NOPX", "Normal Map with paralax"),
    ("NOVHQ", "two-part DXT5 compression"),
    ("DT", "Detail Texture"),
    ("CDT", "Colored detail texture"),
    ("MCO", "Multiply color"),
    ("DTSMDI", "Detail SMDI map"),
    ("MC", "Macro Texture"),
    ("AS", "Ambient Shadow texture"),
    ("ADS", "Ambient Shadow in Blue"),
    ("PR", "Ambient shadow from directions"),
    ("SM", "Specular Map"),
    ("SMDI", "Specular Map, optimized"),
    ("MASK", "Mask for multimaterial"),
    ("TI", "Thermal imaging map"),
)

#: Endings that a texture or material path normally has. Anything else only gets a warning.
KNOWN_EXTENSIONS = (".paa", ".tga", ".png", ".rvmat")


def normalise_path(path: str) -> str:
    """Every ``/`` becomes ``\\`` (D14 Q-6). Nothing else changes: no case, no root, no ending."""
    return path.replace("/", "\\")


def color_texture(color: Sequence[float], color_type: str) -> str:
    """A3OB's procedural colour texture: ``#(argb,8,8,3)color(r,g,b,a,TYPE)`` with three decimals."""
    return "#(argb,8,8,3)color(%.3f,%.3f,%.3f,%.3f,%s)" % (color[0], color[1], color[2], color[3], color_type)


def has_known_extension(path: str) -> bool:
    return path.lower().endswith(KNOWN_EXTENSIONS)

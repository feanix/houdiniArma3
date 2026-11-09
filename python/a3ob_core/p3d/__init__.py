# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py, v2.5.1 (9f2b8bc): this package splits that module into model, reader, writer,
# lod_types and proxy. A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""P3D MLOD (v257, P3DM 28.256): lossless reader/writer plus a semantic API.

    from a3ob_core import p3d
    model = p3d.read_p3d_file("box.p3d")
    lod = model.lods[0]
    lod.lod_type, lod.resolution, lod.properties(), lod.selection("camo1").weights()
    data = p3d.write_p3d(model)   # byte-identical to the input if nothing was edited
"""

from .lod_types import (
    LOD_TYPES, LodType, LodTypeInfo, decode_signature, encode_signature, f32, format_lod_name, has_resolution,
    signature_clashes, signature_key,
)
from .model import (
    ANIMATION, EDITOR_MASKS, END_OF_FILE, HIDDEN, LOCK, LOD, MASS, MAX_FACE_SIDES, PROPERTY, SELECTED, SHARP_EDGES,
    UVSET, AnimationTagg, Face, MaskTagg, MassTagg, P3DEOFError, P3DError, P3DModel, PropertyTagg, ProxyRef, RawTagg,
    SelectionTagg, SharpEdgesTagg, Tagg, UVSetTagg, decode_weight, encode_weight, is_count_dependent_tagg_name,
    is_selection_name, is_typed_tagg_name, parse_tagg, tagg_payload_problem,
)
from .proxy import ProxyFrame, format_proxy_name, parse_proxy_name, proxy_frame, proxy_triangle
from .reader import read_p3d, read_p3d_file
from .writer import write_p3d, write_p3d_file

__all__ = [
    "LOD_TYPES", "LodType", "LodTypeInfo", "decode_signature", "encode_signature", "f32", "format_lod_name",
    "has_resolution",
    "ANIMATION", "EDITOR_MASKS", "END_OF_FILE", "HIDDEN", "LOCK", "LOD", "MASS", "MAX_FACE_SIDES", "PROPERTY",
    "SELECTED", "SHARP_EDGES", "UVSET", "AnimationTagg", "Face", "MaskTagg", "MassTagg", "P3DEOFError", "P3DError",
    "P3DModel", "PropertyTagg", "ProxyRef", "RawTagg", "SelectionTagg", "SharpEdgesTagg", "Tagg", "UVSetTagg",
    "decode_weight", "encode_weight", "is_count_dependent_tagg_name", "is_selection_name", "is_typed_tagg_name",
    "parse_tagg", "tagg_payload_problem",
    "ProxyFrame", "format_proxy_name", "parse_proxy_name", "proxy_frame", "proxy_triangle",
    "read_p3d", "read_p3d_file", "write_p3d", "write_p3d_file",
]

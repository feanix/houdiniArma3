# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Names and strings at the Houdini boundary (spec sections 5 and 7.3, D-STR).

Strings: M1 (``a3ob_core``) strings are always valid Unicode, so they go into Houdini unchanged. When a string comes
back from Houdini for export, ``check_encodable`` says whether M1 can write it. A lone surrogate crashes Houdini, so
``hou_string`` refuses it before it gets there.

Group names: Houdini group and attribute names must look like identifiers. ``encode_group_name`` and
``decode_group_name`` wrap Houdini's own reversible ``hou.text.encodeAttrib`` / ``decodeAttrib``. Only the two wrappers
import ``hou``; the other helpers work under plain Python.
"""

import hashlib
import struct
from typing import Optional, Tuple

from a3ob_core import binary

__all__ = [
    "POINT_WEIGHT_PREFIX", "PRIM_WEIGHT_PREFIX", "encode_group_name", "decode_group_name", "point_weight_attrib",
    "prim_weight_attrib", "split_weight_attrib", "hou_string", "check_encodable", "lod_uid", "to_int32", "from_int32",
    "float_bits", "bits_float",
]

POINT_WEIGHT_PREFIX = "a3ob_w_"
PRIM_WEIGHT_PREFIX = "a3ob_fw_"


def encode_group_name(name: str) -> str:
    """The Houdini group name for an A3OB selection name. Valid names pass unchanged (spec 7.3)."""
    import hou
    return hou.text.encodeAttrib(name)


def decode_group_name(encoded: str) -> str:
    """The A3OB selection name for a Houdini group name."""
    import hou
    return hou.text.decodeAttrib(encoded)


def point_weight_attrib(encoded_group: str) -> str:
    """Name of the point weight attribute of an (already encoded) group."""
    return POINT_WEIGHT_PREFIX + encoded_group


def prim_weight_attrib(encoded_group: str) -> str:
    """Name of the primitive weight attribute of an (already encoded) group."""
    return PRIM_WEIGHT_PREFIX + encoded_group


def split_weight_attrib(attrib: str) -> Optional[Tuple[str, str]]:
    """``("point" | "prim", encoded group)`` for a weight attribute name, or None if it is not one."""
    if attrib.startswith(POINT_WEIGHT_PREFIX):
        return "point", attrib[len(POINT_WEIGHT_PREFIX):]
    if attrib.startswith(PRIM_WEIGHT_PREFIX):
        return "prim", attrib[len(PRIM_WEIGHT_PREFIX):]
    return None


def hou_string(text: str) -> str:
    """Return ``text`` if Houdini can hold it, else raise ``ValueError``.

    Houdini crashes on lone surrogates and cannot hold NUL characters. M1 never produces either."""
    if not isinstance(text, str):
        raise ValueError("expected text, got %r" % type(text).__name__)
    if "\x00" in text:
        raise ValueError("text %r contains a NUL character" % text)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("text %r contains a lone surrogate" % text) from None
    return text


def check_encodable(text: str) -> Optional[str]:
    """None if M1 can write ``text`` to a P3D file, else the reason (the spec's validator error, section 5)."""
    try:
        binary.encode_string(text)
    except ValueError as ex:
        return str(ex)
    return None


def lod_uid(source_key: str, lod_index: int) -> int:
    """The stable id of an imported LOD (spec 3.5): 1 .. 2**31-1, never 0.

    ``source_key`` is the importer node's path, so a recook gives the same id and another import node gives a
    different one."""
    digest = hashlib.blake2b(("%s|%d" % (source_key, lod_index)).encode("utf-8"), digest_size=8).digest()
    return 1 + int.from_bytes(digest, "little") % (2 ** 31 - 1)


def to_int32(value: int) -> int:
    """A u32 as the two's-complement int32 that Houdini stores (spec 5, "Value rules")."""
    return value - 2 ** 32 if value >= 2 ** 31 else value


def from_int32(value: int) -> int:
    """The u32 behind a Houdini int32."""
    return value & 0xFFFFFFFF


def float_bits(value: float) -> int:
    """The raw float32 bits of ``value`` as an int32 (``a3ob_lod_signature``, spec 3.4)."""
    return struct.unpack("<i", struct.pack("<f", value))[0]


def bits_float(bits: int) -> float:
    """The float32 whose raw bits are the int32 ``bits``."""
    return struct.unpack("<f", struct.pack("<i", bits))[0]

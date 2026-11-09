# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The decisions of the LOD Properties node, without ``hou`` (ux-tools.md section 5.4, spec 3.3, 3.4 and 11.1).

The node module reads the geometry and writes the attributes. This module decides what to write, so the rules can be
tested under plain Python. Signature maths always goes through ``a3ob_core`` ``encode_signature`` and
``decode_signature`` (spec 3.4); nothing here re-implements it.
"""

import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from a3ob_core import binary
from a3ob_core.p3d import lod_types

from . import names
from .errors import UserError
from .messages import format_problem

__all__ = [
    "NODE_NAME", "MAX_PROPERTY_CHARS", "TYPE_ITEMS", "type_from_token", "type_token", "short_f32", "view_resolution",
    "encode_view", "range_text", "signature_after_edit", "Entry", "check_text", "merge_properties",
    "merge_copies", "duplicate_names",
]

NODE_NAME = "LOD Properties"

#: A named property key or value holds at most 63 characters (64-byte field with the NUL; spec 11.1).
MAX_PROPERTY_CHARS = 63

#: ``((token, label), ...)`` for the LOD type menu: every type of ``LOD_TYPES``, by display name, Unknown last.
TYPE_ITEMS: Tuple[Tuple[str, str], ...] = tuple(
    (lod_type.name.lower(), info.display_name) for lod_type, info in lod_types.LOD_TYPES.items())


def type_from_token(token: str) -> lod_types.LodType:
    """The ``LodType`` of a menu token. Raises ``UserError`` for a token that is not in the menu."""
    for lod_type in lod_types.LOD_TYPES:
        if lod_type.name.lower() == token:
            return lod_type
    raise UserError(format_problem(NODE_NAME, "%r is not a LOD type" % token, "Pick a type from the LOD Type menu"))


def type_token(lod_type) -> str:
    return lod_types.LodType(lod_type).name.lower()


def _f32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def short_f32(value: float) -> float:
    """The shortest decimal that is the same float32 as ``value`` (0.3, not 0.30000001192092896)."""
    target = _f32_bits(value)
    for digits in range(1, 10):
        candidate = float("%.*g" % (digits, value))
        if _f32_bits(candidate) == target:
            return candidate
    return value


def view_resolution(lod_type, resolution: float) -> float:
    """The resolution that goes on the LOD: 0.0 for the types that have none (like the importer writes it)."""
    return float(resolution) if lod_types.has_resolution(lod_type) else 0.0


def range_text(lod_type) -> str:
    """The resolutions a type accepts, for an error message."""
    info = lod_types.LOD_TYPES[lod_types.LodType(lod_type)]
    if info.scale is not None:
        top = lod_types.format_resolution((info.limit - info.base) / info.scale)
        return "0 up to (not including) %s" % top
    if info.base is None:
        return "any signature value that is not the signature of a known type"
    return "none: this type has no resolution"


def encode_view(lod_type, resolution: float, what: str = "") -> float:
    """``encode_signature`` with the failure turned into a node error that names the type and the allowed range."""
    resolution = view_resolution(lod_type, resolution)
    try:
        return lod_types.encode_signature(lod_type, resolution)
    except ValueError as ex:
        info = lod_types.LOD_TYPES[lod_types.LodType(lod_type)]
        raise UserError(format_problem(
            NODE_NAME, "%s%s cannot take the resolution %s (%s)" % (
                what, info.display_name, lod_types.format_resolution(resolution), ex),
            "Allowed for this type: %s" % range_text(lod_type))) from None


def signature_after_edit(stored_bits: Optional[int], lod_type, resolution: float) -> Tuple[Optional[int], bool]:
    """What ``a3ob_lod_signature`` holds after the type and resolution were set (spec 3.4).

    Returns ``(bits, changed)``. ``bits`` is None when the LOD has no stored signature (it stays absent; the export
    encodes it). When the stored bits still decode to the new type and resolution they are kept (``changed`` is
    False), so an unedited LOD keeps its exact bits. Otherwise they are replaced by ``encode_signature``."""
    resolution = view_resolution(lod_type, resolution)
    new_bits = names.float_bits(encode_view(lod_type, resolution))
    if stored_bits is None:
        return None, False
    stored_type, stored_resolution = lod_types.decode_signature(names.bits_float(stored_bits))
    if stored_type == lod_types.LodType(lod_type) and _f32_bits(lod_types.f32(stored_resolution)) == _f32_bits(
            lod_types.f32(resolution)):
        return stored_bits, False
    return new_bits, True


@dataclass
class Entry:
    """One named property. ``raw`` is the base64 payload kept from the import (spec 11.1), or ``""``."""

    name: str
    value: str
    raw: str = ""


def check_text(text: str, what: str) -> None:
    """Raise ``UserError`` when a property text is too long or has a character that cp1252 cannot write."""
    problem = names.check_encodable(text)
    if problem:
        raise UserError(format_problem(NODE_NAME, "%s has a character that a P3D file cannot hold (%s)" % (what, problem),
                                       "Use only characters of the Windows 1252 code page"))
    if len(binary.encode_string(text)) > MAX_PROPERTY_CHARS:
        raise UserError(format_problem(
            NODE_NAME, "%s is longer than %d characters" % (what, MAX_PROPERTY_CHARS),
            "A P3D named property holds at most %d characters in each of its two fields" % MAX_PROPERTY_CHARS))


def merge_properties(existing: Sequence[Entry], rows: Sequence[Tuple[str, str]], replace_all: bool,
                     report=None) -> List[Entry]:
    """The property list after the rows are applied (ux-tools.md 5.4).

    ``rows`` are ``(name, value)`` from the multiparm. A row with no name and no value is skipped with a warning.
    Add mode: a row replaces the input's entry that has the same name (any case); otherwise it is appended. Replace
    mode: only the rows remain. A row equal to the input's entry (same name and value)
    keeps its raw payload; a changed name or value drops it, and untouched entries keep theirs."""
    base = [] if replace_all else [Entry(entry.name, entry.value, entry.raw) for entry in existing]
    fixed = len(base)
    for number, (name, value) in enumerate(rows, start=1):
        if not name and not value:
            if report is not None:
                report.warning(format_problem(NODE_NAME, "property row %d is empty and was skipped" % number))
            continue
        check_text(name, "the name of property row %d" % number)
        check_text(value, "the value of property row %d" % number)
        for index in range(fixed):
            if base[index].name.casefold() == name.casefold():
                same = base[index].name == name and base[index].value == value
                base[index] = Entry(name, value, base[index].raw if same else "")
                break
        else:
            base.append(Entry(name, value))
    return base


def duplicate_names(rows: Sequence[Tuple[str, str]]) -> List[str]:
    """Names that appear in more than one row (any case), once each, in row order."""
    seen, repeated = set(), []
    for name, value in rows:
        key = name.casefold()
        if (name or value) and key in seen and name not in repeated:
            repeated.append(name)
        seen.add(key)
    return repeated


def merge_copies(existing: Sequence[Tuple[int, float]], rows: Sequence[Tuple[int, float]],
                 replace_all: bool) -> List[Tuple[int, float]]:
    """The copy list ``[(type, resolution)]`` after the rows: appended in Add mode, alone in Replace mode.
    Each copy's resolution is cleaned like the LOD's own (0.0 for types that have none)."""
    base = [] if replace_all else list(existing)
    for lod_type, resolution in rows:
        base.append((int(lod_type), view_resolution(lod_type, resolution)))
    return base

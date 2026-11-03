# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): LOD copies, duplicate signatures and LOD sorting follow
# ``process_lod`` and ``export_p3d`` (lines 377-387, 614-622, 716). A3OB: MrClock (present add-on), Hans-Joerg
# "Alwarren" Frieden (original ArmaToolbox).
"""Spec 16 steps 3 to 6 for a whole file: build every LOD, expand copies, check duplicates, sort, make the model.
No ``hou`` here.
"""

import copy
import math
import struct
from typing import List, Optional, Tuple

from a3ob_core import p3d
from a3ob_core.p3d import LOD, P3DError, P3DModel

from .. import messages
from .data import Collection
from .lod_build import build_lod
from .options import ExportOptions, keeps_uvs
from .path_map import PathMapper
from .renames import strip_uvs

__all__ = ["build_model", "to_bytes", "signature_key"]


def signature_key(lod: LOD) -> bytes:
    """The raw bits of the signature: two LODs are duplicates only if these are equal."""
    return struct.pack("<f", lod.resolution_signature)


def build_model(collection: Collection, options: ExportOptions, report: messages.Report) -> Optional[P3DModel]:
    """The ``P3DModel`` for the collected LODs, or None when ``report`` got errors (spec 16)."""
    if not collection.lods:
        report.error(messages.format_problem(None, "there is no LOD to export", "Connect a P3D geometry or a LOD geometry"))
        return None
    mapper = PathMapper(options.project_root, options.relative_paths)  # one per export: caches per folder
    entries: List[Tuple[str, LOD]] = []
    dropped: List[str] = []
    for data in collection.lods:
        lod = build_lod(data, options, report, mapper)
        if lod is None:
            continue
        copies = _copies(data, lod, report)
        entries.append((data.label, lod))
        entries.extend((label, duplicate) for label, duplicate, _ in copies)
        if options.drop_uvs:
            # Copies were made first, so each one is judged by the type it is written with (spec 18.4).
            if not keeps_uvs(data.details.lod_type) and strip_uvs(lod):
                dropped.append(data.label)
            for label, duplicate, lod_type in copies:
                if not keeps_uvs(lod_type) and strip_uvs(duplicate):
                    dropped.append(label)
    mapper.report_to(report)
    if report.has_errors:
        return None
    if dropped:
        report.info("Dropped the UVs of %d LODs (%s)" % (len(dropped), ", ".join(dropped)))
    entries = _handle_duplicates(entries, options, report)
    if report.has_errors:
        return None
    entries = _order(entries, options, report)
    model = P3DModel([lod for _, lod in entries])
    model.trailing = collection.trailing
    return model


def _copies(data, lod: LOD, report: messages.Report) -> List[Tuple[str, LOD, int]]:
    """LOD copies (spec 3.3): the same mesh again under extra signatures. They always use ``encode_signature``."""
    details = data.details
    types, resolutions = details.copy_types, details.copy_resolutions
    if len(types) != len(resolutions):
        report.warning(messages.format_problem(
            data.label, "a3ob_lod_copy_types has %d entries and a3ob_lod_copy_resolutions has %d" % (
                len(types), len(resolutions)), "Only the pairs that have both are written"))
    result = []
    for lod_type, resolution in zip(types, resolutions):
        try:
            signature = p3d.encode_signature(lod_type, resolution)
        except ValueError as ex:
            report.error(messages.format_problem(
                data.label, "the LOD copy with type %s and resolution %s cannot be written: %s" % (lod_type, resolution, ex),
                "Change a3ob_lod_copy_types or a3ob_lod_copy_resolutions"))
            continue
        duplicate = copy.deepcopy(lod)
        duplicate.resolution_signature = signature
        result.append((p3d.format_lod_name(lod_type, resolution), duplicate, int(lod_type)))
    return result


def _handle_duplicates(entries: List[Tuple[str, LOD]], options: ExportOptions,
                       report: messages.Report) -> List[Tuple[str, LOD]]:
    """Spec 3.2 "Duplicate signatures": an error by default; option skip or ignore (A3OB ``lod_collisions``)."""
    seen = set()
    kept: List[Tuple[str, LOD]] = []
    for label, lod in entries:
        key = signature_key(lod)
        if key in seen and options.duplicate_lods != "ignore":
            if options.duplicate_lods == "fail":
                report.error(messages.format_problem(
                    label, "another LOD has the same type and resolution",
                    "Change the resolution of one of them, or set Duplicate LODs to Skip or Ignore"))
            else:
                report.warning(messages.format_problem(label, "a LOD with the same type and resolution came first. This one is skipped"))
                continue
        seen.add(key)
        kept.append((label, lod))
    return kept


def _sort_key(lod: LOD) -> float:
    value = lod.resolution_signature
    return math.inf if value != value else value


def _order(entries: List[Tuple[str, LOD]], options: ExportOptions, report: messages.Report) -> List[Tuple[str, LOD]]:
    """Spec 3.2 "Order": keep the collected order; sorting by signature is an option, off by default."""
    keys = [_sort_key(lod) for _, lod in entries]
    if options.sort_lods:
        return sorted(entries, key=lambda entry: _sort_key(entry[1]))
    if keys != sorted(keys):
        report.warning(messages.format_problem(
            None, "the LODs are not sorted by resolution", "The file keeps your order. Turn on Sort LODs to sort them"))
    return entries


def to_bytes(model: P3DModel, report: messages.Report) -> Optional[bytes]:
    """The file bytes, or None with an error in ``report``."""
    try:
        return p3d.write_p3d(model)
    except P3DError as ex:
        report.error(messages.format_problem(None, "Internal error: the file could not be written (%s)" % ex,
                                             "Please report this"))
        return None

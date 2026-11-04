# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): ``process_lod`` is rewritten for Houdini data and the build
# order of the Houdini data model (docs/design/houdini-data-model.md section 16). A3OB: MrClock (present add-on),
# Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""Spec 16 step 3: build one ``a3ob_core`` LOD from a ``LodData``. No ``hou`` here.

Order (spec 11.2): a fresh LOD, its signature, ``set_mesh`` first (so ``dropped_taggs`` stays empty), then the TAGGs.
Afterwards ``structure_errors()`` is a hard stop and ``check()`` is advisory.
"""

import re
import struct
from collections import Counter
from typing import List, Optional

from a3ob_core import p3d
from a3ob_core.p3d import LOD, P3DError

from .. import layout_record, messages, names
from . import flag_check, type_rules
from .data import LodData
from .harden import harden_lod
from .mesh_build import FileMesh, build_file_mesh
from .options import ExportOptions
from .path_map import PathMapper
from .proxy_rebuild import FileProxy, rebuild_proxy
from .selections import GEOMETRY_TYPES, add_auto_components, collect_selections
from .renames import apply_name_options
from .taggs import make_items, order_item_list, output_text

__all__ = ["build_lod", "signature_for", "ADVISORY_PREEMPTED"]

#: M1 ``check()`` messages that the glue has already handled and reported itself (spec 16 step 4).
ADVISORY_PREEMPTED = (
    re.compile(r"#UVSet# id \d+ appears more than once"),
    re.compile(r"selection .* has the same name as .* up to case"),
    re.compile(r"face \d+ has \d+ sides"),
)


def _f32_bits(value: float) -> int:
    try:
        return struct.unpack("<I", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return struct.unpack("<I", struct.pack("<f", float("inf") if value > 0 else float("-inf")))[0]


def signature_for(details) -> float:
    """Spec 3.4: the stored bits while the type and resolution still describe them, else ``encode_signature``.

    Raises ``ValueError`` when the type or resolution cannot be encoded."""
    if details.signature is not None:
        signature = names.bits_float(details.signature)
        lod_type, resolution = p3d.decode_signature(signature)
        if int(lod_type) == details.lod_type and _f32_bits(resolution) == _f32_bits(details.resolution):
            return signature
    return p3d.encode_signature(details.lod_type, details.resolution)


def build_lod(data: LodData, options: ExportOptions, report: messages.Report,
              mapper: Optional[PathMapper] = None) -> Optional[LOD]:
    """The M1 LOD for ``data``, or None when errors were reported for it.

    ``mapper`` is the path mapper of the whole export (task #51); a lone call gets its own."""
    if mapper is None:
        mapper = PathMapper(options.project_root)
    label = data.label
    errors_before = len(report.texts(messages.ERROR))
    try:
        signature = signature_for(data.details)
    except ValueError as ex:
        report.error(messages.format_problem(
            label, "the LOD type %s with resolution %s cannot be written: %s" % (
                data.details.lod_type, data.details.resolution, ex), "Change a3ob_lod_type or a3ob_lod_resolution"))
        return None
    data = harden_lod(data, report)  # task #48: Shadow and Underground LODs are always written with hard edges
    proxies = _rebuild_proxies(data, label, report)
    file_mesh = build_file_mesh(data, proxies, options, report)
    _apply_path_options(file_mesh, options, mapper, label)
    _check_strings(file_mesh, label, report)
    lod = LOD()
    lod.resolution_signature = signature
    lod.flags = data.details.flags & 0xFFFFFFFF
    lod.eof_active = data.details.eof_active & 0xFF
    if not _set_mesh(lod, file_mesh, label, report):
        return None
    selections = collect_selections(data, file_mesh, proxies, options, report)
    if options.auto_components:
        add_auto_components(data, file_mesh, selections, report)
    items = make_items(data, file_mesh, selections, options, report, mapper)
    ordered = order_item_list(items, data.details.tagg_order, data.details.tagg_active)
    apply_name_options(ordered, options, label, report)
    lod.taggs = [item.tagg for item in ordered]
    _apply_layout(lod, data, label, report)
    _checks(lod, data, file_mesh, options, label, report)
    if len(report.texts(messages.ERROR)) > errors_before:
        return None
    return lod


def _apply_layout(lod: LOD, data: LodData, label: str, report: messages.Report) -> None:
    """Spec 19: write the file layout of the import back while the positions and topology are unchanged."""
    if not data.details.layout:
        return
    reason = layout_record.apply_record(lod, data.details.layout)
    if reason is not None:
        report.info(messages.format_problem(
            label, "the layout record does not match the mesh (%s), so the LOD is written in the layout of the export, "
            "not the layout of the source file" % reason))


def _rebuild_proxies(data: LodData, label: str, report: messages.Report) -> List[FileProxy]:
    result = []
    for proxy in data.proxies:
        rebuilt = rebuild_proxy(proxy, label, report)
        if rebuilt is not None:
            result.append(rebuilt)
    return result


def _apply_path_options(file_mesh: FileMesh, options: ExportOptions, mapper: PathMapper, label: str) -> None:
    """Texture and material paths: .paa ending (only with ``paa_textures``), relative paths, then lowercase (spec 5 "Paths")."""
    file_mesh.textures = [output_text(text, options, mapper, "texture", label) for text in file_mesh.textures]
    if options.relative_paths or options.lowercase:
        file_mesh.materials = [output_text(text, options, mapper, "material", label) for text in file_mesh.materials]


def _check_strings(file_mesh: FileMesh, label: str, report: messages.Report) -> None:
    """Spec 5 (D-STR): a character that ``encode_string`` cannot write is an error that names the attribute and prim."""
    for attrib, values in (("a3ob_texture", file_mesh.textures), ("a3ob_material", file_mesh.materials)):
        bad = [(face, names.check_encodable(text)) for face, text in enumerate(values) if names.check_encodable(text)]
        if bad:
            report.error(messages.format_problem(
                label, "%s of %d faces cannot be written (%s): %s" % (
                    attrib, len(bad), messages.list_elements("faces", [face for face, _ in bad]), bad[0][1]),
                "Use characters of Windows-1252 only"))


def _set_mesh(lod: LOD, file_mesh: FileMesh, label: str, report: messages.Report) -> bool:
    try:
        lod.set_mesh(file_mesh.points, file_mesh.polygons, file_mesh.normal_table, file_mesh.normal_indices,
                     file_mesh.record_uvs, file_mesh.point_flags, file_mesh.face_flags, file_mesh.textures,
                     file_mesh.materials)
    except ValueError as ex:
        report.error(messages.format_problem(label, "the mesh cannot be built: %s" % ex))
        return False
    return True


def _checks(lod: LOD, data: LodData, file_mesh: FileMesh, options: ExportOptions, label: str,
            report: messages.Report) -> None:
    """Spec 16 step 3.3 and 4, and the Houdini-side warnings of 17.1 that need the built counts."""
    if lod.dropped_taggs:
        report.error(messages.format_problem(
            label, "Internal error: %d TAGGs were dropped while building (%s)" % (
                len(lod.dropped_taggs), ", ".join(tagg.name for tagg in lod.dropped_taggs[:5])),
            "Please report this"))
    problems = lod.structure_errors()
    if problems:
        report.error(messages.format_problem(
            label, "Internal error: the built LOD is inconsistent (%s)" % "; ".join(problems[:5]), "Please report this"))
        return
    for text in lod.check():
        if not any(pattern.fullmatch(text) or pattern.match(text) for pattern in ADVISORY_PREEMPTED):
            report.warning(messages.format_problem(label, text))
    _warn_merged(lod, data, label, report)
    _warn_mass(data, label, report)
    flag_check.check_flags(data, report)
    type_rules.check_lod_rules(data, options, report)


def _warn_merged(lod: LOD, data: LodData, label: str, report: messages.Report) -> None:
    """Spec 17.1 (D15): the uid check, and the point and face count heuristic only for a LOD with no uid."""
    uids = Counter(poly.uid for poly in data.mesh.polys if poly.uid)
    if len(uids) > 1:
        shown = ", ".join("%d (%d polygons)" % (uid, count) for uid, count in list(uids.items())[:3])
        report.warning(messages.format_problem(
            label, "looks like two LODs were merged (uids %s)" % shown,
            "A Merge of unpacked LODs keeps only the first input's LOD data. Pack the LODs with Assemble LODs instead"))
    if uids:
        return  # D15: the uid check is exact, so the count heuristic (which an added proxy would trip) is not used
    counts = data.details.source_counts
    if counts is not None and lod.point_count > counts[0] and lod.face_count > counts[1]:
        report.warning(messages.format_problem(
            label, "has more points and faces than the imported LOD (%d/%d, it had %d/%d), which can mean two LODs were "
            "merged" % (lod.point_count, lod.face_count, counts[0], counts[1]),
            "A Merge of unpacked LODs keeps only the first input's LOD data"))


def _warn_mass(data: LodData, label: str, report: messages.Report) -> None:
    if data.mesh.masses is not None and data.details.lod_type not in GEOMETRY_TYPES:
        report.warning(messages.format_problem(label, "has a3ob_mass, but it is not a geometry-type LOD",
                                               "Only Geometry LODs use mass"))

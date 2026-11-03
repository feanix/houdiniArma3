# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): TAGGs are made from Houdini attributes and groups and ordered
# by a hint (spec 11.3); blobs and active bytes are kept (spec 11.2). A3OB: MrClock (present add-on), Hans-Joerg
# "Alwarren" Frieden (original ArmaToolbox).
"""Spec 16 step 3.2: the TAGGs of one LOD, in file order. No ``hou`` here.

``make_items`` builds every TAGG that the Houdini data holds, each with its order token (spec 11.3), in the canonical
order: sharp edges, UV sets, properties, mass, selections, blobs. ``order_items`` then puts the tokens named by
``a3ob_tagg_order`` first, in hint order, and the rest after them in canonical order.
"""

import base64
import binascii
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from a3ob_core import binary, p3d
from a3ob_core.p3d import P3DError

from .. import messages, names
from ..permutation import Permutation, remap_payload
from .data import LodData
from .mesh_build import FileMesh
from .options import ExportOptions, fold_collisions, keeps_uvs, make_relative, relative_proxy_name
from .selections import SelectionEntry

__all__ = ["Item", "make_items", "order_items", "order_item_list", "output_name", "output_text"]


@dataclass
class Item:
    """A TAGG with the token that names it in ``a3ob_tagg_order``."""

    token: str
    tagg: p3d.Tagg
    #: A blob keeps its own active byte; the order hint does not set it.
    blob: bool = False


def output_text(text: str, options: ExportOptions) -> str:
    """Texture and material paths: relative first, then lowercase (spec 5 "Paths")."""
    if options.relative_paths:
        text = make_relative(text, options.project_root)
    return text.lower() if options.lowercase else text


def output_name(name: str, options: ExportOptions) -> str:
    """A selection name as written: the path of a proxy name made relative, then lowercase."""
    if options.relative_paths:
        name = relative_proxy_name(name, options.project_root)
    return name.lower() if options.lowercase else name


def _decode(text: str, what: str, label: str, report: messages.Report) -> Optional[bytes]:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        report.error(messages.format_problem(label, "%s is not valid base64" % what, "Delete the attribute or reimport"))
        return None


def make_items(data: LodData, file_mesh: FileMesh, selections: Sequence[SelectionEntry], options: ExportOptions,
               report: messages.Report) -> List[Item]:
    """Every TAGG of the LOD in canonical order."""
    items: List[Item] = []
    counts = (len(file_mesh.point_flags), len(file_mesh.polygons), file_mesh.corner_count)
    items.extend(_sharp_items(data, file_mesh, counts, report))
    items.extend(_uv_items(data, file_mesh))
    items.extend(_property_items(data, options, report))
    items.extend(_mass_items(file_mesh))
    items.extend(_selection_items(data, selections, options, report))
    items.extend(_blob_items(data, file_mesh, counts, options, report))
    return items


def _counts_text(counts) -> str:
    return "%d points, %d faces, %d corners" % tuple(counts)


def _sharp_items(data: LodData, file_mesh: FileMesh, counts, report: messages.Report) -> List[Item]:
    """Spec 9.2: the edge group in group order, then the extra pairs while the counts still match."""
    mesh = data.mesh
    extras = data.details.sharp_extra
    if mesh.sharp is None:
        if extras:
            report.warning(messages.format_problem(
                data.label, "the sharp edge pairs in a3ob_sharp_extra are dropped because there is no a3ob_sharp edge group"))
        return []
    pairs: List[Tuple[int, int]] = []
    for a, b in mesh.sharp:
        if a in file_mesh.point_map and b in file_mesh.point_map:
            pairs.append((file_mesh.point_map[a], file_mesh.point_map[b]))
    if extras:
        if data.details.source_counts == counts:
            pairs.extend(zip(extras[0::2], extras[1::2]))
        else:
            report.warning(messages.format_problem(
                data.label, "%d sharp edge pairs that are not polygon edges are dropped (the mesh changed: %s, it was %s)" % (
                    len(extras) // 2, _counts_text(counts),
                    _counts_text(data.details.source_counts) if data.details.source_counts else "unknown")))
    return [Item(p3d.SHARP_EDGES, p3d.SharpEdgesTagg.from_pairs(pairs))]


def _uv_items(data: LodData, file_mesh: FileMesh) -> List[Item]:
    items = []
    for set_id in sorted(file_mesh.uv_sets):
        items.append(Item("%s%d" % (p3d.UVSET, set_id), p3d.UVSetTagg(set_id, file_mesh.uv_sets[set_id])))
    return items


def _property_items(data: LodData, options: ExportOptions, report: messages.Report) -> List[Item]:
    details = data.details
    label = data.label
    count = len(details.prop_names)
    if len(details.prop_values) != count:
        report.warning(messages.format_problem(
            label, "a3ob_prop_names has %d entries and a3ob_prop_values has %d" % (count, len(details.prop_values)),
            "Only the pairs that have both are written"))
        count = min(count, len(details.prop_values))
    items = []
    keys: List[str] = []
    for i in range(count):
        key, value = details.prop_names[i], details.prop_values[i]
        if options.lowercase:
            key, value = key.lower(), value.lower()
        keys.append(key)
        tagg = _property_tagg(details, i, key, value, label, report)
        if tagg is not None:
            items.append(Item(p3d.PROPERTY, tagg))
    if options.lowercase:
        for first, second in fold_collisions(details.prop_names[:count]):
            report.warning(messages.format_problem(
                label, "lowercase makes the property names %r and %r equal" % (first, second),
                "Both are still written"))
    return items


def _property_tagg(details, i: int, key: str, value: str, label: str, report: messages.Report) -> Optional[p3d.PropertyTagg]:
    """Spec 11.1: the raw payload while key and value are unchanged, else the canonical payload."""
    tagg = None
    raw_text = details.prop_raw[i] if i < len(details.prop_raw) else ""
    if raw_text:
        raw = _decode(raw_text, "a3ob_prop_raw[%d]" % i, label, report)
        if raw is not None:
            try:
                candidate = p3d.PropertyTagg.from_payload(raw)
            except P3DError:
                candidate = None
            if candidate is not None and candidate.key == key and candidate.value == value:
                tagg = candidate
    if tagg is None:
        tagg = p3d.PropertyTagg(key, value)
    try:
        tagg.payload()
    except (P3DError, ValueError) as ex:
        report.error(messages.format_problem(label, "the property %r cannot be written: %s" % (key, ex),
                                             "Names and values are at most 63 characters of Windows-1252"))
        return None
    return tagg


def _mass_items(file_mesh: FileMesh) -> List[Item]:
    if file_mesh.masses is None:
        return []
    return [Item(p3d.MASS, p3d.MassTagg(file_mesh.masses))]


def _selection_items(data: LodData, selections: Sequence[SelectionEntry], options: ExportOptions,
                     report: messages.Report) -> List[Item]:
    label = data.label
    items = []
    written: List[str] = []
    for entry in selections:
        out = output_name(entry.name, options)
        problem = names.check_encodable(out)
        if problem is not None:
            report.error(messages.format_problem(
                label, "the selection name %r cannot be written: %s" % (entry.name, problem),
                "Use characters of Windows-1252 only"))
            continue
        if not p3d.is_selection_name(out) or out == "":
            report.error(messages.format_problem(
                label, "the selection name %r cannot be written (empty, or it starts or ends with #)" % out,
                "Rename the group"))
            continue
        written.append(out)
        items.append(Item(entry.name, p3d.SelectionTagg(out, entry.vertex, entry.face)))
    _warn_name_collisions(label, [entry.name for entry in selections], written, options, report)
    return items


def _warn_name_collisions(label: str, original: List[str], written: List[str], options: ExportOptions,
                          report: messages.Report) -> None:
    """Names equal up to case: the glue pre-empts M1's ``check()`` message (spec 16, 17.1). With the lowercase option
    the warning is about the option (D14, Q-12); both names are still written."""
    if options.lowercase:
        for first, second in fold_collisions(original):
            report.warning(messages.format_problem(
                label, "lowercase makes the selection names %r and %r equal" % (first, second), "Both are still written"))
        return
    for first, second in fold_collisions(written):
        report.warning(messages.format_problem(
            label, "the selections %r and %r differ only in case. Object Builder and the game may treat them as one"
            % (first, second), "Rename one of them"))


def _blob_items(data: LodData, file_mesh: FileMesh, counts, options: ExportOptions,
                report: messages.Report) -> List[Item]:
    """Spec 11.2: opaque TAGGs, written unchanged while the counts equal ``a3ob_source_counts``.

    With Drop UVs on a type that has no UVs, a ``#UVSet#`` blob is UV data and is dropped without a word (spec 18.4)."""
    details = data.details
    label = data.label
    drop = options.drop_uvs and not keeps_uvs(details.lod_type)
    wanted = [i for i, name in enumerate(details.unknown_names) if not (drop and name == p3d.UVSET)]
    total = len(wanted)
    if total == 0:
        return []
    if details.source_counts != counts:
        shown = ", ".join(repr(details.unknown_names[i]) for i in wanted[:5])
        report.warning(messages.format_problem(
            label, "%d opaque TAGGs are dropped because the mesh changed (%s; %s)" % (
                total, shown, "now %s, it was %s" % (
                    _counts_text(counts), _counts_text(details.source_counts) if details.source_counts else "unknown")),
            "Data kept from the file only fits the mesh it came with"))
        return []
    perm = _sort_permutation(data, file_mesh)
    items = []
    for i in wanted:
        active = details.unknown_active[i] if i < len(details.unknown_active) else 1
        text = details.unknown_data[i] if i < len(details.unknown_data) else ""
        payload = _decode(text, "a3ob_tagg_unknown_data[%d]" % i, label, report)
        if payload is None:
            continue
        name = details.unknown_names[i]
        try:
            tagg = p3d.parse_tagg(name, active & 0xFF, payload, *counts)
        except P3DError:
            tagg = p3d.RawTagg(name, payload, active & 0xFF, counts=tuple(details.source_counts))
        if perm is not None:
            tagg = _follow_sorted_faces(tagg, perm, counts, i, data.label, report)
            if tagg is None:
                continue
        items.append(Item("#?%d" % i, tagg, blob=True))
    return items


def _sort_permutation(data: LodData, file_mesh: FileMesh) -> Optional[Permutation]:
    """The face move of the Sort sections option (spec 13.6): the mesh faces are written in ``file_mesh.mesh_faces``
    order, then the proxies. None when the faces stay in place. Points never move here (the importer put them in the
    export order)."""
    polys = data.mesh.polys
    faces = file_mesh.mesh_faces
    if len(faces) != len(polys) or faces == list(range(len(faces))):
        return None
    proxies = len(file_mesh.proxy_faces)
    sides = [len(poly.points) for poly in polys] + [3] * proxies
    return Permutation(range(len(file_mesh.point_flags)), list(faces) + [len(polys) + k for k in range(proxies)], sides)


def _follow_sorted_faces(tagg: p3d.Tagg, perm: Permutation, counts, number: int, label: str,
                         report: messages.Report) -> Optional[p3d.Tagg]:
    """A blob with face or corner data, rewritten for the sorted face order, or None (dropped with a warning)."""
    payload = remap_payload(tagg, perm)
    if payload is None:
        report.warning(messages.format_problem(
            label, "the opaque TAGG %r (record %d) is dropped because Sort sections moved the faces and its face "
            "numbers cannot be moved with them" % (tagg.name, number)))
        return None
    if isinstance(tagg, p3d.RawTagg):
        tagg.data = payload
        return tagg
    return p3d.parse_tagg(tagg.name, tagg.active, payload, *counts)


def order_items(items: List[Item], hint: Sequence[str], hint_active: Sequence[int]) -> List[p3d.Tagg]:
    """Spec 11.3: tokens named by the hint first, in hint order (each with its raw active byte), then the rest."""
    return [item.tagg for item in order_item_list(items, hint, hint_active)]


def order_item_list(items: List[Item], hint: Sequence[str], hint_active: Sequence[int]) -> List[Item]:
    """``order_items`` that keeps the items, so the D17 renames can find the live selections (spec 18.1)."""
    waiting: Dict[str, List[Item]] = defaultdict(list)
    for item in items:
        waiting[item.token].append(item)
    used = set()
    ordered: List[Item] = []
    for position, token in enumerate(hint):
        queue = waiting.get(token)
        if not queue:
            continue
        item = queue.pop(0)
        if not item.blob and position < len(hint_active):
            item.tagg.active = hint_active[position] & 0xFF
        used.add(id(item))
        ordered.append(item)
    ordered.extend(item for item in items if id(item) not in used)
    return ordered

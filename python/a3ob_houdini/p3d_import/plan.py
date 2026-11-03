# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Decide what one LOD becomes in Houdini (spec sections 6 to 13), without touching ``hou``.

``make_plan`` reads an ``a3ob_core`` LOD and answers the questions the builder needs:

- which TAGGs become groups, attributes or opaque blobs (spec 7.3, 8, 11.2);
- which selections are packed proxies and which stay inline triangles (spec 13.6);
- which points and faces stay in the mesh (proxy triangles move into packed prims);
- the order hint of the TAGGs (spec 11.3).

Keeping this apart from the builder means the rules can be tested under plain Python, and the builder only has to
write what the plan says.
"""

from array import array
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from a3ob_core import p3d
from a3ob_core.p3d import LOD, SelectionTagg
from a3ob_core.p3d import model as m1

from .. import messages
from ..permutation import Permutation, remap_payload

__all__ = [
    "BlobPlan", "GroupPlan", "ProxyPlan", "LodPlan", "make_plan", "houdini_corner_order", "uv_attrib_name",
    "SHARP_TOKEN", "MASS_TOKEN", "PROPERTY_TOKEN", "UVSET_TOKEN",
]

SHARP_TOKEN = m1.SHARP_EDGES
MASS_TOKEN = m1.MASS
PROPERTY_TOKEN = m1.PROPERTY
UVSET_TOKEN = m1.UVSET


def houdini_corner_order(sides: int) -> List[int]:
    """Spec 4.3: Houdini vertex ``k`` is file corner ``(-k) mod n``. The map is its own inverse."""
    return [(-k) % sides for k in range(sides)]


def uv_attrib_name(set_id: int) -> str:
    """Spec 8: id 0 is ``uv``, id k is ``uv<k+1>``."""
    return "uv" if set_id == 0 else "uv%d" % (set_id + 1)


@dataclass
class BlobPlan:
    """A TAGG stored as three parallel detail entries (spec 11.2)."""

    name: str
    active: int
    payload: bytes
    reason: str
    #: The TAGG as read, so that ``_remap_blobs`` can find out what its bytes mean.
    tagg: Optional[m1.Tagg] = None


@dataclass
class GroupPlan:
    """A selection that becomes a point group (and maybe weights and a prim group)."""

    name: str
    tagg: SelectionTagg
    #: Inline proxy: prim attributes ``a3ob_proxy_path`` and ``a3ob_proxy_index`` go on the member faces.
    inline_proxy: Optional[Tuple[str, int]] = None


@dataclass
class ProxyPlan:
    """A proxy selection that becomes a packed prim (spec 13.3)."""

    name: str
    path: str
    index: int
    face: int
    #: The face's three point indices in file corner order.
    corners: Tuple[int, int, int]
    selection: SelectionTagg

    @property
    def points(self) -> List[int]:
        """The three points in ascending file index: the order of the embedded points."""
        return sorted(self.corners)


@dataclass
class LodPlan:
    """What one LOD becomes."""

    lod: LOD
    lod_index: int
    lod_name: str
    #: File point and face indices that stay in the LOD geometry, ascending.
    mesh_points: List[int] = field(default_factory=list)
    mesh_faces: List[int] = field(default_factory=list)
    groups: List[GroupPlan] = field(default_factory=list)
    proxies: List[ProxyPlan] = field(default_factory=list)
    blobs: List[BlobPlan] = field(default_factory=list)
    #: Live #UVSet# TAGGs by id (the first of each id).
    uv_sets: Dict[int, p3d.UVSetTagg] = field(default_factory=dict)
    sharp: Optional[p3d.SharpEdgesTagg] = None
    mass: Optional[p3d.MassTagg] = None
    properties: List[p3d.PropertyTagg] = field(default_factory=list)
    #: Spec 8: face-record UVs that differ from set 0 (or set 0 is missing and they are not all zero).
    needs_face_uv: bool = False
    order: List[str] = field(default_factory=list)
    order_active: List[int] = field(default_factory=list)
    needs_order_active: bool = False
    warnings: List[str] = field(default_factory=list)
    #: ``corner_start[f]``: the file corner index where face ``f`` starts.
    corner_start: List[int] = field(default_factory=list)
    #: File order to export order (spec 13.6). Index-bound records are remapped through it (``_remap_blobs``).
    permutation: Optional[Permutation] = None

    @property
    def inline_proxy_count(self) -> int:
        return sum(1 for group in self.groups if group.inline_proxy is not None)


def make_plan(lod: LOD, lod_index: int) -> LodPlan:
    """Plan one LOD. Raises ``ValueError`` (with a finished message) when a face points at a missing point."""
    plan = LodPlan(lod=lod, lod_index=lod_index, lod_name=lod.name)
    _check_indices(plan)
    _check_short_faces(plan)
    plan.corner_start = _corner_starts(lod)
    _classify_taggs(plan)
    _split_mesh(plan)
    _remap_blobs(plan)
    _check_face_uv(plan)
    _check_proxy_order(plan)
    return plan


def _warn(plan: LodPlan, what: str, fix: Optional[str] = None) -> None:
    plan.warnings.append(messages.format_problem(plan.lod_name, what, fix))


def _check_indices(plan: LodPlan) -> None:
    lod = plan.lod
    bad = [face for face in range(lod.face_count)
           if any(v >= lod.point_count for v in lod.face_vertices[4 * face:4 * face + lod.face_sides[face]])]
    if bad:
        raise ValueError(messages.format_problem(
            plan.lod_name, "%d faces use points that do not exist (%s)" % (
                len(bad), messages.list_elements("faces", bad)), "The file is damaged"))
    bad = [face for face in range(lod.face_count)
           if any(n >= lod.normal_count for n in lod.face_normals[4 * face:4 * face + lod.face_sides[face]])]
    if bad:
        raise ValueError(messages.format_problem(
            plan.lod_name, "%d faces use normals that do not exist (%s)" % (
                len(bad), messages.list_elements("faces", bad)), "The file is damaged"))


def _check_short_faces(plan: LodPlan) -> None:
    """Spec 5: faces with 0 to 2 sides are imported, but export drops them. Say so on the import."""
    short = [face for face, sides in enumerate(plan.lod.face_sides) if sides < 3]
    if short:
        _warn(plan, "%d faces have fewer than 3 points (%s)" % (len(short), messages.list_elements("faces", short)),
              "They are imported, but the export drops them")


def _corner_starts(lod: LOD) -> List[int]:
    starts = []
    total = 0
    for sides in lod.face_sides:
        starts.append(total)
        total += sides
    return starts


# -- TAGGs ---------------------------------------------------------------------------------------------------------

def _classify_taggs(plan: LodPlan) -> None:
    """One pass over the TAGGs in file order: group, attribute, proxy or blob; and the order hint."""
    lod = plan.lod
    seen_names: Dict[str, str] = {}
    seen_folded: Dict[str, str] = {}
    face_use = _face_use_counts(lod)
    for tagg in lod.taggs:
        token = _classify_one(plan, tagg, seen_names, seen_folded, face_use)
        plan.order.append(token)
        plan.order_active.append(tagg.active)
        if token[:2] != "#?" and tagg.active not in (0, 1):
            plan.needs_order_active = True


def _face_use_counts(lod: LOD) -> List[int]:
    counts = [0] * lod.point_count
    for face in range(lod.face_count):
        for v in lod.face_vertices[4 * face:4 * face + lod.face_sides[face]]:
            counts[v] += 1
    return counts


def _blob(plan: LodPlan, tagg: m1.Tagg, reason: str) -> str:
    plan.blobs.append(BlobPlan(tagg.name, tagg.active, tagg.payload(), reason, tagg))
    return "#?%d" % (len(plan.blobs) - 1)


def _classify_one(plan: LodPlan, tagg: m1.Tagg, seen_names, seen_folded, face_use) -> str:
    """Return the order token of ``tagg`` and record what it becomes."""
    if isinstance(tagg, (m1.MaskTagg, m1.AnimationTagg)):
        return _blob(plan, tagg, "editor data without a Houdini form")
    if type(tagg) is m1.RawTagg:
        return _blob(plan, tagg, "unknown or malformed TAGG")
    if tagg.active == 0:
        return _blob(plan, tagg, "inactive TAGG")
    if isinstance(tagg, m1.SelectionTagg):
        return _classify_selection(plan, tagg, seen_names, seen_folded, face_use)
    if isinstance(tagg, m1.UVSetTagg):
        if tagg.set_id in plan.uv_sets:
            _warn(plan, "UV set id %d appears more than once" % tagg.set_id,
                  "Later copies are kept as opaque data and exported unchanged")
            return _blob(plan, tagg, "repeated UV set id")
        plan.uv_sets[tagg.set_id] = tagg
        return "%s%d" % (UVSET_TOKEN, tagg.set_id)
    if isinstance(tagg, m1.SharpEdgesTagg):
        if plan.sharp is not None:
            return _blob(plan, tagg, "repeated #SharpEdges#")
        plan.sharp = tagg
        return SHARP_TOKEN
    if isinstance(tagg, m1.MassTagg):
        if plan.mass is not None:
            return _blob(plan, tagg, "repeated #Mass#")
        plan.mass = tagg
        return MASS_TOKEN
    plan.properties.append(tagg)
    return PROPERTY_TOKEN


def _classify_selection(plan: LodPlan, tagg: SelectionTagg, seen_names, seen_folded, face_use) -> str:
    name = tagg.name
    if name == "":
        _warn(plan, "a selection has an empty name, which Houdini cannot use for a group",
              "It is kept as opaque data and exported unchanged")
        return _blob(plan, tagg, "empty selection name")
    if name in seen_names:
        _warn(plan, "the selection %r appears more than once" % name,
              "Later copies are kept as opaque data and exported unchanged")
        return _blob(plan, tagg, "duplicate selection name")
    seen_names[name] = name
    folded = name.casefold()
    if folded in seen_folded:
        _warn(plan, "the selections %r and %r differ only in case. Object Builder and the game may treat them as one"
              % (seen_folded[folded], name), "Rename one of them")
    else:
        seen_folded[folded] = name

    parsed = p3d.parse_proxy_name(name)
    if parsed is None:
        plan.groups.append(GroupPlan(name, tagg))
        return name
    path, index = parsed
    reformats = p3d.format_proxy_name(path, index) == name
    proxy = _packed_proxy(plan, tagg, path, index, face_use) if reformats else None
    if proxy is not None:
        plan.proxies.append(proxy)
        return name
    if reformats:
        plan.groups.append(GroupPlan(name, tagg, (path, index)))
    else:
        plan.groups.append(GroupPlan(name, tagg))
    _warn(plan, "the proxy %r stays an inline triangle in the mesh" % name,
          "It does not fit the packed form (one triangle on three points that nothing else uses). The round trip "
          "is still lossless")
    return name


def _packed_proxy(plan: LodPlan, tagg: SelectionTagg, path: str, index: int, face_use) -> Optional[ProxyPlan]:
    """Spec 13.6: a proxy becomes a packed prim only if it is exactly one triangle on three private points."""
    lod = plan.lod
    members = [i for i, byte in enumerate(tagg.vertex_weights) if byte]
    faces = [i for i, byte in enumerate(tagg.face_weights) if byte]
    if len(members) != 3 or len(faces) != 1:
        return None
    face = faces[0]
    if lod.face_sides[face] != 3 or tagg.face_weights[face] != 1 or any(tagg.vertex_weights[i] != 1 for i in members):
        return None
    corners = tuple(lod.face_vertices[4 * face:4 * face + 3])
    if sorted(corners) != members or len(set(corners)) != 3:
        return None
    if any(face_use[v] != 1 for v in corners):
        return None
    try:
        p3d.proxy_frame(*(lod.point(v) for v in corners))
    except ValueError:
        _warn(plan, "the proxy %r has a degenerate triangle" % tagg.name, "Check the proxy's three points")
        return None
    return ProxyPlan(tagg.name, path, index, face, corners, tagg)


# -- Mesh, UVs, proxy order ----------------------------------------------------------------------------------------

def _split_mesh(plan: LodPlan) -> None:
    """Spec 13.3: packed proxies leave the mesh; their prims are made last, in ascending proxy face order."""
    lod = plan.lod
    plan.proxies.sort(key=lambda proxy: proxy.face)
    proxy_points = {v for proxy in plan.proxies for v in proxy.corners}
    proxy_faces = {proxy.face for proxy in plan.proxies}
    plan.mesh_points = [i for i in range(lod.point_count) if i not in proxy_points]
    plan.mesh_faces = [i for i in range(lod.face_count) if i not in proxy_faces]
    plan.permutation = Permutation.for_import(plan.mesh_points, plan.mesh_faces, plan.proxies, lod.face_sides)


def _remap_blobs(plan: LodPlan) -> None:
    """Spec 13.6: when the proxies move, every kept record that holds point, face or corner numbers moves with them.

    The blobs are rewritten for the export order (see ``permutation``). A blob that cannot be rewritten is dropped
    with a warning, and its order token goes with it (the later blob tokens are renumbered). The live sharp pairs of
    ``a3ob_sharp_extra`` are remapped by the builder (``_write_sharp_edges``)."""
    perm = plan.permutation
    if perm.is_identity or not plan.blobs:
        return
    kept: List[BlobPlan] = []
    new_index: Dict[int, int] = {}
    for old_index, blob in enumerate(plan.blobs):
        payload = remap_payload(blob.tagg, perm) if blob.tagg is not None else None
        if payload is None:
            _warn(plan, "the TAGG %r (record %d) is dropped because the proxies moved to the end and its point and face "
                  "numbers cannot be moved with them" % (blob.name, old_index),
                  "Its layout is unknown, or it does not fit the LOD")
            continue
        blob.payload = payload
        new_index[old_index] = len(kept)
        kept.append(blob)
    if len(kept) == len(plan.blobs):
        return
    order, active = [], []
    for token, byte in zip(plan.order, plan.order_active):
        if token[:2] == "#?":
            if int(token[2:]) not in new_index:
                continue
            token = "#?%d" % new_index[int(token[2:])]
        order.append(token)
        active.append(byte)
    plan.blobs, plan.order, plan.order_active = kept, order, active


def _check_face_uv(plan: LodPlan) -> None:
    """Spec 8: keep the UVs of the face records only if they carry something set 0 does not."""
    lod = plan.lod
    face_uvs = lod.corner_uvs()
    first = plan.uv_sets.get(0)
    if first is not None:
        plan.needs_face_uv = array("f", first.uvs) != face_uvs
    else:
        plan.needs_face_uv = any(face_uvs)


def _check_proxy_order(plan: LodPlan) -> None:
    """Spec 13.6: proxies at the end of the point and face lists round-trip byte for byte, others do not."""
    if not plan.proxies:
        return
    lod = plan.lod
    count = len(plan.proxies)
    tail_faces = set(range(lod.face_count - count, lod.face_count))
    tail_points = set(range(lod.point_count - 3 * count, lod.point_count))
    faces = {proxy.face for proxy in plan.proxies}
    points = {v for proxy in plan.proxies for v in proxy.corners}
    if faces != tail_faces or points != tail_points:
        _warn(plan, "the proxies are not the last points and faces of the LOD",
              "They are packed anyway. Exporting this LOD gives the same content, but the bytes can differ")

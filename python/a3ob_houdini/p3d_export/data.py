# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Plain Python data that the export reads out of Houdini geometry (no ``hou`` here).

``read_geo`` fills these classes from a ``hou.Geometry``. Everything after that (``lod_build`` and its helpers) works on
them alone, so the export decisions of spec sections 3 to 16 can be tested under plain Python.

Values are kept as Houdini holds them: positions and normals are in Houdini axes, polygon corners are in Houdini
vertex order, flags are int32 (or u32). The build step maps them to file space (spec 4.3).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from a3ob_core import p3d

__all__ = ["Vec", "Poly", "GroupData", "MeshData", "ProxyData", "Details", "LodData", "Collection"]

Vec = Tuple[float, float, float]


@dataclass
class Poly:
    """One closed polygon of a LOD (or of a proxy's embedded triangle)."""

    #: Primitive number in the source geometry, for messages.
    number: int
    #: Point numbers in Houdini vertex order.
    points: Tuple[int, ...]
    #: Normal of each vertex (Houdini axes), or None when the geometry has no ``N`` at all.
    normals: Optional[List[Vec]] = None
    #: ``uv``, ``uv2``... and ``a3ob_face_uv``: the (u, v) of each vertex, as Houdini holds them.
    uvs: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    flags: int = 0
    texture: str = ""
    material: str = ""
    #: Inline proxy reference (``a3ob_proxy_path``, ``a3ob_proxy_index``), or None.
    proxy: Optional[Tuple[str, int]] = None
    #: ``a3ob_lod_uid`` (0 = none).
    uid: int = 0


@dataclass
class GroupData:
    """A point group that is a selection candidate, with the data that belongs to it (spec 7.2)."""

    encoded: str
    #: The decoded P3D name.
    name: str
    #: Member point numbers.
    members: List[int] = field(default_factory=list)
    #: ``a3ob_w_<group>`` for every point, or None when the attribute does not exist.
    weights: Optional[List[float]] = None
    #: Member polygons (index into ``MeshData.polys``) of the prim group of the same name, or None without one.
    faces: Optional[List[int]] = None
    #: ``a3ob_fw_<group>`` for every polygon, or None.
    face_weights: Optional[List[float]] = None


@dataclass
class MeshData:
    """The points, polygons and groups of one geometry."""

    points: List[Vec] = field(default_factory=list)
    #: ``a3ob_vertex_flags`` per point, or None when the attribute does not exist.
    point_flags: Optional[List[int]] = None
    #: ``a3ob_mass`` per point, or None.
    masses: Optional[List[float]] = None
    polys: List[Poly] = field(default_factory=list)
    groups: List[GroupData] = field(default_factory=list)
    #: Edge group ``a3ob_sharp`` as ordered point-number pairs, or None when the group does not exist.
    sharp: Optional[List[Tuple[int, int]]] = None
    #: The ``uv*`` attributes that become UV sets, in ascending set id.
    uv_names: List[str] = field(default_factory=list)
    #: Points that only packed prims use (spec 5, "Pack points"). They are not exported.
    pack_points: Set[int] = field(default_factory=set)
    #: True when something other than polygons and packed prims was found (the reader reported it).
    has_other_prims: bool = False


@dataclass
class ProxyData:
    """A packed proxy prim (spec 13.3)."""

    prim_number: int
    outer_path: str
    outer_index: int
    inner_path: str
    inner_index: int
    #: ``fullTransform()`` after the LOD was unpacked: 16 numbers, row-major, row vectors (translation in row 3).
    transform: Tuple[float, ...]
    #: The embedded geometry.
    mesh: MeshData
    #: ``a3ob_proxy_src_P`` / ``a3ob_proxy_src_N``: 9 floats each (spec 13.3), or None.
    src_p: Optional[Tuple[float, ...]] = None
    src_n: Optional[Tuple[float, ...]] = None

    @property
    def valid_triangle(self) -> bool:
        """Spec 13.5 step 2: exactly 3 points and 1 triangle, nothing else."""
        mesh = self.mesh
        return (len(mesh.points) == 3 and len(mesh.polys) == 1 and len(mesh.polys[0].points) == 3
                and len(set(mesh.polys[0].points)) == 3 and not mesh.has_other_prims)


@dataclass
class Details:
    """The detail attributes of a LOD geometry (spec 3.3). ``None`` or an empty list means "absent"."""

    lod_type: int = 0
    resolution: float = 1.0
    signature: Optional[int] = None
    flags: int = 0
    eof_active: int = 1
    copy_types: List[int] = field(default_factory=list)
    copy_resolutions: List[float] = field(default_factory=list)
    prop_names: List[str] = field(default_factory=list)
    prop_values: List[str] = field(default_factory=list)
    prop_raw: List[str] = field(default_factory=list)
    tagg_order: List[str] = field(default_factory=list)
    tagg_active: List[int] = field(default_factory=list)
    unknown_names: List[str] = field(default_factory=list)
    unknown_active: List[int] = field(default_factory=list)
    unknown_data: List[str] = field(default_factory=list)
    source_counts: Optional[Tuple[int, int, int]] = None
    sharp_extra: List[int] = field(default_factory=list)
    #: ``a3ob_layout`` (spec 19): the file layout record as text, or "" when the LOD has none.
    layout: str = ""


@dataclass
class LodData:
    """One collected LOD, ready to build (spec 3.2)."""

    index: int
    mesh: MeshData
    details: Details
    proxies: List[ProxyData] = field(default_factory=list)
    #: The LOD prim's transform had a negative determinant: the face builder reverses every polygon.
    mirrored: bool = False

    @property
    def label(self) -> str:
        """The LOD name used in messages (``Resolution 1``, ``Geometry``), or ``LOD <n>``."""
        try:
            return p3d.format_lod_name(self.details.lod_type, self.details.resolution)
        except (ValueError, TypeError):
            return "LOD %d" % self.index


@dataclass
class Collection:
    """What collection found: LODs in order and the file-level trailing bytes (spec 3.3)."""

    lods: List[LodData] = field(default_factory=list)
    trailing: bytes = b""

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB utilities/validator.py, v2.5.1 (9f2b8bc): the per-LOD-type validator rules are ported as warnings
# on Houdini data (spec 17.1.1). The line numbers in the rule table are lines of that file. A3OB: MrClock (present
# add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""Tier-2 validator rules: A3OB's per-LOD-type checks as switchable warnings (spec 17.1.1, D14 Q-3). No ``hou`` here.

Each rule has an id such as ``T2-TRI``, the LOD types it applies to, and a check on the plain data of one LOD
(``LodData``). A failing rule gives one warning per LOD that names the first bad element, as A3OB does. A rule is on
unless its id is in ``ExportOptions.rules_off``. The rules only warn: they never change the bytes.

The data is in Houdini axes and vertex order. The checks need only shapes, so the axis map does not matter.
"""

import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from a3ob_core.p3d.lod_types import LodType

from .. import messages
from .data import LodData, Poly
from .normals import face_normal
from .options import DEFAULT_RULES_OFF, ExportOptions

__all__ = ["Rule", "RULES", "RULE_IDS", "rule_by_id", "check_lod_rules", "GEOMETRY_TYPES", "SHADOW_TYPES"]

GEOMETRY_TYPES = frozenset((LodType.GEOMETRY, LodType.GEOMETRY_BUOY, LodType.GEOMETRY_PHYSX))
MASS_TYPES = frozenset((LodType.GEOMETRY,))
SHADOW_TYPES = frozenset((LodType.SHADOW, LodType.SHADOW_VIEW_CARGO, LodType.SHADOW_VIEW_PILOT,
                          LodType.SHADOW_VIEW_GUNNER))
POINT_CLOUD_TYPES = frozenset((LodType.MEMORY, LodType.LANDCONTACT, LodType.HITPOINTS))
ANY = None  # a rule for every LOD type

#: A3OB ``only_conventional_indices``.
SHADOW_RESOLUTIONS = (0, 10, 1000, 1010, 1020)
#: A3OB ``only_valid_uvs`` (Groundlayer): the UV values a ground layer may use.
GROUNDLAYER_UVS = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (1.0, -1.0))
MASS_MINIMUM = 0.001
ROADWAY_POINT_LIMIT = 255
#: A3OB ``is_convex_edge_internal``: two faces whose normals have a dot product in this range are flat against each other.
FLAT_RANGE = (0.9999, 1.0001)
#: A smooth polygon (for ``T2-SHARP``): a vertex normal that is further from the face normal than this.
SMOOTH_DOT = 0.9999
UV_TOLERANCE = 1e-4

_COMPONENT = re.compile(r"component\d+", re.IGNORECASE)
_OCCLUDER = re.compile(r"occluder\d+", re.IGNORECASE)
_ENTRY = re.compile(r"in\d+", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    """One tier-2 rule."""

    id: str
    #: Short name for the switch label.
    label: str
    #: The LOD types it applies to, or None for every type.
    types: Optional[FrozenSet[int]]
    #: ``check(context)`` returns None when the LOD passes, else ``(what is wrong, how to fix it)``.
    check: Callable[["_Context"], Optional[Tuple[str, str]]]
    help: str
    #: D24: a failure is an info note, not a warning.
    info: bool = False

    @property
    def default_on(self):
        return self.id not in DEFAULT_RULES_OFF

    def applies_to(self, lod_type: int) -> bool:
        return self.types is None or lod_type in self.types


class _Context:
    """The data of one LOD and the things several rules share (computed once, only when a rule asks)."""

    def __init__(self, data: LodData, options: ExportOptions):
        self.data = data
        self.mesh = data.mesh
        self.options = options
        #: Polygons that are written: 3 or 4 points (spec 5).
        self.polys: List[Poly] = [poly for poly in self.mesh.polys if 3 <= len(poly.points) <= 4]
        self._edges: Optional[Dict[Tuple[int, int], List[Tuple[int, Tuple[int, int]]]]] = None
        self._sign: Optional[float] = None

    @property
    def lod_type(self) -> int:
        return self.data.details.lod_type

    @property
    def edges(self) -> Dict[Tuple[int, int], List[Tuple[int, Tuple[int, int]]]]:
        """Undirected edge -> ``[(index in self.polys, (from point, to point)), ...]`` in polygon order."""
        if self._edges is None:
            edges: Dict[Tuple[int, int], List[Tuple[int, Tuple[int, int]]]] = {}
            for number, poly in enumerate(self.polys):
                count = len(poly.points)
                for k in range(count):
                    a, b = poly.points[k], poly.points[(k + 1) % count]
                    if a != b:
                        edges.setdefault((min(a, b), max(a, b)), []).append((number, (a, b)))
            self._edges = edges
        return self._edges

    def has_group(self, pattern: "re.Pattern") -> bool:
        """A group whose decoded name starts with the pattern (A3OB ``re.match``, ignoring case). None for no points."""
        if not self.mesh.points:
            return False
        return any(pattern.match(group.name) for group in self.mesh.groups)

    def positions(self, poly: Poly):
        return [self.mesh.points[p] for p in poly.points]

    def normal(self, poly: Poly):
        """The unit Houdini face normal, or None for a polygon with no area."""
        x, y, z = face_normal(self.positions(poly))
        length = math.sqrt(x * x + y * y + z * z)
        return None if length == 0.0 else (x / length, y / length, z / length)

    @property
    def outward(self) -> float:
        """+1 when the winding of the mesh points the Houdini normals outwards (positive signed volume), else -1. This
        makes the convexity test independent of the winding of the whole mesh."""
        if self._sign is None:
            volume = 0.0
            for poly in self.polys:
                area = face_normal(self.positions(poly))
                origin = self.mesh.points[poly.points[0]]
                volume += origin[0] * area[0] + origin[1] * area[1] + origin[2] * area[2]
            self._sign = -1.0 if volume < 0.0 else 1.0
        return self._sign

    @property
    def scale(self) -> float:
        points = self.mesh.points
        if not points:
            return 1.0
        low = [min(p[k] for p in points) for k in range(3)]
        high = [max(p[k] for p in points) for k in range(3)]
        return max(1.0, math.sqrt(sum((high[k] - low[k]) ** 2 for k in range(3))))


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# -- The rules -------------------------------------------------------------------------------------------------------

def _uv2(c: _Context):
    count = len(c.mesh.uv_names)
    if count > 2:
        return ("the LOD has %d UV sets and the game uses at most 2" % count, "Remove the extra uv attributes")
    return None


def _contig(c: _Context):
    if _skips_for_occluder(c):
        return None
    for edge, users in c.edges.items():
        if len(users) == 2 and users[0][1] == users[1][1]:
            numbers = [c.polys[users[0][0]].number, c.polys[users[1][0]].number]
            return ("the mesh is not contiguous: polygons %d and %d run along their shared edge in the same direction "
                    "(one of them is flipped)" % tuple(numbers), "Reverse the flipped polygons")
    return None


def _skips_for_occluder(c: _Context) -> bool:
    return c.lod_type in GEOMETRY_TYPES and c.has_group(_OCCLUDER)


def _quad_is_convex(c: _Context, poly: Poly) -> bool:
    """True when the four corners turn the same way round the face normal (no reflex corner, no bow tie)."""
    normal = face_normal(c.positions(poly))
    if normal == (0.0, 0.0, 0.0):
        return False
    points = c.positions(poly)
    signs = set()
    for k in range(4):
        a, b, d = points[k], points[(k + 1) % 4], points[(k + 2) % 4]
        edge1 = tuple(b[i] - a[i] for i in range(3))
        edge2 = tuple(d[i] - b[i] for i in range(3))
        cross = (edge1[1] * edge2[2] - edge1[2] * edge2[1], edge1[2] * edge2[0] - edge1[0] * edge2[2],
                 edge1[0] * edge2[1] - edge1[1] * edge2[0])
        turn = _dot(cross, normal)
        if turn == 0.0:
            return False
        signs.add(turn > 0.0)
    return len(signs) == 1


def _tri(c: _Context):
    # D24: Bohemia's own Geometry and Shadow Volume LODs hold convex quads, so those are fine there.
    quads_ok = c.lod_type in GEOMETRY_TYPES or c.lod_type in SHADOW_TYPES
    for poly in c.mesh.polys:
        count = len(poly.points)
        if count < 3 or count == 3 or (count == 4 and quads_ok and _quad_is_convex(c, poly)):
            continue
        if quads_ok:
            what = "the mesh is not triangulated: polygon %d is %s" % (
                poly.number, "a quad that is not convex" if count == 4 else "a polygon with %d points" % count)
            return what + " (only triangles and convex quads are allowed)", "Add a Divide node with Convex Polygons on"
        return ("the mesh is not triangulated: polygon %d has %d points" % (poly.number, count),
                "Add a Divide node with Convex Polygons on and Maximum Edges 3")
    return None


def _convex(c: _Context):
    tolerance = 1e-5 * c.scale
    for edge, users in c.edges.items():
        if len(users) != 2:
            continue
        first, second = c.polys[users[0][0]], c.polys[users[1][0]]
        n1, n2 = c.normal(first), c.normal(second)
        if n1 is None or n2 is None:
            continue
        if FLAT_RANGE[0] <= _dot(n1, n2) <= FLAT_RANGE[1]:
            continue
        origin = c.mesh.points[edge[0]]
        away = max(_dot((c.outward * n1[0], c.outward * n1[1], c.outward * n1[2]),
                        tuple(c.mesh.points[p][k] - origin[k] for k in range(3)))
                   for p in second.points if p not in edge)
        if away > tolerance:
            return ("the mesh is not convex: the edge between polygons %d and %d bends outwards" % (
                first.number, second.number), "Split the geometry into convex components")
    return None


def _comp(c: _Context):
    if _skips_for_occluder(c) or c.options.auto_components:
        return None  # auto-components make the missing ones (the check counts them as present)
    if not c.has_group(_COMPONENT):
        return ("the LOD has no component selections (no group called component01, component02, ...)",
                "Make a point group for each convex piece, named component01, component02, ...")
    return None


def _mass(c: _Context):
    masses = c.mesh.masses
    if masses is None or math.fsum(masses) < MASS_MINIMUM:
        return ("this Geometry LOD has no mass. Fine for items such as vests or scopes; physical objects need mass",
                "Use the Mass node on physical objects")
    return None


def _massall(c: _Context):
    masses = c.mesh.masses
    if masses is None:
        return "the points have no mass (there is no a3ob_mass)", "Use the Mass node"
    for number, value in enumerate(masses):
        if number not in c.mesh.pack_points and not value >= MASS_MINIMUM:
            return "point %d has no mass (a3ob_mass is below 0.001)" % number, "Use the Mass node on all points"
    return None


def _smooth(c: _Context, poly: Poly) -> bool:
    """A polygon is smooth when its vertex normals differ from its face normal, or when it has none (the export then
    computes smooth normals, split only along ``a3ob_sharp``)."""
    if poly.normals is None:
        return True
    face = c.normal(poly)
    if face is None:
        return False
    for normal in poly.normals:
        length = math.sqrt(_dot(normal, normal))
        if length == 0.0 or _dot(normal, face) / length < SMOOTH_DOT:
            return True
    return False


def _sharp(c: _Context):
    if not any(_smooth(c, poly) for poly in c.polys):
        return None
    sharp = {(min(a, b), max(a, b)) for a, b in (c.mesh.sharp or ())}
    for edge, users in c.edges.items():
        if len(users) == 2 and edge not in sharp:
            return ("the mesh has smooth edges: the edge between polygons %d and %d is not in a3ob_sharp although "
                    "some polygons are smooth" % (c.polys[users[0][0]].number, c.polys[users[1][0]].number),
                    "Put the edges of a shadow volume in the a3ob_sharp edge group")
    return None


def _nomat(c: _Context):
    # D24: Shadow Volume LODs may carry a texture or material (foliage alpha shadows in Bohemia's own files).
    if c.lod_type in SHADOW_TYPES:
        return None
    for poly in c.mesh.polys:
        if poly.texture or poly.material:
            return ("polygon %d has a texture or material" % poly.number, "Clear a3ob_texture and a3ob_material")
    return None


def _shadowres(c: _Context):
    resolution = c.data.details.resolution
    if resolution != resolution or resolution != int(resolution) or int(resolution) not in SHADOW_RESOLUTIONS:
        return ("the shadow resolution %s is unconventional (usual: %s)" % (
            ("%g" % resolution), ", ".join(str(v) for v in SHADOW_RESOLUTIONS)), "Check the resolution of the LOD")
    return None


def _shadowoff(c: _Context):
    details = c.data.details
    for name, value in zip(details.prop_names, details.prop_values):
        if name.strip().lower() == "lodnoshadow" and value.strip() == "1":
            return "the LOD is disabled by the property LODNoShadow = 1", "Remove the property to cast shadows"
    return None


def _pc_edge(c: _Context):
    for poly in c.mesh.polys:
        if len(poly.points) == 2:
            return "the LOD contains edges (polygon %d has 2 points)" % poly.number, "Use points only"
    return None


def _pc_face(c: _Context):
    for poly in c.mesh.polys:
        if len(poly.points) >= 3:
            return "the LOD contains faces (polygon %d)" % poly.number, "Use points only"
    return None


def _rw_faces(c: _Context):
    if not c.polys:
        return "the LOD has no polygons", "Add the polygons of the surface"
    return None


def _rw_limit(c: _Context):
    count = len(c.mesh.points) - len(c.mesh.pack_points)
    if count > ROADWAY_POINT_LIMIT:
        return ("the LOD has %d points and animations break above %d" % (count, ROADWAY_POINT_LIMIT),
                "Use fewer points")
    return None


def _rw_sound(c: _Context):
    for poly in c.polys:
        if not poly.texture:
            return "polygon %d has no sound texture (a3ob_texture is empty)" % poly.number, "Set a texture on every polygon"
    return None


def _gl_uv(c: _Context):
    count = len(c.mesh.uv_names)
    if count != 1:
        return "the LOD has %d UV sets and a ground layer needs exactly 1" % count, "Keep one uv attribute"
    return None


def _gl_uvvalues(c: _Context):
    if len(c.mesh.uv_names) != 1:
        return None  # T2-GL-UV reports that
    name = c.mesh.uv_names[0]
    for poly in c.polys:
        for uv in poly.uvs.get(name, ()):
            if not any(abs(uv[0] - a) <= UV_TOLERANCE and abs(uv[1] - b) <= UV_TOLERANCE for a, b in GROUNDLAYER_UVS):
                return ("polygon %d has the UV (%g, %g), and a ground layer allows only (0, 0), (1, 0), (1, 1) and (1, -1)"
                        % (poly.number, uv[0], uv[1]), "Fix the UVs")
    return None


def _path_entry(c: _Context):
    if not c.has_group(_ENTRY):
        return "the LOD has no entry points (no group called in01, in02, ...)", "Make a point group for each entry"
    return None


def _path_pos(c: _Context):
    # A3OB tests the same pattern as the entry rule here although its message says "position points" (spec 17.1.1).
    if not c.has_group(_ENTRY):
        return "the LOD has no position points (no group called in01, in02, ...)", "Make a point group for each position"
    return None


_TYPES_CONTIG = GEOMETRY_TYPES | SHADOW_TYPES | {LodType.UNDERGROUND}

RULES: Tuple[Rule, ...] = (
    Rule("T2-UV2", "At most 2 UV sets", ANY, _uv2, "The LOD has no more than 2 UV sets."),
    Rule("T2-CONTIG", "Contiguous mesh", _TYPES_CONTIG, _contig,
         "Geometry, shadow and underground LODs: no flipped polygons (shared edges run in opposite directions)."),
    Rule("T2-TRI", "Triangulated", _TYPES_CONTIG, _tri,
         "Underground LODs: every polygon is a triangle. Geometry and shadow LODs: triangles and convex quads."),
    Rule("T2-CONVEX", "Convex mesh", GEOMETRY_TYPES | {LodType.UNDERGROUND}, _convex,
         "Geometry and underground LODs: every shared edge is convex or flat."),
    Rule("T2-COMP", "Component selections", GEOMETRY_TYPES | {LodType.UNDERGROUND}, _comp,
         "Geometry and underground LODs: a group called component01, component02, ... exists."),
    # D16: mass is checked on the Geometry LOD only, as in A3OB (validator.py:292-311; the subtype validator has none).
    Rule("T2-MASS", "Mass is set", MASS_TYPES, _mass,
         "Geometry LOD: a note (not a warning) when the points have a total mass below 0.001.", info=True),
    Rule("T2-MASSALL", "Every point has mass", MASS_TYPES, _massall, "Geometry LOD: every point has a mass of at least 0.001."),
    Rule("T2-SHARP", "No smooth edges", SHADOW_TYPES | {LodType.UNDERGROUND}, _sharp,
         "Shadow and underground LODs: with smooth polygons, every shared edge is in a3ob_sharp."),
    Rule("T2-NOMAT", "No materials", SHADOW_TYPES | {LodType.UNDERGROUND}, _nomat,
         "Underground LODs: no polygon has a texture or material. Shadow LODs may have them."),
    Rule("T2-SHADOWRES", "Conventional shadow resolution", SHADOW_TYPES, _shadowres,
         "Shadow LODs: the resolution is 0, 10, 1000, 1010 or 1020. Off by default."),
    Rule("T2-SHADOWOFF", "Shadow not disabled", SHADOW_TYPES, _shadowoff, "Shadow LODs: no LODNoShadow = 1 property. Off by default."),
    Rule("T2-PC-EDGE", "No edges in point clouds", POINT_CLOUD_TYPES, _pc_edge,
         "Memory, land contact and hit-point LODs: no 2-point primitives."),
    Rule("T2-PC-FACE", "No faces in point clouds", POINT_CLOUD_TYPES, _pc_face,
         "Memory, land contact and hit-point LODs: no polygons."),
    Rule("T2-RW-FACES", "Has polygons", frozenset((LodType.ROADWAY, LodType.GROUNDLAYER, LodType.PATHS)), _rw_faces,
         "Roadway, ground layer and paths LODs: at least one polygon."),
    Rule("T2-RW-LIMIT", "Roadway point limit", frozenset((LodType.ROADWAY,)), _rw_limit, "Roadway LODs: at most 255 points."),
    Rule("T2-RW-SOUND", "Roadway sound textures", frozenset((LodType.ROADWAY,)), _rw_sound,
         "Roadway LODs: every polygon has a texture (the sound)."),
    Rule("T2-GL-UV", "Ground layer UV set", frozenset((LodType.GROUNDLAYER,)), _gl_uv, "Ground layer LODs: exactly one UV set."),
    Rule("T2-GL-UVVALUES", "Ground layer UV values", frozenset((LodType.GROUNDLAYER,)), _gl_uvvalues,
         "Ground layer LODs: every UV is (0, 0), (1, 0), (1, 1) or (1, -1)."),
    Rule("T2-PATH-ENTRY", "Path entry points", frozenset((LodType.PATHS,)), _path_entry,
         "Paths LODs: a group called in01, in02, ... exists."),
    Rule("T2-PATH-POS", "Path position points", frozenset((LodType.PATHS,)), _path_pos,
         "Paths LODs: the same test as the entry rule (A3OB's own test, see spec 17.1.1)."),
)

RULE_IDS: Tuple[str, ...] = tuple(rule.id for rule in RULES)


def rule_by_id(rule_id: str) -> Rule:
    for rule in RULES:
        if rule.id == rule_id:
            return rule
    raise KeyError(rule_id)


def check_lod_rules(data: LodData, options: ExportOptions, report: messages.Report) -> None:
    """Run every rule that applies to the LOD's type and is on. Each failure is one warning (spec 17.1.1)."""
    lod_type = data.details.lod_type
    context = None
    for rule in RULES:
        if rule.id in options.rules_off or not rule.applies_to(lod_type):
            continue
        if context is None:
            context = _Context(data, options)
        problem = rule.check(context)
        if problem is not None:
            what, fix = problem
            text = messages.format_problem(data.label, "%s (rule %s)" % (what, rule.id), fix)
            if rule.info:
                report.info(text)
            else:
                report.warning(text)

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the pages are built by ``a3ob_core.digest`` (docs/design/digests.md, task #81).
"""The rig digests in a Houdini session: read a node's geometry with the project's own readers, hand it to ``a3ob_core.digest``.

From a live session (a one-line call; each returns a ``Page`` that prints as its text)::

    from a3ob_houdini import digest
    print(digest.skeleton("/obj/rig_fit_66/rig_fit"))

From a command line: ``hython scripts/a3ob_digest.py <digest> <scene.hip*> <node path> [options]`` (``main`` below). The
command loads the scene read-only and never saves it.

Every reader here is one that a node already uses: ``rig_weights.read_part`` (the parts of a Rig Fit output),
``read_skeleton``, ``read_selections`` (the bone weights as P3D Export reads them), ``read_capture``, ``read_mesh`` and
``group_mask``; ``landmark_io`` for the landmarks and the sample side. A digest holds no formula of its own.
"""

import argparse
import os
import sys
from typing import List, Optional

import numpy as np

from a3ob_core import digest as core
from a3ob_core.digest import CouldNotAnswer, WrongQuestion
from a3ob_core.digest.landmarks import SampleSide
from a3ob_core.digest.skeleton import Skel, Source
from a3ob_core.rig import LANDMARK_NAMES

from . import landmark_io, rig_weights
from .errors import UserError
from .p3d_export.rig_bones import OFP2_MANSKELETON_BONES

try:
    import hou
except ImportError:  # the pure parts are also read under plain Python
    hou = None

__all__ = ["skeleton", "skeleton_compare", "deform_compare", "weights", "landmarks", "feet", "wrap_distance", "DIGESTS", "main"]

LABEL = "digest"
#: The Rig Check limits the weights page puts beside its figures.
SUM_LIMIT = 0.01
MAX_INFLUENCES = 4


# ---- reading ------------------------------------------------------------------------------------------------------------

def _scene() -> str:
    return hou.hipFile.basename() or "(unsaved scene)"


def _node(path: str):
    node = hou.node(path)
    if node is None:
        raise WrongQuestion("there is no node %s in the scene" % path)
    return node


def _geometry(node):
    try:
        geo = node.geometry()
    except hou.OperationFailed as problem:
        raise CouldNotAnswer("%s does not cook: %s" % (node.path(), "; ".join(node.errors()) or problem)) from None
    except hou.ObjectWasDeleted:  # pragma: no cover
        raise WrongQuestion("%s was deleted" % node.path()) from None
    if geo is None:
        raise CouldNotAnswer("%s has no geometry" % node.path())
    return geo


def _input(node, number: int):
    try:
        return node.inputGeometry(number)
    except hou.OperationFailed:
        return None


def _part(geo, wanted: str, node, part: Optional[str]):
    """``(geometry, part name)``: the part of a Rig Fit output, or the geometry itself when it has no parts."""
    name = part or wanted
    try:
        found = rig_weights.read_part(LABEL, geo, name)
    except UserError as problem:
        raise WrongQuestion("%s: %s" % (node.path(), problem)) from None
    return found, (name if geo.findPrimAttrib(rig_weights.PART_ATTRIB) is not None else "")


def _read_skel(geo, node) -> Skel:
    try:
        info = rig_weights.read_skeleton(LABEL, geo)
    except UserError as problem:
        raise CouldNotAnswer("%s has no skeleton: %s" % (node.path(), problem)) from None
    return Skel(info.names, info.parents, np.array(info.positions, dtype=np.float64))


def _skeleton_of(path: str, part: Optional[str]):
    node = _node(path)
    geo, used = _part(_geometry(node), rig_weights.PART_SKELETON, node, part)
    return _read_skel(geo, node), Source(_scene(), node.path(), used), node


def _kind(node) -> str:
    return node.type().name()


# ---- the digests --------------------------------------------------------------------------------------------------------

def skeleton(path: str, rest: Optional[str] = None, part: Optional[str] = None, show_all: bool = False):
    """The ``skeleton`` digest: joints, parents and bone lengths of the skeleton at ``path`` (a Rig Skeleton node, or a Rig Fit
    node: its ``fitted_skeleton`` part), beside the Arma rest skeleton. ``rest`` is the node path of the Arma skeleton; by
    default a Rig Skeleton node is its own rest and a Rig Fit node takes its input 2."""
    skel, source, node = _skeleton_of(path, part)
    rest_skel, rest_source = None, ""
    if rest:
        rest_node = _node(rest)
        rest_skel = _read_skel(_geometry(rest_node), rest_node)
        rest_source = rest_node.path()
    elif "rig_skeleton" in _kind(node):
        rest_skel, rest_source = skel, node.path() + " (this node)"
    elif "rig_fit" in _kind(node) and _input(node, 1) is not None:
        rest_skel, rest_source = _read_skel(_input(node, 1), node), node.inputs()[1].path() + " (input 2)"
    return core.skeleton_page(skel, rest_skel, source, rest_source, show_all)


def skeleton_compare(path_a: str, path_b: str, part_a: Optional[str] = None, part_b: Optional[str] = None,
                     show_all: bool = False):
    """The ``skeleton-compare`` digest: skeleton B beside skeleton A, joint by joint and in body measures."""
    a, source_a, _ = _skeleton_of(path_a, part_a)
    b, source_b, _ = _skeleton_of(path_b, part_b)
    return core.compare_page(a, b, source_a, source_b, show_all)


def _bones() -> List[str]:
    """The bone names P3D Export recognises (the man skeleton): a selection is a bone selection when it matches one."""
    return list(OFP2_MANSKELETON_BONES)


def _read_weights(geo, label: str):
    """``(weights, how, zero members)``: the bone weights as P3D Export reads them, or the capture weights when there are
    no bone selections. ``weights`` is None when the mesh has neither."""
    bones = _bones()
    try:
        found = rig_weights.read_selections(LABEL, geo, bones)
        if found.groups:
            return found.weights, "bone selections", found.zero_members
        if geo.findPointAttrib(rig_weights.CAPTURE) is not None:
            return rig_weights.read_capture(LABEL, geo, bones)[0], "capture weights (boneCapture)", 0
    except UserError as problem:
        raise CouldNotAnswer("%s: %s" % (label, problem)) from None
    return None, "", 0


def _proxy_mask(geo) -> np.ndarray:
    """The points only a packed primitive uses (a proxy's three points): P3D Export does not write them as mesh points."""
    from .nodes.weight_cleanup import _proxy_points
    mask = np.zeros(len(geo.points()), dtype=bool)
    mask[sorted(_proxy_points(geo))] = True
    return mask


def weights(path: str, show_all: bool = False):
    """The ``weights`` digest: points per bone, unweighted points, points over 4 bones, the most influences and the per-point
    sum off 1 of the mesh at ``path``, read as P3D Export reads them."""
    node = _node(path)
    geo = _geometry(node)
    matrix, how, zero = _read_weights(geo, node.path())
    if matrix is None:
        raise CouldNotAnswer("%s has no bone selections (a group named like a bone of the man skeleton) and no boneCapture"
                             % node.path())
    return core.weights_page(matrix, _bones(), Source(_scene(), node.path()), how, zero, _proxy_mask(geo), MAX_INFLUENCES,
                             SUM_LIMIT, show_all)


def _mesh_of(path: str, part: Optional[str]):
    node = _node(path)
    geo, used = _part(_geometry(node), rig_weights.PART_BODY, node, part)
    positions, tris = rig_weights.read_mesh(geo)
    if not len(tris):
        raise CouldNotAnswer("%s has no polygons" % node.path())
    return geo, positions, tris, Source(_scene(), node.path(), used), node


def _group(geo, name: str, node, explicit: bool):
    """The mask of the point group ``name``: None when the mesh has no such group (a WrongQuestion when it was asked for)."""
    if not name:
        return None
    if geo.findPointGroup(name) is None:
        if explicit:
            raise WrongQuestion("%s has no point group %s" % (node.path(), name))
        return None
    return rig_weights.group_mask(LABEL, geo, name, node.path())


def deform_compare(path_a: str, path_b: str, by: str = "region", group: str = "", body: str = "body",
                   part_a: Optional[str] = None, part_b: Optional[str] = None):
    """The ``deform-compare`` digest: the edge-length ratio of mesh B (``path_b``) against mesh A, per body region (or bone) by
    the dominant bone in A, for the ``body`` group and the rest, and for the group ``group`` with its size before and after.
    The two meshes must have the same topology."""
    geo_a, pos_a, tris_a, source_a, node_a = _mesh_of(path_a, part_a)
    _, pos_b, tris_b, source_b, node_b = _mesh_of(path_b, part_b)
    if pos_a.shape != pos_b.shape or tris_a.shape != tris_b.shape or not np.array_equal(tris_a, tris_b):
        raise CouldNotAnswer("the meshes do not have the same topology (A: %d points, %d triangles; B: %d points, %d triangles)"
                             % (len(pos_a), len(tris_a), len(pos_b), len(tris_b)))
    matrix, _, _ = _read_weights(geo_a, node_a.path())
    return core.deform_page(pos_a, pos_b, tris_a, matrix, _bones(), source_a, source_b, by,
                            _group(geo_a, body, node_a, False), body, _group(geo_a, group, node_a, True), group)


def _landmark_points(geo, node) -> dict:
    out = {}
    names = geo.pointStringAttribValues("name") if geo.findPointAttrib("name") is not None else ()
    points = np.array(geo.pointFloatAttribValues("P"), dtype=np.float64).reshape(-1, 3)
    for name, position in zip(names, points):
        if name in LANDMARK_NAMES:
            out[name] = position
    return out


def landmarks(path: str, mesh: Optional[str] = None, group: str = ""):
    """The ``landmarks`` digest: the landmarks of a Rig Fit node (its ``landmarks`` part) or a Rig Landmarks node, beside the
    matching measures on the mesh (the points of ``group`` on the node's input 1, or on the node ``mesh``) and on the Arma
    sample (the template body and skeleton on inputs 3 and 2)."""
    node = _node(path)
    kind = _kind(node)
    fitted = None
    if "rig_fit" in kind:
        geo, used = _part(_geometry(node), rig_weights.PART_LANDMARKS, node, None)
        positions = _landmark_points(geo, node)
        fitted_geo, _ = _part(_geometry(node), rig_weights.PART_SKELETON, node, None)
        fitted = _read_skel(fitted_geo, node)
        mesh_geo = _input(node, 0)
    elif "rig_landmarks" in kind:
        mesh_geo = _geometry(node)
        positions, used = landmark_io.read_landmarks(mesh_geo), ""
    else:
        raise WrongQuestion("%s is a %s node; the landmarks digest reads a Rig Fit or a Rig Landmarks node" % (node.path(), kind))
    label = "node's input 1"
    if mesh:
        mesh_node = _node(mesh)
        mesh_geo, label = _geometry(mesh_node), mesh_node.path()
    points = None
    if mesh_geo is not None:
        mask = _group(mesh_geo, group, node, True) if group else None
        every, _ = rig_weights.read_mesh(mesh_geo)
        points = every[mask] if mask is not None else every
        label += ", group %s" % group if group else ", all points"
    else:
        label = "none"
    side = SampleSide()
    skeleton_geo, sample_geo = _input(node, 1), _input(node, 2)
    if skeleton_geo is None or sample_geo is None:
        side.note = "input 2 or 3 of the node is not connected"
    else:
        try:
            found = landmark_io.sample_side(LABEL, skeleton_geo, sample_geo)
            side = SampleSide(found.template, Skel(found.skeleton.names, found.skeleton.parents, found.rest))
        except UserError as problem:
            side.note = str(problem)
    return core.landmarks_page(positions, points, side, Source(_scene(), node.path(), used), label, fitted)


def _body_points(geo, group: str, node, explicit: bool = False):
    """The points that belong to polygons, only those of the point group ``group`` when the mesh has it."""
    positions, tris = rig_weights.read_mesh(geo)
    used = np.zeros(len(positions), dtype=bool)
    used[np.unique(tris)] = True
    mask = _group(geo, group, node, explicit)
    return positions[used & mask] if mask is not None else positions[used]


def feet(paths: List[str], part: Optional[str] = None, group: str = "body"):
    """The ``feet`` digest: the sole pitch, heel and toe-tip of each foot of the bodies at ``paths``. A Rig Fit node gives
    three rows (your body: input 1, ``group``; the wrapped body: its ``fitted_body`` part; the Arma sample: input 3); any other
    node gives one row (its points in ``group``, or all of them when it has no such group; ``part`` picks a Rig Fit part)."""
    entries, sources = [], []
    front = 1.0
    for path in paths:
        node = _node(path)
        if "rig_fit" in _kind(node) and not part:
            owner, sample = _input(node, 0), _input(node, 2)
            wrapped, _ = _part(_geometry(node), rig_weights.PART_BODY, node, None)
            for label, geo, grp in (("your body", owner, group), ("wrapped body", wrapped, ""), ("Arma sample", sample, "")):
                if geo is None:
                    continue
                entries.append((label, _body_points(geo, grp, node)))
            sources.append(Source(_scene(), node.path(), "rows: your body, wrapped, sample"))
        else:
            geo, used = _part(_geometry(node), rig_weights.PART_BODY, node, part)
            entries.append((node.name() + (":" + used if used else ""), _body_points(geo, group, node)))
            sources.append(Source(_scene(), node.path(), used))
    return core.feet_page(entries, sources, front)


def wrap_distance(path: str, group: str = "body"):
    """The ``wrap-distance`` digest: how far the wrapped sample body of the Rig Fit node at ``path`` lies from your body (its
    input 1, the point group ``group``), per body region, and how far its heel and toe tips are."""
    node = _node(path)
    if "rig_fit" not in _kind(node):
        raise WrongQuestion("%s is a %s node; wrap-distance reads a Rig Fit node" % (node.path(), _kind(node)))
    wrapped_geo, used = _part(_geometry(node), rig_weights.PART_BODY, node, None)
    owner_geo = _input(node, 0)
    if owner_geo is None:
        raise CouldNotAnswer("%s has no mesh on input 1" % node.path())
    wrapped, wrapped_tris = rig_weights.read_mesh(wrapped_geo)
    owner, owner_tris = rig_weights.read_mesh(owner_geo)
    mask = _group(owner_geo, group, node, group != "body") if group else None
    if mask is not None:  # the triangles of the group only; the points keep their numbers
        owner_tris = owner_tris[mask[owner_tris].all(axis=1)]
    if not len(wrapped_tris) or not len(owner_tris):
        raise CouldNotAnswer("%s: the wrapped body or your body has no polygons" % node.path())
    matrix, _, _ = _read_weights(wrapped_geo, node.path())
    labels = core.deform.dominant_labels(matrix, _bones(), "region") if matrix is not None else None
    used_points = np.zeros(len(wrapped), dtype=bool)
    used_points[np.unique(wrapped_tris)] = True
    if labels is not None:
        labels = np.where(used_points, labels, "")
    return core.wrap_distance_page(wrapped, wrapped_tris, labels, owner, owner_tris, Source(_scene(), node.path(), used),
                                   Source(_scene(), node.path() + " input 1", "group " + group if group else ""))


# ---- the command line ---------------------------------------------------------------------------------------------------

DIGESTS = ("skeleton", "skeleton-compare", "deform-compare", "weights", "landmarks", "feet", "wrap-distance")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="a3ob_digest.py", description="Rig digests: figures beside their reference values; "
                                "no verdicts (docs/design/digests.md). Run with hython; the scene is read, never saved.",
                                epilog="exit 0 answered, 1 could not answer, 2 the question was written wrongly")
    p.add_argument("digest", choices=DIGESTS)
    p.add_argument("scene", help="the .hip/.hiplc/.hipnc file")
    p.add_argument("nodes", nargs="+", metavar="node", help="node path; skeleton-compare and deform-compare take A then B; feet takes any number")
    p.add_argument("--part", help="the Rig Fit part to read from the node (A for the compares)")
    p.add_argument("--part-b", help="the part of node B")
    p.add_argument("--rest", help="skeleton: the Arma skeleton node (default: this node if a Rig Skeleton, else Rig Fit input 2)")
    p.add_argument("--by", default="region", choices=("region", "bone"), help="deform-compare: rows per region or bone")
    p.add_argument("--group", default="", help="deform-compare, landmarks: a point group (deform-compare: its size before "
                   "and after; landmarks: the points the mesh measures run on); feet, wrap-distance: your body group "
                   "(default body)")
    p.add_argument("--body", default="body", help="deform-compare: the body group (default body)")
    p.add_argument("--mesh", help="landmarks: the node whose mesh the mesh measures read (default: the node's input 1)")
    p.add_argument("--all", action="store_true", help="skeleton, skeleton-compare, weights: list every joint or bone")
    return p


def run(argv: List[str]):
    """The page for a command line (``argv`` without the program name). Raises ``DigestError`` (``code`` 1 or 2)."""
    args = _parser().parse_args(argv)
    if not os.path.isfile(args.scene):
        raise WrongQuestion("there is no scene file %s" % args.scene)
    wanted = 2 if args.digest.endswith("compare") else 1
    if args.digest != "feet" and len(args.nodes) != wanted:
        raise WrongQuestion("%s takes %d node path%s, not %d" % (args.digest, wanted, "" if wanted == 1 else "s", len(args.nodes)))
    try:
        hou.hipFile.load(args.scene, suppress_save_prompt=True, ignore_load_warnings=True)
    except hou.OperationFailed as problem:
        raise CouldNotAnswer("the scene did not load: %s" % problem) from None
    first = args.nodes[0]
    if args.digest == "skeleton":
        return skeleton(first, args.rest, args.part, args.all)
    if args.digest == "skeleton-compare":
        return skeleton_compare(first, args.nodes[1], args.part, args.part_b, args.all)
    if args.digest == "deform-compare":
        return deform_compare(first, args.nodes[1], args.by, args.group, args.body, args.part, args.part_b)
    if args.digest == "weights":
        return weights(first, args.all)
    if args.digest == "feet":
        return feet(args.nodes, args.part, args.group or "body")
    if args.digest == "wrap-distance":
        return wrap_distance(first, args.group or "body")
    return landmarks(first, args.mesh, args.group)


def main(argv: Optional[List[str]] = None) -> int:
    """Print the page; return the exit code (0 answered, 1 could not answer, 2 the question was written wrongly)."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        page = run(argv)
    except core.DigestError as problem:
        print("digest: %s" % problem, file=sys.stderr)
        return problem.code
    print(page.text())
    notice = page.size_notice()
    if notice:
        print(notice, file=sys.stderr)
    return core.EXIT_ANSWERED

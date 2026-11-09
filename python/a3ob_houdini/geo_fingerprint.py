# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""A hash of what a ``hou.Geometry`` holds (task #94), so a node can tell that an input is the same as at its last cook.

Houdini cooks a node again whenever a node in front of it is dirty, even when that node gives the same geometry. A node
that is slow (Rig Fit) keeps its last result under this hash and returns it when the hash has not changed.

``hou.Geometry.data()`` is not usable for this: its bytes hold the time they were made and the attribute names in an order
that changes from cook to cook. This hash reads the contents instead, in bulk (a mesh with 26000 primitives takes about
30 ms):

- the number of points, vertices and primitives, and of each primitive type;
- the points of each vertex and the primitive of each vertex (the topology);
- every point, vertex, primitive and detail attribute: name, type, size and values, in the order of the names;
- every point and primitive group: name and members;
- the geometry inside every packed primitive, and its transform.

Not in the hash: whether a polygon is closed, group order, attribute options (type info such as "vector", the default).
"""

import hashlib

import numpy as np

try:
    import hou
except ImportError:  # the module is only used inside Houdini
    hou = None

__all__ = ["fingerprint"]

#: The primitive type names that hold other geometry.
_PACKED = ("PackedGeometry", "PackedDisk", "PackedFragment", "PackedAgent", "PackedDiskSequence", "AlembicRef", "PackedUSD")


def fingerprint(geo) -> bytes:
    """A 16-byte hash of the contents of ``geo`` (see the module text)."""
    digest = hashlib.blake2b(digest_size=16)
    _add(digest, geo)
    return digest.digest()


def _text(digest, *parts) -> None:
    digest.update(("\x1f".join(str(part) for part in parts) + "\x1e").encode("utf-8", "surrogatepass"))


def _numbers(digest, values, dtype) -> None:
    digest.update(np.asarray(values, dtype=dtype).tobytes())


def _add(digest, geo) -> None:
    points, prims = len(geo.points()), len(geo.prims())
    _text(digest, "counts", points, geo.vertexCount(), prims)
    for name in geo.primTypeNames():
        count = geo.countPrimType(name)
        if count:
            _text(digest, "type", name, count)
    if prims:
        _topology(digest, geo, points, prims)
    for kind, attribs, bulk in (("point", geo.pointAttribs(), _point), ("vertex", geo.vertexAttribs(), _vertex),
                                ("prim", geo.primAttribs(), _prim)):
        for attrib in sorted(attribs, key=lambda item: item.name()):
            _text(digest, kind, attrib.name(), attrib.dataType().name(), attrib.size(), attrib.isArrayType())
            bulk(digest, geo, attrib)
    for attrib in sorted(geo.globalAttribs(), key=lambda item: item.name()):
        _text(digest, "detail", attrib.name(), attrib.dataType().name(), attrib.size(), attrib.isArrayType(),
              repr(geo.attribValue(attrib)))
    for kind, groups in (("pointgroup", geo.pointGroups()), ("primgroup", geo.primGroups())):
        for group in sorted(groups, key=lambda item: item.name()):
            _text(digest, kind, group.name())
            members = group.points() if kind == "pointgroup" else group.prims()
            _numbers(digest, [member.number() for member in members], np.int64)
    if any(geo.countPrimType(name) for name in _PACKED):
        for prim in geo.prims():
            if isinstance(prim, hou.PackedPrim):
                _text(digest, "packed", prim.number(), repr(tuple(prim.fullTransform().asTuple())))
                if not hasattr(prim, "getEmbeddedGeometry"):  # a packed disk primitive: its file, not its geometry (task #97)
                    _text(digest, "disk", repr(sorted(prim.intrinsicValueDict().items())))
                    continue
                inner = prim.getEmbeddedGeometry()
                if inner is not None:
                    _add(digest, inner)


def _topology(digest, geo, points: int, prims: int) -> None:
    """The point and the primitive of every vertex. A copy gets two integer attributes that Attribute Promote carries to the
    vertices, where they are read in bulk (there is no other bulk way to the points of a vertex)."""
    work = hou.Geometry()
    work.copy(geo)
    work.addAttrib(hou.attribType.Point, "a3ob_fp_point", 0)
    work.setPointIntAttribValues("a3ob_fp_point", list(range(points)))
    work.addAttrib(hou.attribType.Prim, "a3ob_fp_prim", 0)
    work.setPrimIntAttribValues("a3ob_fp_prim", list(range(prims)))
    verb = hou.sopNodeTypeCategory().nodeVerb("attribpromote")
    for name, kind in (("a3ob_fp_point", 2), ("a3ob_fp_prim", 1)):
        verb.setParms({"inname": name, "inclass": kind, "outclass": 3, "method": 0, "deletein": 0})
        promoted = hou.Geometry()
        verb.execute(promoted, [work])
        work = promoted
        _numbers(digest, work.vertexIntAttribValues(name), np.int64)


def _values(digest, attrib, ints, floats, strings, lists) -> None:
    """Hash the values of one attribute with the bulk readers of its class (``ints`` and the rest are bound methods)."""
    name, kind = attrib.name(), attrib.dataType()
    if attrib.isArrayType():
        digest.update(repr(lists(name)).encode("utf-8", "surrogatepass"))
    elif kind == hou.attribData.Int:
        _numbers(digest, ints(name), np.int64)
    elif kind == hou.attribData.Float:
        _numbers(digest, floats(name), np.float64)
    elif kind == hou.attribData.String:
        _text(digest, *strings(name))
    else:  # a dictionary: slow, one by one (the data model has none)
        digest.update(repr(lists(name)).encode("utf-8", "surrogatepass"))


def _point(digest, geo, attrib) -> None:
    _values(digest, attrib, geo.pointIntAttribValues, geo.pointFloatAttribValues, geo.pointStringAttribValues,
            lambda name: [point.attribValue(attrib) for point in geo.points()])


def _vertex(digest, geo, attrib) -> None:
    _values(digest, attrib, geo.vertexIntAttribValues, geo.vertexFloatAttribValues, geo.vertexStringAttribValues,
            lambda name: [vertex.attribValue(attrib) for prim in geo.prims() for vertex in prim.vertices()])


def _prim(digest, geo, attrib) -> None:
    _values(digest, attrib, geo.primIntAttribValues, geo.primFloatAttribValues, geo.primStringAttribValues,
            lambda name: [prim.attribValue(attrib) for prim in geo.prims()])

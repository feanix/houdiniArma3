# M2 data-model revision 3 check (W1-W7): the per-prim LOD uid `a3ob_lod_uid` (D8).
# Run: "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" hcheck5_lod_uid.py
import os
import sys
import tempfile

import hou

print("Houdini", hou.applicationVersionString(), "Python", sys.version.split()[0])
cat = hou.sopNodeTypeCategory()
PRIM = hou.attribType.Prim
UID = "a3ob_lod_uid"


def make_lod(uid, offset=0.0, quads=2, with_attr=True):
    """A LOD geometry of `quads` separate quads, every prim stamped with `uid`; detail a3ob_lod_type."""
    g = hou.Geometry()
    a = g.addAttrib(PRIM, UID, 0) if with_attr else None
    g.addAttrib(hou.attribType.Global, "a3ob_lod_type", 0)
    for i in range(quads):
        pts = g.createPoints([(offset + i * 3 + dx, dy, 0.0) for dx, dy in ((0, 0), (1, 0), (1, 1), (0, 1))])
        pr = g.createPolygon()
        for p in pts:
            pr.addVertex(p)
        if with_attr:
            pr.setAttribValue(a, uid)
    return g


def uids(g):
    if g.findPrimAttrib(UID) is None:
        return None
    return [p.attribValue(UID) for p in g.prims() if not isinstance(p, hou.PackedPrim)]


def distinct_nonzero(g):
    """The validator rule (spec 17.1): distinct non-zero uids on the polygons of one LOD geometry."""
    values = uids(g)
    return set() if values is None else {v for v in values if v != 0}


# ---------------------------------------------------------------- W1 attribute type and range
g = hou.Geometry()
a = g.addAttrib(PRIM, UID, 0)
print("W1 dataType", a.dataType(), "size", a.size(), "default", a.defaultValue())
pr = g.createPolygon()
big = 2**31 - 1
pr.setAttribValue(a, big)
print("W1 2**31-1 round trip:", pr.attribValue(a) == big)
pr2 = g.createPolygon()
print("W1 a prim created after the attribute reads", pr2.attribValue(a), "(unstamped = 0)")

# ---------------------------------------------------------------- W2 Pack verb, Unpack verb (one LOD)
lod1 = make_lod(111, 0.0)
lod2 = make_lod(222, 20.0)
packs = []
for lod in (lod1, lod2):
    out = hou.Geometry()
    cat.nodeVerb("pack").execute(out, [lod])
    packs.append(out)
print("W2 packed prims per LOD", [len(p.prims()) for p in packs],
      "outer prim has the attribute:", [p.findPrimAttrib(UID) is not None for p in packs])
emb = packs[0].prims()[0].getEmbeddedGeometry()
print("W2 embedded geometry keeps uids:", uids(emb))
un = hou.Geometry()
cat.nodeVerb("unpack").execute(un, [packs[0]])
print("W2 Unpack of one packed LOD:", uids(un))

# ---------------------------------------------------------------- W3 Merge of packed LODs, then Unpack of both
both = hou.Geometry()
cat.nodeVerb("merge").execute(both, [packs[0], packs[1]])
print("W3 Merge of two packed LODs -> prims", len(both.prims()))
unb = hou.Geometry()
cat.nodeVerb("unpack").execute(unb, [both])
print("W3 Unpack of both: uids", uids(unb), "distinct", sorted(distinct_nonzero(unb)),
      "(detail a3ob_lod_type is now", "prim attr" if unb.findPrimAttrib("a3ob_lod_type") else
      ("detail" if unb.findGlobalAttrib("a3ob_lod_type") else "gone"), ")")

# ---------------------------------------------------------------- W4 Merge of two unpacked LODs
mg = hou.Geometry()
cat.nodeVerb("merge").execute(mg, [lod1, lod2])
print("W4 Merge of two unpacked LODs: uids", uids(mg), "distinct", sorted(distinct_nonzero(mg)))

# ---------------------------------------------------------------- W5 SOPs that rebuild or edit prims
for label, verb, parms in [
    ("blast first quad", "blast", {"group": "0", "grouptype": 4}),
    ("fuse::2.0", "fuse::2.0", {}),
    ("polyextrude::2.0 all", "polyextrude::2.0", {"dist": 0.5}),
]:
    out = hou.Geometry()
    try:
        v = cat.nodeVerb(verb)
        if parms:
            v.setParms(parms)
        v.execute(out, [lod1])
        print("W5", label, "-> prims", len(out.prims()), "uids", uids(out))
    except Exception as ex:
        print("W5", label, "failed:", type(ex).__name__, str(ex)[:80])

# copytopoints: copies of one LOD share the uid (two copies of one LOD are not "two uids")
pts = hou.Geometry()
pts.createPoints([(0, 0, 0), (50, 0, 0)])
cp = hou.Geometry()
try:
    cat.nodeVerb("copytopoints::2.0").execute(cp, [lod1, pts])
    print("W5 copytopoints::2.0 (2 copies): prims", len(cp.prims()), "distinct uids", sorted(distinct_nonzero(cp)))
except Exception as ex:
    print("W5 copytopoints failed:", type(ex).__name__, str(ex)[:80])

# ---------------------------------------------------------------- W6 an artist adds a prim to an imported LOD
edit = hou.Geometry()
edit.merge(lod1)
extra_pts = edit.createPoints([(9, 9, 0), (10, 9, 0), (10, 10, 0)])
extra = edit.createPolygon()
for p in extra_pts:
    extra.addVertex(p)
print("W6 HOM-created prim in a stamped LOD: uids", uids(edit), "distinct nonzero", sorted(distinct_nonzero(edit)))
nouid = make_lod(0, 40.0, with_attr=False)
print("W6 hand-authored LOD without the attribute: uids", uids(nouid), "distinct", sorted(distinct_nonzero(nouid)))
mixed = hou.Geometry()
cat.nodeVerb("merge").execute(mixed, [lod1, nouid])
print("W6 Merge(stamped LOD, hand-authored LOD): uids", uids(mixed), "distinct", sorted(distinct_nonzero(mixed)))

# ---------------------------------------------------------------- W7 bgeo save/load
path = os.path.join(tempfile.mkdtemp(), "uid.bgeo")
both.saveToFile(path)
back = hou.Geometry()
back.loadFromFile(path)
unb2 = hou.Geometry()
cat.nodeVerb("unpack").execute(unb2, [back])
print("W7 bgeo save/load of the packed P3D geometry, then Unpack:", uids(unb2))


# ---------------------------------------------------------------- uid generator (spec 3.5, lead revision)
import hashlib


def lod_uid(source_key, lod_index):
    """Deterministic positive int32, never 0: hash of the import's source key (importer node path) and LOD index."""
    digest = hashlib.blake2b(("%s|%d" % (source_key, lod_index)).encode("utf-8"), digest_size=8).digest()
    return 1 + int.from_bytes(digest, "little") % (2**31 - 1)


same = lod_uid("/obj/geo1/a3ob_import1", 3) == lod_uid("/obj/geo1/a3ob_import1", 3)
keys = [("/obj/geo%d/a3ob_import1" % node, lod) for node in range(1000) for lod in range(32)]
draw = [lod_uid(k, lod) for k, lod in keys]
print("W8 generator: deterministic", same, "| 1000 import nodes x 32 LODs =", len(draw), "uids, distinct", len(set(draw)),
      "min", min(draw), "max", max(draw), "all fit int32:", all(0 < v < 2**31 for v in draw))

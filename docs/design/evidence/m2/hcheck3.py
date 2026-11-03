# M2 data-model revision checks (V1-V9). Run: "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" hcheck3.py
# Does not hand a lone surrogate to hou (that segfaults hython 22.0.466, see review evidence rv_sur.py); V1 only
# checks that the converted (latin-1) form is safe.
import sys
import hou

print("Houdini", hou.applicationVersionString(), "Python", sys.version.split()[0])
cat = hou.sopNodeTypeCategory()


def tri(geo, pts):
    ps = geo.createPoints(pts)
    pr = geo.createPolygon()
    for p in ps:
        pr.addVertex(p)
    return pr


# ---------------------------------------------------------------- V1 string boundary
def to_h(s):  # M1 str (cp1252 + surrogateescape) -> hou-safe str (latin-1 code points)
    return s.encode("cp1252", "surrogateescape").decode("latin-1")


def from_h(h):
    return h.encode("latin-1").decode("cp1252", "surrogateescape")


all_bytes = bytes(range(1, 256))
m1 = all_bytes.decode("cp1252", "surrogateescape")
print("V1 M1 str has lone surrogates:", sum(1 for c in m1 if 0xD800 <= ord(c) <= 0xDFFF))
h = to_h(m1)
print("V1 converted max code point:", hex(max(map(ord, h))), "bijective back to M1 str:", from_h(h) == m1,
      "bytes equal:", from_h(h).encode("cp1252", "surrogateescape") == all_bytes)
g = hou.Geometry()
pr = tri(g, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
sa = g.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
pr.setAttribValue(sa, h)
back = pr.attribValue(sa)
g.addArrayAttrib(hou.attribType.Global, "a3ob_prop_values", hou.attribData.String)
g.setGlobalAttribValue("a3ob_prop_values", [h, "x" + h])
arr = g.attribValue("a3ob_prop_values")
print("V1 prim string round trip:", back == h, "detail string[] round trip:", list(arr) == [h, "x" + h])
bad_groups = []
for b in range(1, 256):
    name = to_h(bytes([0x61, b, 0x62]).decode("cp1252", "surrogateescape"))
    enc = hou.text.encodeAttrib(name)
    try:
        grp = g.createPointGroup(enc)
        ok = hou.text.decodeAttrib(grp.name()) == name
        grp.destroy()
    except hou.OperationFailed:
        ok = False
    if not ok:
        bad_groups.append(b)
print("V1 group names a<byte>b for bytes 1..255 that fail encode/create/decode:", bad_groups)
try:
    hou.text.encodeAttrib("\udc81")
    print("V1 encodeAttrib(lone surrogate): no error")
except Exception as ex:
    print("V1 encodeAttrib(lone surrogate):", type(ex).__name__)

# ---------------------------------------------------------------- V2 name edge cases
print("V2 encodeAttrib('') =", repr(hou.text.encodeAttrib("")))
try:
    g.createPointGroup(hou.text.encodeAttrib(""))
    print("V2 createPointGroup('') ok")
except Exception as ex:
    print("V2 createPointGroup('') fails:", type(ex).__name__)
print("V2 encodeAttrib('__foo') =", repr(hou.text.encodeAttrib("__foo")),
      "encodeAttrib('Head') =", repr(hou.text.encodeAttrib("Head")),
      "encodeAttrib('head') =", repr(hou.text.encodeAttrib("head")))
g.createPointGroup("Head")
g.createPointGroup("head")
print("V2 groups Head and head coexist:", sorted(x.name() for x in g.pointGroups()))

# ---------------------------------------------------------------- V3 unpack bakes transform; mirror keeps winding
inner = hou.Geometry()
ipr = tri(inner, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
na = inner.addAttrib(hou.attribType.Vertex, "N", (0.0, 0.0, 0.0), transform_as_normal=True)
for v in ipr.vertices():
    v.setAttribValue(na, (0.0, 0.0, -1.0))
print("V3 inner prim.normal", tuple(ipr.normal()))
for label, m in [("translate+5x", hou.hmath.buildTranslate(5, 0, 0)), ("mirrorX", hou.hmath.buildScale(-1, 1, 1))]:
    outer = hou.Geometry()
    pk = outer.createPacked("PackedGeometry")
    pk.setEmbeddedGeometry(inner.freeze())
    pk.setTransform(m)
    un = hou.Geometry()
    cat.nodeVerb("unpack").execute(un, [outer])
    q = un.prims()[0]
    print("V3", label, "P", [tuple(v.point().position()) for v in q.vertices()], "prim.normal", tuple(q.normal()),
          "N", [tuple(v.attribValue("N")) for v in q.vertices()][0])

# ---------------------------------------------------------------- V4 nested proxy: transform composition + embedded attrs
lod = hou.Geometry()
tri(lod, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
pinner = hou.Geometry()
ptri = tri(pinner, [(0, 0, 0), (0, 2, 0), (0, 0, -1)])
ipa = pinner.addAttrib(hou.attribType.Prim, "a3ob_proxy_path", "")
iia = pinner.addAttrib(hou.attribType.Prim, "a3ob_proxy_index", 0)
ptri.setAttribValue(ipa, "\\a3\\inner")
ptri.setAttribValue(iia, 7)
ppk = lod.createPacked("PackedGeometry")
ppk.setEmbeddedGeometry(pinner.freeze())
P = hou.hmath.buildRotate(0, 90, 0) * hou.hmath.buildTranslate(1, 2, 3)
ppk.setTransform(P)
opa = lod.addAttrib(hou.attribType.Prim, "a3ob_proxy_path", "")
ppk.setAttribValue(opa, "\\a3\\outer")
lod.addAttrib(hou.attribType.Global, "a3ob_lod_type", 0)
T = hou.hmath.buildTranslate(10, 0, 0)
p3d = hou.Geometry()
lodpk = p3d.createPacked("PackedGeometry")
lodpk.setEmbeddedGeometry(lod.freeze())
lodpk.setTransform(T)
un = hou.Geometry()
cat.nodeVerb("unpack").execute(un, [p3d])
packs = [p for p in un.prims() if isinstance(p, hou.PackedPrim)]
ft = packs[0].fullTransform()
expect = P * T
err = max(abs(ft.at(i, j) - expect.at(i, j)) for i in range(4) for j in range(4))
print("V4 after unpacking the LOD (default parms): nested packed prims", len(packs), "fullTransform == P*T err", err,
      "outer a3ob_proxy_path kept on nested prim:", packs[0].attribValue("a3ob_proxy_path") if un.findPrimAttrib("a3ob_proxy_path") else None,
      "detail a3ob_lod_type kept:", un.findGlobalAttrib("a3ob_lod_type") is not None)
un2 = hou.Geometry()
cat.nodeVerb("unpack").execute(un2, [un])
tris = [p for p in un2.prims() if not isinstance(p, hou.PackedPrim)]
print("V4 after unpacking the proxy too (default parms, transfer_attributes=''): polygons", len(tris),
      "embedded a3ob_proxy_path/index values", [(p.attribValue("a3ob_proxy_path"), p.attribValue("a3ob_proxy_index"))
                                               for p in tris if un2.findPrimAttrib("a3ob_proxy_index")])

# ---------------------------------------------------------------- V5 fuse shares the pack point with a mesh point
fl = hou.Geometry()
tri(fl, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
fpk = fl.createPacked("PackedGeometry")
fpk.setEmbeddedGeometry(pinner.freeze())
fpk.setTransform(hou.hmath.identityTransform())
fo = hou.Geometry()
cat.nodeVerb("fuse::2.0").execute(fo, [fl])
used_by_poly = set()
used_by_pack = set()
for p in fo.prims():
    s = used_by_pack if isinstance(p, hou.PackedPrim) else used_by_poly
    for v in p.vertices():
        s.add(v.point().number())
print("V5 fuse::2.0 points", len(fl.points()), "->", len(fo.points()), "pack points", sorted(used_by_pack),
      "shared with polygons", sorted(used_by_pack & used_by_poly),
      "pack-only points", sorted(used_by_pack - used_by_poly))

# ---------------------------------------------------------------- V6 pack defaults and Pack by Name fragments
fg = hou.Geometry()
nm = fg.addAttrib(hou.attribType.Prim, "name", "")
for k in range(2):
    tri(fg, [(k, 0, 0), (k + 1, 0, 0), (k, 1, 0)]).setAttribValue(nm, "piece%d" % k)
fg.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
pv = cat.nodeVerb("pack")
print("V6 pack verb packbyname default:", pv.parms().get("packbyname"))
o0 = hou.Geometry()
pv.execute(o0, [fg])
print("V6 default pack: prims", [p.intrinsicValue("typename") for p in o0.prims()])
pv2 = cat.nodeVerb("pack")
pv2.setParms({"packbyname": 1})
o1 = hou.Geometry()
pv2.execute(o1, [fg])
print("V6 packbyname=1: prims", [p.intrinsicValue("typename") for p in o1.prims()])
for p in o1.prims():
    names = p.intrinsicNames()
    info = {k: p.intrinsicValue(k) for k in names if k in ("typename", "fragmentname", "fragmentattribute",
                                                             "packedprimname", "packedprimitivename",
                                                             "sharedgeometryid", "geometryid", "unexpandedfilename")}
    one = hou.Geometry()
    single = hou.Geometry()
    single.merge(o1)
    keep = [q for q in single.prims() if q.number() != p.number()]
    single.deletePrims(keep)
    cat.nodeVerb("unpack").execute(one, [single])
    print("   fragment", info, "unpacked prims", len(one.prims()), "detail a3ob_lod_type",
          one.attribValue("a3ob_lod_type") if one.findGlobalAttrib("a3ob_lod_type") else None)
print("V6 fragment intrinsic names:", o1.prims()[0].intrinsicNames())

# ---------------------------------------------------------------- V7 weight attribute default for new points
wg = hou.Geometry()
tri(wg, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
wa = wg.addAttrib(hou.attribType.Point, "a3ob_w_head", 1.0)
wg.points()[0].setAttribValue(wa, 0.25)
newp = wg.createPoint()
print("V7 addAttrib default 1.0: new HOM point weight", newp.attribValue(wa))
try:
    sv = cat.nodeVerb("subdivide")
    so = hou.Geometry()
    sv.execute(so, [wg])
    vals = sorted(set(round(p.attribValue("a3ob_w_head"), 4) for p in so.points()))
    print("V7 subdivide new points interpolate weights:", vals[:6])
except Exception as ex:
    print("V7 subdivide:", type(ex).__name__, ex)

# ---------------------------------------------------------------- V8 merge of two unpacked LODs
a = hou.Geometry()
tri(a, [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
a.addAttrib(hou.attribType.Global, "a3ob_lod_type", 0)
a.addArrayAttrib(hou.attribType.Global, "a3ob_source_counts", hou.attribData.Int)
a.setGlobalAttribValue("a3ob_source_counts", [3, 1])
b = hou.Geometry()
tri(b, [(5, 0, 0), (6, 0, 0), (5, 1, 0)])
b.addAttrib(hou.attribType.Global, "a3ob_lod_type", 6)
b.addArrayAttrib(hou.attribType.Global, "a3ob_source_counts", hou.attribData.Int)
b.setGlobalAttribValue("a3ob_source_counts", [3, 1])
m = hou.Geometry()
cat.nodeVerb("merge").execute(m, [a, b])
print("V8 merge of two unpacked LODs: lod_type", m.attribValue("a3ob_lod_type"), "source_counts",
      m.attribValue("a3ob_source_counts"), "current counts", (len(m.points()), len(m.prims())))

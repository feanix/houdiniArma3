"""Headless hython checks for docs/design/houdini-data-model.md (M2).
Run: "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" hcheck_datamodel.py
Each section prints a tag T<n> so the spec can cite it."""
import os, sys, tempfile
import hou

print("houdini", hou.applicationVersionString(), "python", sys.version.split()[0])
OUT = os.path.dirname(os.path.abspath(__file__))

def tri_geo(order):
    g = hou.Geometry()
    pts = [g.createPoint() for _ in range(3)]
    for p, pos in zip(pts, [(0, 0, 0), (1, 0, 0), (0, 1, 0)]):
        p.setPosition(pos)
    poly = g.createPolygon()
    for i in order:
        poly.addVertex(pts[i])
    return g, poly

# T1 winding: points p0=(0,0,0) p1=(1,0,0) p2=(0,1,0); algebraic (p1-p0)x(p2-p0) = +Z
g, poly = tri_geo([0, 1, 2])
print("T1 prim.normal() for vertex order 0,1,2 (algebraic cross +Z):", tuple(poly.normal()))
g2, poly2 = tri_geo([0, 2, 1])
print("T1 prim.normal() for vertex order 0,2,1:", tuple(poly2.normal()))

# T1b same via a SOP network: Add SOP polygon + Normal SOP (point normals)
obj = hou.node("/obj")
geo = obj.createNode("geo", "t1")
add = geo.createNode("add")
add.parm("points").set(3)
for i, pos in enumerate([(0, 0, 0), (1, 0, 0), (0, 1, 0)]):
    add.parmTuple("pt%d" % i).set(pos)
    add.parm("usept%d" % i).set(1)
add.parm("prims").set(1)
add.parm("prim0").set("0 1 2")
nrm = geo.createNode("normal")
nrm.setInput(0, add)
nrm.parm("type").set(0)  # points
gg = nrm.geometry()
print("T1b Normal SOP N on add-polygon '0 1 2':", gg.points()[0].attribValue("N") if gg.findPointAttrib("N") else gg.findVertexAttrib("N") and gg.prims()[0].vertices()[0].attribValue("N"))
rev = geo.createNode("reverse"); rev.setInput(0, add)
print("T1c Reverse SOP vertex order:", [v.point().number() for v in rev.geometry().prims()[0].vertices()])

# T2 attribute / group names
g = hou.Geometry(); g.createPoint(); g.createPolygon()
names = ["head", "pos_driver", "-proxies", "a b", "x.y", "1abc", "proxy:\\a3\\characters_f\\proxies\\backpack.001", "Component01", "xn__abc", "ň"]
for n in names:
    res = []
    try:
        a = g.addAttrib(hou.attribType.Point, n, 0.0); res.append("attrib OK as %r" % a.name())
    except Exception as e:
        res.append("attrib FAIL %s" % type(e).__name__)
    try:
        grp = g.createPointGroup(n); res.append("ptgroup OK as %r" % grp.name())
    except Exception as e:
        res.append("ptgroup FAIL %s" % type(e).__name__)
    enc = hou.text.encodeAttrib(n)
    res.append("encodeAttrib=%r decode_ok=%s" % (enc, hou.text.decodeAttrib(enc) == n))
    print("T2", repr(n), "|", "; ".join(res))
long = "proxy:\\" + "\\".join(["verylongfoldername%02d" % i for i in range(12)]) + "\\x.001"
enc = hou.text.encodeAttrib(long)
grp = g.createPointGroup(enc)
print("T2 long name len", len(long), "encoded len", len(enc), "group created:", grp.name() == enc, "decode ok", hou.text.decodeAttrib(enc) == long)

# T3 point group and prim group with same name; point attrib with same name as group
g = hou.Geometry(); p = g.createPoint(); pr = g.createPolygon(); pr.addVertex(p)
a = g.createPointGroup("spine1"); b = g.createPrimGroup("spine1")
g.addAttrib(hou.attribType.Point, "spine1", 0.0)
print("T3 same-name point+prim group + point attrib:", a.name(), b.name(), [x.name() for x in g.pointAttribs()])

# T4 attribute kinds: detail dict, string array, int array, vertex uv, 64-bit?
g = hou.Geometry()
d = g.addAttrib(hou.attribType.Global, "a3ob_props", {})
g.setGlobalAttribValue("a3ob_props", {"lodnoshadow": "1"})
sa = g.addArrayAttrib(hou.attribType.Global, "a3ob_prop_keys", hou.attribData.String)
g.setGlobalAttribValue("a3ob_prop_keys", ["autocenter", "autocenter", "buoyancy"])
ia = g.addArrayAttrib(hou.attribType.Global, "a3ob_blob", hou.attribData.Int)
g.setGlobalAttribValue("a3ob_blob", [0, 255, 4294967295 & 0x7fffffff])
fa = g.addAttrib(hou.attribType.Global, "a3ob_res", 0.0)
g.setGlobalAttribValue("a3ob_res", 1e13 + 0.5)
print("T4 dict:", g.attribValue("a3ob_props"), "str[]:", g.attribValue("a3ob_prop_keys"), "int[]:", g.attribValue("a3ob_blob"))
print("T4 float detail precision:", fa.dataType(), "numericDataPrecision" if hasattr(fa, "numericDataPrecision") else "", "value back", repr(g.attribValue("a3ob_res")))
for meth in ("precision", "numericDataPrecision", "dataTypeSize"):
    if hasattr(fa, meth):
        print("T4 Attrib.%s exists ->" % meth, getattr(fa, meth)())
# int attribute range: u32 flags
ga = g.addAttrib(hou.attribType.Global, "a3ob_u32", 0)
for v in (0x02000000, 0xfe000000, 0xffffffff):
    try:
        g.setGlobalAttribValue("a3ob_u32", v); print("T4 int store", hex(v), "->", g.attribValue("a3ob_u32"))
    except Exception as e:
        print("T4 int store", hex(v), "FAIL", type(e).__name__, e)
for v in (-33554432, -1):
    g.setGlobalAttribValue("a3ob_u32", v); print("T4 int store signed", v, "->", g.attribValue("a3ob_u32"), "&0xffffffff =", hex(g.attribValue("a3ob_u32") & 0xffffffff))

# T4b 64-bit attrib via Attribute Create SOP precision, then read back from HOM
ac = geo.createNode("attribcreate")
ac.setInput(0, add)
print("T4b attribcreate parms with 'prec':", [p.name() for p in ac.parms() if "prec" in p.name()][:6])
try:
    ac.parm("name1").set("uv64"); ac.parm("class1").set("vertex"); ac.parm("size1").set(3)
    ac.parm("precision1").set("fpreal64") if ac.parm("precision1") else None
    ac.parm("value1v1").set(0.1)
    att = ac.geometry().findVertexAttrib("uv64")
    print("T4b attribcreate uv64 vertex:", att and (att.dataType(), att.size(), getattr(att, "numericDataPrecision", lambda: None)() if hasattr(att, "numericDataPrecision") else ""), "value", repr(ac.geometry().prims()[0].vertices()[0].attribValue("uv64")))
    print("T4b precision1 menu:", ac.parm("precision1").menuItems() if ac.parm("precision1") else None)
except Exception as e:
    print("T4b FAIL", type(e).__name__, e)

# T4c float32 round-trip of UV flip
import struct
def f32(x): return struct.unpack("<f", struct.pack("<f", x))[0]
bad = 0; total = 0
import random
random.seed(1)
for _ in range(100000):
    v = f32(random.random())
    total += 1
    if f32(1 - f32(1 - v)) != v: bad += 1
print("T4c f32 storage: v != f32(1-f32(1-v)) for %d of %d random v in [0,1)" % (bad, total))
bad64 = sum(1 for _ in range(100000) if (lambda v: f32(1 - (1 - v)) != v)(f32(random.random())))
print("T4c f64 storage: mismatches %d of 100000" % bad64)

# T5 vertex N and uv on a polygon, typeinfo
g = hou.Geometry(); pts = [g.createPoint() for _ in range(3)]
poly = g.createPolygon(); [poly.addVertex(p) for p in pts]
na = g.addAttrib(hou.attribType.Vertex, "N", (0.0, 0.0, 0.0))
uva = g.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
print("T5 vertex N typeinfo:", na.qualifier() if hasattr(na, "qualifier") else "", "isTransformedAsNormal", na.isTransformedAsNormal(), "| uv qualifier:", uva.qualifier() if hasattr(uva, "qualifier") else "")
ma = g.addAttrib(hou.attribType.Point, "mass", 0.0)
print("T5 point mass created ok:", ma.name())

# T6 edge groups: create, save bgeo, reload; survive Transform SOP?
g = hou.Geometry(); pts = [g.createPoint() for _ in range(4)]
for p, pos in zip(pts, [(0,0,0),(1,0,0),(1,1,0),(0,1,0)]): p.setPosition(pos)
poly = g.createPolygon(); [poly.addVertex(p) for p in pts]
eg = g.createEdgeGroup("a3ob_sharp")
edge = g.findEdge(pts[0], pts[1]); eg.add(edge)
fn = os.path.join(OUT, "t6_edge.bgeo")
g.saveToFile(fn)
g2 = hou.Geometry(); g2.loadFromFile(fn)
print("T6 edge groups after bgeo reload:", [(e.name(), len(e.edges())) for e in g2.edgeGroups()])
fsop = geo.createNode("file"); fsop.parm("file").set(fn.replace("\\", "/"))
xf = geo.createNode("xform"); xf.setInput(0, fsop); xf.parm("ry").set(30)
fu = geo.createNode("fuse"); fu.setInput(0, xf)
pd = geo.createNode("polyextrude::2.0") if hou.nodeType(hou.sopNodeTypeCategory(), "polyextrude::2.0") else geo.createNode("polyextrude")
pd.setInput(0, xf)
for n in (xf, fu, pd):
    try:
        print("T6 edge groups after %s:" % n.type().name(), [(e.name(), len(e.edges())) for e in n.geometry().edgeGroups()])
    except Exception as e:
        print("T6", n.type().name(), "FAIL", e)

# T7 packed geometry: create in Python, embedded detail attrs, transform, unpack behaviour
inner = hou.Geometry()
ip = [inner.createPoint() for _ in range(3)]
for p, pos in zip(ip, [(0,0,0),(0,2,0),(0,0,-1)]): p.setPosition(pos)
ipoly = inner.createPolygon(); [ipoly.addVertex(p) for p in ip]
inner.addAttrib(hou.attribType.Global, "a3ob_lod_signature", 0.0)
inner.setGlobalAttribValue("a3ob_lod_signature", 1e15)
inner.createPointGroup("spine3").add(ip[0])
outer = hou.Geometry()
try:
    pk = outer.createPacked("PackedGeometry")
    pk.setEmbeddedGeometry(inner.freeze())
    m = hou.hmath.buildTranslate(1, 2, 3)
    pk.setTransform(hou.hmath.buildRotate(0, 90, 0) * m)
    print("T7 createPacked('PackedGeometry') ok; prim type:", pk.type(), "typename intrinsic:", pk.intrinsicValue("typename"))
    print("T7 fullTransform:", pk.fullTransform().asTuple())
    emb = pk.getEmbeddedGeometry()
    print("T7 embedded detail attr survives:", emb.attribValue("a3ob_lod_signature"), "groups", [x.name() for x in emb.pointGroups()])
    print("T7 intrinsic names:", [n for n in pk.intrinsicNames() if "trans" in n or "pivot" in n or n == "typename"])
    fn7 = os.path.join(OUT, "t7_packed.bgeo"); outer.saveToFile(fn7)
    f7 = geo.createNode("file"); f7.parm("file").set(fn7.replace("\\", "/"))
    up = geo.createNode("unpack"); up.setInput(0, f7)
    ug = up.geometry()
    print("T7 Unpack SOP: detail attribs", [a.name() for a in ug.globalAttribs()], "point groups", [x.name() for x in ug.pointGroups()], "P0", tuple(ug.points()[0].position()))
    print("T7 Unpack parms:", [p.name() for p in up.parms()][:12])
except Exception as e:
    import traceback; traceback.print_exc()

# T7b Pack SOP on a box with a detail attribute; merge two packed; pivot/transform
box = geo.createNode("box")
wr = geo.createNode("attribwrangle"); wr.setInput(0, box); wr.parm("class").set(0); wr.parm("snippet").set('f@a3ob_lod_resolution = 1.0;')
pack = geo.createNode("pack"); pack.setInput(0, wr)
pg = pack.geometry(); pp = pg.prims()[0]
print("T7b Pack SOP prim:", pp.intrinsicValue("typename"), "embedded detail:", [a.name() for a in pp.getEmbeddedGeometry().globalAttribs()], "pivot", pp.intrinsicValue("pivot"), "fullTransform==identity:", pp.fullTransform() == hou.hmath.identityTransform())
box2 = geo.createNode("box"); wr2 = geo.createNode("attribwrangle"); wr2.setInput(0, box2); wr2.parm("class").set(0); wr2.parm("snippet").set('f@a3ob_lod_resolution = 2.0;')
mg = geo.createNode("merge"); mg.setInput(0, wr); mg.setInput(1, wr2)
print("T7c Merge of two unpacked LODs: detail a3ob_lod_resolution =", mg.geometry().attribValue("a3ob_lod_resolution"), "(second input's value lost)")
pack2 = geo.createNode("pack"); pack2.setInput(0, wr2)
mg2 = geo.createNode("merge"); mg2.setInput(0, pack); mg2.setInput(1, pack2)
print("T7d Merge of two packed LODs: embedded values", [p.getEmbeddedGeometry().attribValue("a3ob_lod_resolution") for p in mg2.geometry().prims()])
up2 = geo.createNode("unpack"); up2.setInput(0, pack)
print("T7e Unpack of Pack(box with detail attr): detail attribs", [a.name() for a in up2.geometry().globalAttribs()])

# T8 matrix convention: row vectors
v = hou.Vector3(1, 0, 0) * hou.hmath.buildTranslate(0, 0, 5)
print("T8 Vector3(1,0,0) * buildTranslate(0,0,5) =", tuple(v), "(row-vector convention: translation in last row)")
m = hou.Matrix4(((1,0,0,0),(0,1,0,0),(0,0,1,0),(7,8,9,1)))
print("T8 Vector3(0,0,0) * Matrix4 rows X,Y,Z,T(7,8,9) =", tuple(hou.Vector3(0,0,0) * m))

# T9 encodeAttrib for all selection names in the character fixture (sample)
for n in ["-proxies", "proxy:\\a3\\characters_f\\proxies\\backpack.001", "Component01", "pos driver", "Light_L", "zbytek"]:
    print("T9", repr(n), "->", hou.text.encodeAttrib(n))

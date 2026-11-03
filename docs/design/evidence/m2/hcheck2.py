"""Second headless hython check (M2 spec): 64-bit attributes, edge-group order/direction,
Normal SOP vs prim.normal, Unpack detail_attributes default, Attrib typeinfo options."""
import os, struct
import hou

OUT = os.path.dirname(os.path.abspath(__file__))
print("U1 Geometry attrib-creating methods:", [m for m in dir(hou.Geometry) if "ttrib" in m and ("add" in m.lower() or "create" in m.lower())])
print("U1 addAttrib doc head:", (hou.Geometry.addAttrib.__doc__ or "")[:300].replace("\n", " "))
print("U1 numericData members:", [m for m in dir(hou.numericData) if not m.startswith("_")])

geo = hou.node("/obj").createNode("geo", "u")
add = geo.createNode("add")
add.parm("points").set(3)
for i, pos in enumerate([(0, 0, 0), (1, 0, 0), (0, 1, 0)]):
    add.parmTuple("pt%d" % i).set(pos); add.parm("usept%d" % i).set(1)
add.parm("prims").set(1); add.parm("prim0").set("0 1 2"); add.parm("closed0").set(1) if add.parm("closed0") else print("no closed0 parm", [p.name() for p in add.parms() if "0" in p.name()])

ac = geo.createNode("attribcreate"); ac.setInput(0, add)
pm = ac.parm("precision1")
print("U2 attribcreate precision1 menu:", pm.menuItems(), pm.menuLabels())
ac.parm("name1").set("uv"); ac.parm("class1").set("vertex"); ac.parm("size1").set(3)
pm.set("64")
ac.parm("value1v1").set(0.1)
g = ac.geometry()
att = g.findVertexAttrib("uv")
print("U2 attribcreate uv:", att.numericDataType(), "value", repr(g.prims()[0].vertices()[0].attribValue("uv")[0]))
# Python SOP downstream writes a double into the 64-bit attribute
py = geo.createNode("python"); py.setInput(0, ac)
py.parm("python").set(
    "geo = hou.pwd().geometry()\n"
    "a = geo.findVertexAttrib('uv')\n"
    "for v in geo.prims()[0].vertices():\n"
    "    v.setAttribValue(a, (0.1, 1.0 - 0.10000000149011612, 0.0))\n"
    "geo.addAttrib(hou.attribType.Vertex, 'uv32', (0.0, 0.0, 0.0))\n"
)
pg = py.geometry()
v0 = pg.prims()[0].vertices()[0]
print("U2 python SOP write into 64-bit uv:", pg.findVertexAttrib("uv").numericDataType(), repr(v0.attribValue("uv")[1]), "exact double?", v0.attribValue("uv")[1] == 1.0 - 0.10000000149011612)
print("U2 python addAttrib float default precision:", pg.findVertexAttrib("uv32").numericDataType())
# Does a Transform SOP / Fuse keep 64-bit?
xf = geo.createNode("xform"); xf.setInput(0, py)
print("U2 after xform uv precision:", xf.geometry().findVertexAttrib("uv").numericDataType())
# Does a float64 vertex attrib survive bgeo save/load?
fn = os.path.join(OUT, "u2.bgeo"); pg.saveToFile(fn)
g2 = hou.Geometry(); g2.loadFromFile(fn)
print("U2 bgeo reload uv precision:", g2.findVertexAttrib("uv").numericDataType(), repr(g2.prims()[0].vertices()[0].attribValue("uv")[1]))
# hou.Geometry() standalone: can we copy a 64-bit attrib definition from another geometry via merge?
g3 = hou.Geometry(); g3.merge(pg)
print("U2 hou.Geometry.merge keeps precision:", g3.findVertexAttrib("uv").numericDataType())

# U3 Normal SOP (vertex normals) vs prim.normal for '0 1 2'
nrm = geo.createNode("normal"); nrm.setInput(0, add)
print("U3 Normal SOP parms type menu:", nrm.parm("type").menuItems())
for item in nrm.parm("type").menuItems()[:3]:
    nrm.parm("type").set(item)
    gg = nrm.geometry()
    a = gg.findVertexAttrib("N") or gg.findPointAttrib("N") or gg.findPrimAttrib("N")
    if a.type() == hou.attribType.Vertex:
        val = gg.prims()[0].vertices()[0].attribValue("N")
    elif a.type() == hou.attribType.Point:
        val = gg.points()[0].attribValue("N")
    else:
        val = gg.prims()[0].attribValue("N")
    print("U3 Normal SOP type=%s ->" % item, a.type(), tuple(round(x, 4) for x in val))
print("U3 Normal SOP parm names:", [p.name() for p in nrm.parms()])

# U4 edge group order and direction
g = hou.Geometry()
pts = [g.createPoint() for _ in range(4)]
for p, pos in zip(pts, [(0,0,0),(1,0,0),(1,1,0),(0,1,0)]): p.setPosition(pos)
poly = g.createPolygon(); [poly.addVertex(p) for p in pts]
eg = g.createEdgeGroup("a3ob_sharp")
want = [(2, 1), (0, 3), (1, 0)]
for a, b in want:
    e = g.findEdge(pts[a], pts[b]); eg.add(e)
got = [tuple(p.number() for p in e.points()) for e in eg.edges()]
print("U4 edge group insertion", want, "-> read back", got)
fn = os.path.join(OUT, "u4.bgeo"); g.saveToFile(fn)
g2 = hou.Geometry(); g2.loadFromFile(fn)
print("U4 after bgeo reload:", [tuple(p.number() for p in e.points()) for e in g2.edgeGroups()[0].edges()])
print("U4 findEdge for non-edge pair (0,2):", g.findEdge(pts[0], pts[2]))

# U5 Unpack detail_attributes default + typeinfo options
up = geo.createNode("unpack")
print("U5 unpack detail_attributes default:", repr(up.parm("detail_attributes").eval()), "transfer_attributes:", repr(up.parm("transfer_attributes").eval()), "transfer_groups:", repr(up.parm("transfer_groups").eval()))
g = hou.Geometry()
n = g.addAttrib(hou.attribType.Vertex, "N", (0.0, 0.0, 0.0), transform_as_normal=True)
u = g.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
print("U5 HOM-created N qualifier:", repr(n.qualifier()), "transformAsNormal", n.isTransformedAsNormal(), "| uv qualifier:", repr(u.qualifier()), "options", u.options())
try:
    u.setOption("type", "texturecoord"); print("U5 uv after setOption type=texturecoord:", repr(u.qualifier()), u.options())
except Exception as e:
    print("U5 setOption FAIL", e)

# U6 hou.Matrix4 extraction of rows for a proxy frame
m = hou.Matrix4(((0,0,-1,0),(0,1,0,0),(1,0,0,0),(1,2,3,1)))
print("U6 Vector3(0,0,-1)*M:", tuple(hou.Vector3(0,0,-1) * m), "Vector3(0,2,0)*M:", tuple(hou.Vector3(0,2,0) * m))

# U7 create a 64-bit vertex float attribute on a standalone hou.Geometry via the attribcreate SOP verb (no nodes)
verb = hou.sopNodeTypeCategory().nodeVerb("attribcreate::2.0")
src = hou.Geometry()
pts = [src.createPoint() for _ in range(3)]
poly = src.createPolygon(); [poly.addVertex(p) for p in pts]
print("U7 verb parms:", {k: v for k, v in verb.parms().items() if k.endswith("1") or k == "numattr"}); verb.setParms({"numattr": ({"enable#": 1, "name#": "uv", "class#": 3, "type#": 0, "size#": 3, "precision#": 3, "typeinfo#": 0, "existing#": 3, "writevalues#": 1, "value#v": hou.Vector4(0, 0, 0, 0), "default#v": hou.Vector4(0, 0, 0, 0)},)})
dst = hou.Geometry()
verb.execute(dst, [src])
ua = dst.findVertexAttrib("uv")
print("U7 verb attribcreate -> uv:", ua and (ua.type(), ua.size(), ua.numericDataType()))
if ua:
    for v in dst.prims()[0].vertices():
        v.setAttribValue(ua, (0.25, 1.0 - 0.10000000149011612, 0.0))
    print("U7 exact double stored:", dst.prims()[0].vertices()[0].attribValue("uv")[1] == 1.0 - 0.10000000149011612)
print("U8 unpack detail_attributes menu:", up.parm("detail_attributes").parmTemplate().type(), getattr(up.parm("detail_attributes").parmTemplate(), "menuLabels", lambda: None)())

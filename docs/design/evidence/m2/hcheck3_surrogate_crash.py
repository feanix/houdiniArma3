# Review evidence (copied from the M2 review, rv_sur.py). Hands a lone surrogate (M1 cp1252+surrogateescape decoding of b'\x81abc') to hou.
# Expected on hython 22.0.466: the process dies inside setAttribValue (segmentation fault). Run it only headless.
import hou
s = b"\x81abc".decode("cp1252", "surrogateescape")
print("value", ascii(s), flush=True)
gs = hou.Geometry(); pp = gs.createPoint(); pr = gs.createPolygon(); pr.addVertex(pp)
sa = gs.addAttrib(hou.attribType.Prim, "a3ob_texture", "")
print("before set", flush=True)
try:
    pr.setAttribValue(sa, s)
    print("after set", ascii(pr.attribValue(sa)), flush=True)
except Exception as ex:
    print("exc", type(ex).__name__, ex, flush=True)
try:
    gs.addArrayAttrib(hou.attribType.Global, "a3ob_prop_values", hou.attribData.String)
    gs.setGlobalAttribValue("a3ob_prop_values", [s])
    print("array ok", ascii(gs.attribValue("a3ob_prop_values")), flush=True)
except Exception as ex:
    print("array exc", type(ex).__name__, ex, flush=True)

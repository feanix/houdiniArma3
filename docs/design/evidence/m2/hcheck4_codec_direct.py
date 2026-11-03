"""V9 (lead, 2026-11-03): M1 strings (cp1252 + C1 for undefined bytes) go into hou unchanged.
Run: hython docs/design/evidence/m2/hcheck4_codec_direct.py
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys
sys.path.insert(0, "E:/Projects/houdiniArma3/python")
import hou
from a3ob_core.binary import decode_string, encode_string
geo = hou.Geometry()
pa = geo.addAttrib(hou.attribType.Prim, "s", "")
da = geo.addArrayAttrib(hou.attribType.Global, "arr", hou.attribData.String)
prim = geo.createPolygon()
bad = []
names = []
for b in range(1, 256):
    s = decode_string(bytes([b]) + b"x")
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in s)
    prim.setAttribValue(pa, s)
    if prim.attribValue(pa) != s or encode_string(prim.attribValue(pa)) != bytes([b]) + b"x":
        bad.append(("prim", b))
    g = geo.createPointGroup(hou.text.encodeAttrib("sel" + s))
    if hou.text.decodeAttrib(g.name()) != "sel" + s:
        bad.append(("group", b))
    names.append(s)
geo.setGlobalAttribValue(da, names)
if list(geo.attribValue(da)) != names:
    bad.append(("detail-array", None))
print("mismatches:", bad)
print("euro:", repr(decode_string(b"\x80")), "c1:", repr(decode_string(b"\x81")))

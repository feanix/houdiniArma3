"""Q2c: git on Windows (core.autocrlf) can check text files out with CRLF line
endings. Does the HDA text source still load if every file has CRLF?

Run: hython q2c_crlf.py <workdir>
"""
import os
import shutil
import sys

import hou

HERE = os.path.dirname(os.path.abspath(__file__))
work = sys.argv[1]
if os.path.isdir(work):
    shutil.rmtree(work)
src = os.path.join(work, "crlf_src")
shutil.copytree(os.path.join(HERE, "spike_v1", "hda_src"), src)
for root, _dirs, files in os.walk(src):
    for name in files:
        p = os.path.join(root, name)
        data = open(p, "rb").read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        open(p, "wb").write(data)
print("CRLF in DialogScript:", b"\r\n" in open(os.path.join(
    src, "Sop_1a3ob__spike__export", "DialogScript"), "rb").read())

out = os.path.join(work, "crlf.hda")
hou.hda.collapseFromDirectory(out, src)
print("collapsed:", os.path.getsize(out), "bytes")
try:
    hou.hda.installFile(out)
    d = hou.hda.definitionsInFile(out)
    print("loaded:", [x.nodeTypeName() for x in d])
    node = hou.node("/obj").createNode("geo").createNode("a3ob_spike_export")
    print("parms:", [p.name() for p in node.parms() if p.name() in ("tag", "path", "save")])
    print("default tag:", repr(node.parm("tag").eval()))
    print("button callback:", node.parm("save").parmTemplate().scriptCallback())
except Exception as e:
    print("FAILED:", type(e).__name__, str(e).strip().splitlines()[0])

print("--- directory form (installed as <name>.hda folder) with CRLF ---")
folder = os.path.join(work, "otls", "a3ob_spike_export.hda")
shutil.copytree(src, folder)
try:
    hou.hda.uninstallFile(out)
    hou.hda.installFile(folder)
    print("loaded from CRLF folder:", [x.nodeTypeName() for x in hou.hda.definitionsInFile(folder)])
    node = hou.node("/obj").createNode("geo").createNode("a3ob_spike_export")
    print("default tag:", repr(node.parm("tag").eval()))
except Exception as e:
    print("FAILED:", type(e).__name__, str(e).strip().splitlines()[0])

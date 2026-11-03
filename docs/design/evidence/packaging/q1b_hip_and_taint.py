"""Q1b: what does Indie do with .hip names, and how does a saved HDA identify as non-commercial?

Run: hython q1b_hip_and_taint.py <tempdir> <hda saved by q1_save_hda.py>
"""
import os
import subprocess
import sys

import hou

out, hda = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

print("default HDA extension for this session:", hou.hda.defaultFileExtension())

# 1. .hip names: does Indie rewrite the extension?
for name in ("plain.hip", "plain.hiplc", "plain.hipnc"):
    path = os.path.join(out, name)
    try:
        hou.hipFile.save(path)
        print("save", name, "->", hou.hipFile.path(), "| files:",
              sorted(f for f in os.listdir(out) if f.startswith("plain")))
    except Exception as e:
        print("save", name, "FAILED", type(e).__name__, e)

# 2. first bytes of what was written
for f in sorted(os.listdir(out)):
    if f.startswith("plain"):
        with open(os.path.join(out, f), "rb") as fh:
            print("header of", f, fh.read(48))

# 3. The HDA: does the file name matter? Load the .hda saved earlier.
hou.hda.installFile(hda)
print("installed", hda)
node = hou.node("/obj").createNode("geo").createNode("a3ob_q1_test")
print("created node of saved .hda in this Indie session:", node.path())
hou.hipFile.save(os.path.join(out, "with_hda.hiplc"))
print("saved .hiplc referencing the HDA; size",
      os.path.getsize(os.path.join(out, "with_hda.hiplc")))

# 4. hotl on the saved HDA: contents are not expandable
hotl = os.path.join(os.environ["HFS"], "bin", "hotl.exe")
for flag in ("-t", "-X"):
    d = os.path.join(out, "x" + flag.strip("-"))
    r = subprocess.run([hotl, flag, d, hda], capture_output=True, text=True)
    print("hotl", flag, "exit", r.returncode, "|", (r.stdout + r.stderr).strip())

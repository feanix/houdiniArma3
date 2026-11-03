"""Q1: what does saving an HDA from this Indie install produce?

Run: hython q1_save_hda.py <tempdir>
"""
import os
import sys

import hou

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

print("houdini", hou.applicationVersionString())
print("licenseCategory", hou.licenseCategory())
print("isApprentice", hou.isApprentice())
print("isIndie (licenseCategory name)", hou.licenseCategory().name())

geo = hou.node("/obj").createNode("geo", "g")
sub = geo.createNode("subnet", "q1_sub")
node_hda = os.path.join(out, "q1_test.hda")
hda_node = sub.createDigitalAsset(
    name="a3ob_q1_test", hda_file_name=node_hda, description="Q1 test",
    min_num_inputs=0, max_num_inputs=1)
d = hda_node.type().definition()
print("saved to", d.libraryFilePath())
print("exists .hda", os.path.exists(node_hda))
print("exists .hdalc", os.path.exists(os.path.join(out, "q1_test.hdalc")))
print("size", os.path.getsize(node_hda))
print("definition.licenseCategory? attrs:",
      [a for a in dir(d) if "icen" in a or "ommercial" in a.lower()])
print("definition.isInstalled", d.isInstalled())

# Sections and the tag in the file bytes
print("sections", sorted(d.sections().keys()))
data = open(node_hda, "rb").read()
for needle in (b"limited", b"Limited", b"Indie", b"indie", b"commercial",
               b"Apprentice", b"hdalc", b"NonCommercial", b"LimitedCommercial"):
    print("bytes contain", needle, needle in data)

# Try explicit .hdalc name
node_lc = os.path.join(out, "q1_test2.hdalc")
sub2 = geo.createNode("subnet", "q1_sub2")
try:
    n2 = sub2.createDigitalAsset(name="a3ob_q1_test2", hda_file_name=node_lc,
                                 description="Q1 test2")
    print("hdalc save ok, exists:", os.path.exists(node_lc))
except Exception as e:
    print("hdalc save failed:", type(e).__name__, e)

for f in sorted(os.listdir(out)):
    print("file", f, os.path.getsize(os.path.join(out, f)))

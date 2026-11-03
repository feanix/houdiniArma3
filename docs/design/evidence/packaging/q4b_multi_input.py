"""Q4b: can a text-authored Python-type SOP asset take several inputs
(D10 'Assemble LODs' merges N LOD branches)? What does the text source
look like for that?

Run: hython q4b_multi_input.py <workdir>
"""
import os
import shutil
import subprocess
import sys

import hou

work = sys.argv[1]
if os.path.isdir(work):
    shutil.rmtree(work)
os.makedirs(work)
HERE = os.path.dirname(os.path.abspath(__file__))
HOTL = os.path.join(os.environ["HFS"], "bin", "hotl.exe")

# Start from the spike_v1 source, build it, raise the input limit through the
# API, save, expand to text and show what changed in the text source.
src = os.path.join(HERE, "spike_v1", "hda_src")
base = os.path.join(work, "base.hda")
hou.hda.collapseFromDirectory(base, src)
hou.hda.installFile(base)
d = hou.hda.definitionsInFile(base)[0]
print("before: min/max inputs", d.minNumInputs(), d.maxNumInputs())
d.setMaxNumInputs(4)
d.setMinNumInputs(1)
multi = os.path.join(work, "multi.hda")
d.copyToHDAFile(multi)
print("after : min/max inputs", hou.hda.definitionsInFile(multi)[0].minNumInputs(),
      hou.hda.definitionsInFile(multi)[0].maxNumInputs())
exp = os.path.join(work, "multi_t")
subprocess.run([HOTL, "-t", exp, multi], check=True)
sub = [x for x in os.listdir(exp) if x.startswith("Sop_")][0]
print("text source: Inputs line in INDEX__SECTION:",
      [l for l in open(os.path.join(exp, "INDEX__SECTION")).read().splitlines()
       if l.startswith("Inputs")])
print("DialogScript after change:")
print(open(os.path.join(exp, sub, "DialogScript")).read())

# And does the node accept 4 inputs and cook with them?
hou.hda.uninstallFile(base)
hou.hda.installFile(multi)
geo = hou.node("/obj").createNode("geo")
boxes = [geo.createNode("box") for _ in range(3)]
n = geo.createNode("a3ob_spike_export")
for i, b in enumerate(boxes):
    n.setInput(i, b)
print("connected inputs:", len(n.inputs()), "| max:", n.type().maxNumInputs())

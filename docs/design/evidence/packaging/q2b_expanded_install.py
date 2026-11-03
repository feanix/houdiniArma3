"""Q2b: can Houdini load an HDA straight from the expanded text directory
(no collapse step), when it sits in an otls folder with an .hda name?

SideFX hotl docs say yes. This checks it on the Indie install, with the
text source from spike_v1/hda_src (a Python-type SOP asset).

Run: python q2b_expanded_install.py <workdir>      (plain python; starts hython)
Child mode (internal): hython q2b_expanded_install.py --child
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HYTHON = r"E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe"

if len(sys.argv) > 1 and sys.argv[1] == "--child":
    import hou
    libs = [f for f in hou.hda.loadedFiles() if "q2b" in f.replace("\\", "/")]
    print("loaded library paths:", [os.path.basename(f) + (" (directory)" if os.path.isdir(f) else " (file)") for f in libs])
    geo = hou.node("/obj").createNode("geo")
    box = geo.createNode("box")
    node = geo.createNode("a3ob_spike_export")
    node.setInput(0, box)
    node.parm("tag").set("from-expanded-dir")
    import a3ob_spike.entry  # noqa: F401  (module on PYTHONPATH)
    node.cook(force=True)
    print("cooked: errors =", node.errors() or "none", "| tag =",
          node.geometry().attribValue("tag"))
    print("definition file is a directory:",
          os.path.isdir(node.type().definition().libraryFilePath()))
    sys.exit(0)

work = os.path.abspath(sys.argv[1])
if os.path.isdir(work):
    shutil.rmtree(work)
otls = os.path.join(work, "q2b", "otls")
os.makedirs(otls)
# The "package" ships the directory, named like a library file.
shutil.copytree(os.path.join(HERE, "spike_v1", "hda_src"),
                os.path.join(otls, "a3ob_spike_export.hda"))
env = dict(os.environ)
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["HOUDINI_PATH"] = os.path.join(work, "q2b").replace("\\", "/") + ";&"
env["PYTHONPATH"] = os.path.join(HERE, "spike_v1")
r = subprocess.run([HYTHON, os.path.abspath(__file__), "--child"], env=env,
                   capture_output=True, text=True)
for line in (r.stdout + r.stderr).splitlines():
    if line.strip() and not line.startswith(("Setting breakpad", "SteamInternal")):
        print(line)
print("exit", r.returncode)

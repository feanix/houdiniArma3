"""Run the Q3/Q4 phases in separate hython processes (one per step, so the
module version on PYTHONPATH can change between a save and a later load).

Usage: python q34_driver.py <workdir>
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
HYTHON = r"E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe"
work = os.path.abspath(sys.argv[1])
if os.path.isdir(work):
    shutil.rmtree(work)
os.makedirs(os.path.join(work, "otls"))
NOISE = ("Setting breakpad", "SteamInternal")


def step(title, version, phase, *extra):
    print("\n--- %s (module v%d) ---" % (title, version))
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOUDINI_PATH"] = work.replace("\\", "/") + ";&"
    env["PYTHONPATH"] = os.pathsep.join([
        os.path.join(HERE, "spike_v%d" % version),
        os.path.join(REPO, "python")])
    r = subprocess.run([HYTHON, os.path.join(HERE, "q34_phase.py"), phase,
                        work, *extra], env=env, capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip() and not line.startswith(NOISE):
            print(line.replace(work.replace("\\", "/"), "<work>")
                  .replace(work, "<work>"))
    print("(exit %d)" % r.returncode)


print("== Q3: Python SOP fallback ==")
step("build in v1, save .hip", 1, "fallback_build")
step("fresh process, same v1 code, load .hip", 1, "fallback_load")
step("fresh process, package updated to v2, load the v1 .hip", 2, "fallback_load")
step("v2: repairs", 2, "fallback_repair")

print("\n\n== Q4 + Q3: HDA built from text, with Save to Disk button ==")
for v in (1, 2):
    step("collapse HDA source v%d" % v, v, "hda_build",
         os.path.join(HERE, "spike_v%d" % v, "hda_src"))
# The package puts its HDAs on the scan path (houdini/otls in the real
# package); here the same thing is done with HOUDINI_PATH (work dir, with an otls subfolder).
shutil.copy(os.path.join(work, "spike_v1.hda"),
            os.path.join(work, "otls", "a3ob_spike_export.hda"))
step("scene with HDA v1, press button, save .hip", 1, "hda_scene",
     )
# A package update replaces the file in place.
shutil.copy(os.path.join(work, "spike_v2.hda"),
            os.path.join(work, "otls", "a3ob_spike_export.hda"))
step("HDA updated to v2 (new parm, new default), load v1 .hip", 2, "hda_load",
     )
print("\nfiles written in workdir:", sorted(f for f in os.listdir(work)
                                          if f.endswith(".json")))

"""Q2: expand / collapse HDAs as text, and author a Python SOP HDA from text only.

Run: hython q2_text_source.py <tempdir>

Parts
  A  control: a SideFX (commercial) library expands with `hotl -t`.
  B  expand one commercial asset, collapse it again with Indie `hotl -l`
     (and `-c`), then try to expand the result: does it carry the
     non-commercial tag? (Indie hotl refuses non-commercial contents with
     "Cannot convert non-commercial HDAs".)
  C  author a minimal Python SOP HDA from text files only, collapse with
     `hotl -l`, `hotl -c` and hou.hda.collapseFromDirectory, install, cook.
"""
import os
import shutil
import subprocess
import sys

import hou

out = sys.argv[1]
if os.path.isdir(out):
    shutil.rmtree(out)
os.makedirs(out)
HFS = os.environ["HFS"]
HOTL = os.path.join(HFS, "bin", "hotl.exe")
OPLIB = os.path.join(HFS, "houdini", "otls", "OPlibSop.hda")
print("licenseCategory:", hou.licenseCategory())


def run(*args):
    r = subprocess.run([HOTL, *args], capture_output=True, text=True)
    msg = (r.stdout + r.stderr).strip().replace(out, "<tmp>")
    print("  hotl", " ".join(os.path.basename(a) if os.path.exists(a) else a
                              for a in args), "-> exit", r.returncode,
          "|", msg.splitlines()[0] if msg else "(no output)")
    return r


def wtext(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(text)


print("\n== A. expand a SideFX library (commercial file) ==")
full = os.path.join(out, "oplibsop_t")
run("-t", full, OPLIB)

print("\n== B. commercial asset -> Indie collapse -> expand again ==")
# Reduce the expanded library to one asset that has a node network inside.
asset = "Sop_1adaptiveprune"
one = os.path.join(out, "one_t")
os.makedirs(one)
shutil.copytree(os.path.join(full, asset), os.path.join(one, asset))
wtext(os.path.join(one, "houdini.hdalibrary"), "")
# INDEX__SECTION is the human-readable listing (what `hotl -V` prints, minus
# sections). An empty one gives a file Houdini refuses to load, so copy the
# block of this one asset.
blocks = open(os.path.join(full, "INDEX__SECTION")).read().split("\n\n")
wtext(os.path.join(one, "INDEX__SECTION"),
      [b for b in blocks if b.startswith("Operator:     adaptiveprune")][0] + "\n\n")
wtext(os.path.join(one, "Sections.list"),
      '""\nINDEX__SECTION\tINDEX_SECTION\nhoudini.hdalibrary\thoudini.hdalibrary\n'
      "%s\tSop/adaptiveprune\n" % asset)
print("  has Contents.mime (network as MIME text):",
      os.path.exists(os.path.join(one, asset, "Contents.dir", "Contents.mime")))
for ext in (".hda", ".hdalc"):
    dst = os.path.join(out, "b_collapsed" + ext)
    run("-l", one, dst)
    print("  size", os.path.getsize(dst))
    run("-V", dst)
    rx = os.path.join(out, "b_reexpand" + ext.strip("."))
    run("-t", rx, dst)
    a = open(os.path.join(one, asset, "Contents.dir", "Contents.mime"), "rb").read()
    b = open(os.path.join(rx, asset, "Contents.dir", "Contents.mime"), "rb").read()
    print("  Contents.mime identical after Indie collapse + expand:", a == b,
          "(%d bytes)" % len(a))

print("\n== C. Python SOP HDA authored from text only ==")
name = "a3ob_q2_pysop"
src = os.path.join(out, "src_t")
adir = os.path.join(src, "Sop_1" + name.replace("_", "__"))
wtext(os.path.join(src, "houdini.hdalibrary"), "")
wtext(os.path.join(src, "INDEX__SECTION"), "".join(
    "%-14s%s\n" % (k + ":", v) for k, v in (
        ("Operator", name), ("Label", "A3OB Q2 Python SOP"),
        ("Path", "oplib:/Sop/%s?Sop/%s" % (name, name)),
        ("Icon", "SOP_python"), ("Table", "Sop"), ("License", ""),
        ("Extra", ""), ("User", ""), ("Inputs", "1 to 1"),
        ("Subnet", "false"), ("Python", "true"), ("Empty", "false"),
        ("Modified", "Mon Nov  3 12:00:00 2026"))) + "\n")
wtext(os.path.join(src, "Sections.list"),
      '""\nINDEX__SECTION\tINDEX_SECTION\nhoudini.hdalibrary\thoudini.hdalibrary\n'
      "%s\tSop/%s\n" % (os.path.basename(adir), name))
wtext(os.path.join(adir, "Sections.list"),
      '""\nDialogScript\tDialogScript\nPythonCook\tPythonCook\n'
      "TypePropertiesOptions\tTypePropertiesOptions\n")
wtext(os.path.join(adir, "DialogScript"), """\
# Dialog script for %(n)s

{
    name	%(n)s
    script	%(n)s
    label	"A3OB Q2 Python SOP"

    help {
	""
    }

    inputlabel	1	"Input"

    parm {
	name	"tag"
	label	"Tag"
	type	string
	default	{ "hello" }
    }
}
""" % {"n": name})
wtext(os.path.join(adir, "PythonCook"), """\
node = hou.pwd()
geo = node.geometry()
geo.addAttrib(hou.attribType.Global, "tag", "")
geo.setGlobalAttribValue("tag", node.evalParm("tag"))
""")
wtext(os.path.join(adir, "TypePropertiesOptions"), """\
ParmsFromVfl := 0;
PrefixDroppedParmName := 1;
UseDSParms := 1;
ForbidOutsideParms := 1;
LockContents := 1;
SaveSpareParms := 0;
CheckExternal := 1;
GzipContents := 1;
MakeDefault := 1;
PrefixDroppedParmLabel := 1;
UnlockOnCreate := 0;
""")

results = {}
for how in ("hotl -l (.hdalc)", "hotl -l (.hda)", "hotl -c (.hda)",
            "hou.hda.collapseFromDirectory (.hda)"):
    tag = how.split("(")[0].strip().replace(" ", "_").replace(".", "_")
    ext = ".hdalc" if "hdalc" in how else ".hda"
    dst = os.path.join(out, "c_" + tag + ext)
    print(" ", how)
    try:
        if how.startswith("hotl -l"):
            run("-l", src, dst)
        elif how.startswith("hotl -c"):
            # -c wants the -x layout; the same plain-text sections work here
            run("-c", src, dst)
        else:
            hou.hda.collapseFromDirectory(dst, src)
    except Exception as e:
        print("   failed:", type(e).__name__, e)
    results[how] = dst if os.path.exists(dst) else None
    print("   exists:", os.path.exists(dst),
          "size:", os.path.getsize(dst) if os.path.exists(dst) else "-")

print("\n  inspect + cook each collapsed result")
for how, dst in results.items():
    if not dst:
        continue
    print(" ", how)
    run("-V", dst)
    run("-t", dst + "_x", dst)
    try:
        hou.hda.installFile(dst)
        defs = hou.hda.definitionsInFile(dst)
        print("   definitions:", [d.nodeTypeName() for d in defs],
              "sections:", sorted(defs[0].sections().keys()))
        geo = hou.node("/obj").createNode("geo", "g_" + os.path.basename(dst).split(".")[0])
        for c in geo.children():
            c.destroy()
        box = geo.createNode("box")
        node = geo.createNode(name)
        node.setInput(0, box)
        node.parm("tag").set("cooked-from-text")
        node.cook(force=True)
        g = node.geometry()
        print("   cooked: points", len(g.points()), "tag =",
              g.attribValue("tag"), "errors", node.errors())
        geo.destroy()
        hou.hda.uninstallFile(dst)
    except Exception as e:
        print("   load/cook failed:", type(e).__name__, e)

print("\n== D. Indie re-saves the text-authored Python SOP: does it become non-commercial? ==")
src_hda = results["hotl -l (.hda)"]
hou.hda.installFile(src_hda)
d = hou.hda.definitionsInFile(src_hda)[0]
resaved = os.path.join(out, "d_resaved_by_indie.hda")
d.copyToHDAFile(resaved)
print("  copyToHDAFile ->", os.path.getsize(resaved), "bytes")
run("-V", resaved)
run("-t", resaved + "_x", resaved)
print("  sections after Indie save:",
      sorted(hou.hda.definitionsInFile(resaved)[0].sections().keys()))
a = open(resaved, "rb").read()
b = open(src_hda, "rb").read()
diff = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
print("  bytes identical to the hotl-collapsed file:", a == b)
print("  sizes %d vs %d; differing byte offsets: %s (these are the 4-byte"
      " modification-time fields in the section index)" % (len(a), len(b), diff))

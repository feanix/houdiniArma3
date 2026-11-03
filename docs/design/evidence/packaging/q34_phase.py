"""Q3 + Q4 phases, run in separate hython processes by q34_driver.py.

Usage: hython q34_phase.py <phase> <workdir> [hda_src_dir]
The spike module version in use is whatever a3ob_spike is first on PYTHONPATH.
"""
import json
import os
import sys

import hou

phase, work = sys.argv[1], sys.argv[2]
HIP = os.path.join(work, "fallback.hip")
HIP2 = os.path.join(work, "hda_scene.hip")


def say(*a):
    print("[%s]" % phase, *a)


def cook_report(node):
    """Cook and report; never raises."""
    try:
        node.cook(force=True)
    except Exception as e:  # hou.OperationFailed on a cook error
        say("  %-8s cook raised %s" % (node.name(), type(e).__name__))
    errs = [e.strip().replace("\n", " | ") for e in node.errors()]
    geo = node.geometry()
    tag = None
    if geo is not None and geo.findGlobalAttrib("tag") is not None:
        tag = geo.attribValue("tag")
    say("  %-8s errors=%s tag=%r" % (node.name(), errs or "none", tag))
    return not errs


def press(node, outfile):
    node.parm("path").set(outfile)
    if os.path.exists(outfile):
        os.remove(outfile)
    try:
        node.parm("save").pressButton()
        say("  %s: pressButton -> exists=%s %s" % (
            node.name(), os.path.exists(outfile),
            open(outfile).read() if os.path.exists(outfile) else ""))
    except Exception as e:
        say("  %s: pressButton raised %s: %s" % (
            node.name(), type(e).__name__, str(e).strip().splitlines()[0]))


# ----------------------------------------------------------------- Q3 fallback
if phase == "fallback_build":
    import a3ob_houdini  # the real package, via PYTHONPATH (repo python/)
    import a3ob_core
    say("real packages importable:", a3ob_houdini.__file__ is not None,
        a3ob_core.__file__ is not None)
    import a3ob_spike.shelf as shelf
    import a3ob_spike.nodes as nodes
    say("spike module version", nodes.VERSION, "from", nodes.__file__)
    geo = hou.node("/obj").createNode("geo", "geo1")
    box = geo.createNode("box")
    for name, naive in (("stable", False), ("naive", True)):
        n = shelf.create_python_sop(geo, name, naive=naive)
        n.setInput(0, box)
        n.parm("tag").set("from-" + name)
        say("created", n.path(), "parms:", [p.name() for p in n.parms()
                                           if p.name() in ("tag", "scale", "path", "save")])
        cook_report(n)
    press(geo.node("stable"), os.path.join(work, "out_stable_build.json"))
    hou.hipFile.save(HIP)
    say("saved", os.path.basename(HIP), os.path.getsize(HIP), "bytes")

elif phase == "fallback_load":
    import a3ob_spike.nodes as nodes
    say("spike module version", nodes.VERSION)
    hou.hipFile.load(HIP, suppress_save_prompt=True)
    geo = hou.node("/obj/geo1")
    for n in (geo.node("stable"), geo.node("naive")):
        say(n.name(), "code:", n.parm("python").eval().strip().replace("\n", " ; "))
        say("  spare parms present:", [p.name() for p in n.parms()
                                       if p.name() in ("tag", "scale", "path", "save")],
            "tag value:", n.parm("tag").eval())
        cook_report(n)
    press(geo.node("stable"), os.path.join(work, "out_stable_%d.json" % nodes.VERSION))
    press(geo.node("naive"), os.path.join(work, "out_naive_%d.json" % nodes.VERSION))

elif phase == "fallback_repair":
    # v2 on path, hip written by v1. Try the repairs.
    import a3ob_spike.entry as entry
    import a3ob_spike.shelf as shelf
    hou.hipFile.load(HIP, suppress_save_prompt=True)
    geo = hou.node("/obj/geo1")
    stable, naive = geo.node("stable"), geo.node("naive")
    say("repair 1: call entry.upgrade(node) outside a cook")
    say("  added parms:", entry.upgrade(stable))
    cook_report(stable)
    say("  tag value (old default stays in the node):", stable.parm("tag").eval(),
        "| scale:", stable.parm("scale").eval())
    say("repair 2: naive wrapper needs its stored code rewritten")
    naive.parm("python").set(shelf.NAIVE_CODE)
    entry.upgrade(naive)
    cook_report(naive)
    say("repair 3: self-heal inside the cook (upgrade called from the node code)")
    healed = geo.createNode("python", "selfheal")
    healed.setInput(0, geo.node("box1"))
    # a v1-style node: only v1 parms, then v2 code that upgrades while cooking
    import a3ob_spike.nodes as nodes
    group = healed.parmTemplateGroup()
    group.append(nodes.spare_parms()[0])
    healed.setParmTemplateGroup(group)
    healed.parm("python").set(
        "import a3ob_spike.entry as e\ne.upgrade(hou.pwd())\ne.cook(hou.pwd())\n")
    cook_report(healed)
    say("  parms after self-heal attempt:", [p.name() for p in healed.parms()
                                            if p.name() in ("tag", "scale", "path", "save")])
    say("fresh node from the v2 shelf function: tag default",
        shelf.create_python_sop(geo, "fresh").parm("tag").eval())

# ---------------------------------------------------------------- Q4 + Q3 (HDA)
elif phase == "hda_build":
    src = sys.argv[3]
    dst = os.path.join(work, os.path.basename(os.path.dirname(src)) + ".hda")
    hou.hda.collapseFromDirectory(dst, src)
    say("collapsed", src.replace("\\", "/").split("/")[-2], "->",
        os.path.basename(dst), os.path.getsize(dst), "bytes")

elif phase == "hda_scene":
    import a3ob_spike.nodes as nodes
    say("spike module version", nodes.VERSION, "| HDA found by scan path:",
        [os.path.basename(f) for f in hou.hda.loadedFiles() if work.replace("\\", "/") in f.replace("\\", "/")])
    geo = hou.node("/obj").createNode("geo", "geo1")
    box = geo.createNode("box")
    n = geo.createNode("a3ob_spike_export", "exporter")
    n.setInput(0, box)
    n.parm("tag").set("from-hda")
    say("definition file:", n.type().definition().libraryFilePath().replace(work.replace("\\", "/"), "<work>"))
    say("parms:", [p.name() for p in n.parms() if p.name() in ("tag", "scale", "path", "save")])
    cook_report(n)
    press(n, os.path.join(work, "out_hda_build.json"))
    hou.hipFile.save(HIP2)
    say("saved", os.path.basename(HIP2), os.path.getsize(HIP2), "bytes")

elif phase == "hda_load":
    import a3ob_spike.nodes as nodes
    say("spike module version", nodes.VERSION, "| HDA found by scan path:",
        [os.path.basename(f) for f in hou.hda.loadedFiles() if work.replace("\\", "/") in f.replace("\\", "/")])
    hou.hipFile.load(HIP2, suppress_save_prompt=True)
    n = hou.node("/obj/geo1/exporter")
    say("definition file:", n.type().definition().libraryFilePath().replace(work.replace("\\", "/"), "<work>"))
    say("parms:", [p.name() for p in n.parms() if p.name() in ("tag", "scale", "path", "save")])
    say("tag value kept from hip:", n.parm("tag").eval(),
        "| scale (new in v2):", n.parm("scale").eval() if n.parm("scale") else "absent")
    cook_report(n)
    press(n, os.path.join(work, "out_hda_%d.json" % nodes.VERSION))
    fresh = n.parent().createNode("a3ob_spike_export", "fresh")
    say("fresh node tag default:", fresh.parm("tag").eval())

else:
    raise SystemExit("unknown phase " + phase)

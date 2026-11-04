# Spike for task #60 (character rigging design): create and cook the KineFX / APEX nodes that matter for fitting a
# skeleton and moving capture weights, on tiny made-up data, and print what each one reports (errors, warnings, cook
# time, attributes). This answers "does it work in Houdini Indie, headless?" for each node.
#
# Run (headless hython only, never the GUI; no HOUDINI_PACKAGE_SKIP, the KineFX nodes live in a default package):
#   "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/spikes/rigging/houdini_node_cooks.py
#
# SPDX-License-Identifier: GPL-3.0-or-later

import time

import hou

SKELETON_CODE = """
import hou
geo = hou.pwd().geometry()
geo.clear()
geo.addAttrib(hou.attribType.Point, "name", "")
ident = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
geo.addAttrib(hou.attribType.Point, "transform", ident)
pts = []
for i, y in enumerate((-0.8, 0.0, 0.8)):
    p = geo.createPoint()
    p.setPosition((0.0, y, 0.0))
    p.setAttribValue("name", "j%d" % i)
    p.setAttribValue("transform", ident)
    pts.append(p)
for a, b in zip(pts[:-1], pts[1:]):
    poly = geo.createPolygon(False)
    poly.addVertex(a)
    poly.addVertex(b)
"""


def line(label, text):
    print("   %-40s %s" % (label, text))


def report(node, label):
    t0 = time.perf_counter()
    try:
        node.cook(force=True)
        dt = time.perf_counter() - t0
        geo = node.geometry()
        errs = node.errors()
        warns = node.warnings()
        line(label, "cook %.3fs  %s  errors=%s warnings=%s" % (
            dt, ("%d pts %d prims" % (len(geo.points()), len(geo.prims()))) if geo else "no geometry",
            [e[:300] for e in errs], [w[:90] for w in warns]))
        return geo
    except hou.OperationFailed as exc:
        line(label, "cook FAILED: %s | errors=%s" % (str(exc)[:100], [e[:300] for e in node.errors()]))
        return None


def attribs(geo, kind):
    return sorted(a.name() for a in (geo.pointAttribs() if kind == "point" else geo.globalAttribs()))


def main():
    print("Houdini %s, licence %s" % (hou.applicationVersionString(), hou.licenseCategory()))
    geo = hou.node("/obj").createNode("geo", "rigging_spike")
    sop = hou.sopNodeTypeCategory()

    print("\n== Existence of the nodes (exact type names) ==")
    for name in ("kinefx::skeleton", "kinefx::rigpose", "kinefx::rigmatchpose", "kinefx::mappoints",
                 "kinefx::skeletonblend::2.0", "kinefx::fullbodyik", "kinefx::rigdoctor", "kinefx::jointdeform",
                 "kinefx::jointcapturebiharmonic", "kinefx::jointcaptureproximity", "kinefx::jointcapturepaint",
                 "kinefx::capturepackedgeo", "kinefx::attachjointgeo", "kinefx::biped_setup", "kinefx::biped_retarget",
                 "kinefx::orientjoints", "kinefx::rigstashpose", "kinefx::skeletonmirror", "kinefx::rigmirrorpose",
                 "kinefx::fbikconfiguretargets", "kinefx::configurejoints", "kinefx::dembones_skinningconverter",
                 "apex::autorigbuilder", "apex::autorigcomponent::3.0", "apex::packcharacter",
                 "bonedeform", "bonecapturebiharmonic", "captureproximity", "capture", "capturecorrect",
                 "bonecapturelines", "captureattribunpack", "captureattribpack", "capturemirror", "captureoverride",
                 "weightarraybiharmonic", "weightarrayinterpolate", "topotransfer", "topolandmark", "shrinkwrap",
                 "attribtransfer", "matchtopology", "attribblur", "attribsmooth", "smooth", "polyreduce", "remesh",
                 "tetconform", "tetembed", "ml_deform", "xform", "pointdeform", "skin"):
        nt = sop.nodeType(name)
        line(name, "present" if nt else "MISSING")

    print("\n== Inputs and outputs (min/max inputs, input names, output names) ==")
    for name in ("kinefx::skeleton", "kinefx::rigpose", "kinefx::rigmatchpose", "kinefx::mappoints",
                 "kinefx::skeletonblend::2.0", "kinefx::fullbodyik", "kinefx::rigdoctor", "kinefx::jointdeform",
                 "kinefx::jointcapturebiharmonic", "kinefx::jointcaptureproximity", "kinefx::jointcapturepaint",
                 "kinefx::capturepackedgeo", "kinefx::biped_setup", "kinefx::skeletonmirror", "kinefx::rigstashpose",
                 "apex::autorigbuilder", "bonedeform", "captureattribunpack", "captureattribpack", "topotransfer",
                 "topolandmark", "attribtransfer", "attribblur", "weightarrayinterpolate", "shrinkwrap"):
        nt = sop.nodeType(name)
        if not nt:
            continue
        node = geo.createNode(name)
        line(name, "inputs %d-%d %s | outputs %s" % (nt.minNumInputs(), nt.maxNumInputs(), list(node.inputNames()),
                                                       list(node.outputNames())))
        node.destroy()

    print("\n== Cooks on made-up data ==")
    tube = geo.createNode("tube", "tube")
    tube.parm("type").set(1)  # polygon mesh
    tube.parm("rad1").set(0.4)
    tube.parm("rad2").set(0.4)
    tube.parm("height").set(2.0)
    tube.parm("rows").set(30)
    tube.parm("cols").set(16)
    tube.parm("cap").set(1)
    report(tube, "tube")
    tri = geo.createNode("divide", "triangulate")
    tri.setInput(0, tube)
    tri.parm("convex").set(1)
    tri.parm("numsides").set(3)
    report(tri, "triangulate")
    tube = tri
    skel = geo.createNode("python", "skel")
    skel.parm("python").set(SKELETON_CODE)
    report(skel, "python skeleton (3 joints)")

    for typename, label in (("kinefx::skeleton", "kinefx::skeleton (no input)"),):
        n = geo.createNode(typename)
        report(n, label)

    jcb = geo.createNode("kinefx::jointcapturebiharmonic")
    jcb.setInput(0, tube)
    jcb.setInput(1, skel)
    g = report(jcb, "jointcapturebiharmonic")
    if g:
        line("  point attribs", attribs(g, "point"))
        line("  boneCapture sample", str(g.points()[10].attribValue("boneCapture")) if g.findPointAttrib("boneCapture") else "none")

    jcp = geo.createNode("kinefx::jointcaptureproximity")
    jcp.setInput(0, tube)
    jcp.setInput(1, skel)
    g = report(jcp, "jointcaptureproximity")
    if g:
        line("  boneCapture sample", str(g.points()[10].attribValue("boneCapture")) if g.findPointAttrib("boneCapture") else "none")

    pose = geo.createNode("kinefx::rigpose")
    pose.setInput(0, skel)
    report(pose, "rigpose")

    deform = geo.createNode("kinefx::jointdeform")
    deform.setInput(0, jcb)
    deform.setInput(1, skel)
    deform.setInput(2, pose)
    report(deform, "jointdeform")

    unpack = geo.createNode("captureattribunpack")
    unpack.setInput(0, jcb)
    g = report(unpack, "captureattribunpack")
    if g:
        line("  point attribs", attribs(g, "point"))
        line("  detail attribs", attribs(g, "detail"))

    pack = geo.createNode("captureattribpack")
    pack.setInput(0, unpack)
    g = report(pack, "captureattribpack (round trip)")
    if g:
        line("  point attribs", attribs(g, "point"))

    for typename, inputs, label in (
            ("kinefx::rigdoctor", [skel], "rigdoctor"),
            ("kinefx::skeletonblend::2.0", [skel, pose], "skeletonblend 2.0"),
            ("kinefx::rigmatchpose", [skel, skel], "rigmatchpose"),
            ("kinefx::mappoints", [skel, skel], "mappoints"),
            ("kinefx::fullbodyik", [skel], "fullbodyik (no targets)"),
            ("kinefx::biped_setup", [skel], "biped_setup"),
            ("kinefx::rigstashpose", [skel], "rigstashpose"),
            ("kinefx::skeletonmirror", [skel], "skeletonmirror"),
            ("kinefx::orientjoints", [skel], "orientjoints"),
            ("topolandmark", [tube, tube], "topolandmark"),
            ("topotransfer", [tube, tube], "topotransfer (no landmarks)"),
            ("weightarraybiharmonic", [tube], "weightarraybiharmonic"),
            ("shrinkwrap", [tube, tube], "shrinkwrap"),
            ("apex::autorigbuilder", [], "apex::autorigbuilder (no input)"),
    ):
        n = geo.createNode(typename)
        try:
            for i, src in enumerate(inputs):
                n.setInput(i, src)
        except hou.InvalidInput:
            line(label, "cannot wire %d inputs (node takes %d)" % (len(inputs), len(n.inputConnectors())))
            continue
        report(n, label)

    geo.destroy()


if __name__ == "__main__":
    main()

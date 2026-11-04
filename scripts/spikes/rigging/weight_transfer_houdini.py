# Spike for task #60 (character rigging design): the Houdini-native routes for moving skin weights, measured on the
# same sample body and the same made-up targets as weight_transfer_numpy.py:
#   N1  Attribute Transfer SOP on one float attribute per bone (point to point, Houdini's own search structures)
#   N2  a VEX wrangle: xyzdist + primuv, i.e. closest point on the surface, with the same distance and normal checks
#   N3  Attribute Blur SOP as the "fill" for the points N2 rejects (Jacobi-like diffusion, native, no scipy)
#   N4  Joint Capture Proximity and Joint Capture Biharmonic from a skeleton (no sample weights at all) as a baseline
# Also tries Attribute Transfer on the packed boneCapture attribute and on the unpacked array attributes.
# Nothing is stored: the Samples are read at run time and only numbers are printed.
#
# Run (headless hython; no HOUDINI_PACKAGE_SKIP, the KineFX nodes need the default packages):
#   "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/spikes/rigging/weight_transfer_houdini.py
#
# SPDX-License-Identifier: GPL-3.0-or-later

import builtins
import os
import sys
import time

import hou
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sample_facts  # noqa: E402
import weight_transfer_numpy as wt  # noqa: E402
from a3ob_core.p3d.reader import read_p3d_file  # noqa: E402

BUILD_CODE = """
import builtins, hou
d = builtins.RIG_SPIKE["%s"]
geo = hou.pwd().geometry()
geo.clear()
P, tris, W = d["P"], d["tris"], d.get("W")
geo.createPoints([tuple(float(x) for x in p) for p in P])
geo.createPolygons([tuple(int(i) for i in t) for t in tris])
if W is not None:
    for j in range(W.shape[1]):
        geo.addAttrib(hou.attribType.Point, "w_%%d" %% j, 0.0)
        geo.setPointFloatAttribValues("w_%%d" %% j, W[:, j].astype(float).tolist())
"""

SKELETON_CODE = """
import builtins, hou
d = builtins.RIG_SPIKE["skeleton"]
geo = hou.pwd().geometry()
geo.clear()
ident = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
geo.addAttrib(hou.attribType.Point, "name", "")
geo.addAttrib(hou.attribType.Point, "transform", ident)
index = {}
for name, pos, parent in d:
    p = geo.createPoint()
    p.setPosition(pos)
    p.setAttribValue("name", name)
    p.setAttribValue("transform", ident)
    index[name] = p
for name, pos, parent in d:
    if parent in index:
        poly = geo.createPolygon(False)
        poly.addVertex(index[parent])
        poly.addVertex(index[name])
"""

VEX_CLOSEST = """
// Closest point on the source surface (input 2), with a distance check and a normal check, then the per-bone weights
// are read at that surface position. Points that fail a check get i@accepted = 0 and keep zero weights.
int sourcePrim;
vector surfaceUV;
float distanceToSource = xyzdist(1, v@P, sourcePrim, surfaceUV);
vector sourceNormal = normalize(primuv(1, "N", sourcePrim, surfaceUV));
float normalAgreement = dot(sourceNormal, normalize(v@N));
i@accepted = distanceToSource <= ch("maxDistance") && normalAgreement >= cos(radians(ch("maxAngle")));
f@distanceToSource = distanceToSource;
if (i@accepted) {
    for (int bone = 0; bone < chi("boneCount"); bone++) {
        string name = sprintf("w_%d", bone);
        float boneWeight = primuv(1, name, sourcePrim, surfaceUV);
        setpointattrib(0, name, @ptnum, boneWeight);
    }
}
"""


def stash(key, **data):
    if not hasattr(builtins, "RIG_SPIKE"):
        builtins.RIG_SPIKE = {}
    builtins.RIG_SPIKE[key] = data


def python_sop(parent, name, key, code=BUILD_CODE):
    node = parent.createNode("python", name)
    node.parm("python").set(code % key if "%s" in code else code)
    return node


def read_weights(geo, bone_count):
    return np.stack([np.array(geo.pointFloatAttribValues("w_%d" % j)) for j in range(bone_count)], axis=1)


def timed_cook(node):
    t0 = time.perf_counter()
    node.cook(force=True)
    return time.perf_counter() - t0


def normalise(W):
    total = W.sum(1, keepdims=True)
    return np.where(total > 1e-9, W / np.maximum(total, 1e-9), W)


def main():
    print("Houdini %s, numpy %s" % (hou.applicationVersionString(), np.__version__))
    src_P, src_tris, src_W, names = wt.load_sample_lod(0)
    B = len(names)
    diag = float(np.linalg.norm(src_P.max(0) - src_P.min(0)))
    centre = (src_P.max(0) + src_P.min(0)) / 2.0
    scale = (1.05, 1.10, 1.15)
    tgt_P = wt.make_target(src_P, scale, 0.002, 1, centre)
    lod2_P, lod2_tris, lod2_W, _ = wt.load_sample_lod(1)
    sub_P, sub_tris, sub_W = wt.subdivide_midpoint(lod2_P, lod2_tris, lod2_W)
    sub_P = wt.make_target(sub_P, scale, 0.002, 2, centre)
    big_P, big_tris, big_W = wt.subdivide_midpoint(src_P, src_tris, src_W)
    big_P = wt.make_target(big_P, scale, 0.002, 3, centre)

    stash("src", P=src_P, tris=src_tris, W=src_W)
    stash("tgt", P=tgt_P, tris=src_tris)
    stash("sub", P=sub_P, tris=sub_tris)
    stash("big", P=big_P, tris=big_tris)

    root = hou.node("/obj").createNode("geo", "transfer_spike")
    src = python_sop(root, "src", "src")
    src_n = root.createNode("normal", "src_normals")
    src_n.setInput(0, src)
    targets = {}
    for key, truth, label in (("tgt", src_W, "A scaled copy, %d pts" % len(tgt_P)),
                              ("sub", sub_W, "B subdivided LOD2, %d pts" % len(sub_P)),
                              ("big", big_W, "C subdivided LOD1, %d pts" % len(big_P))):
        t = python_sop(root, key, key)
        tn = root.createNode("normal", key + "_normals")
        tn.setInput(0, t)
        targets[key] = (tn, truth, label)

    print("\n== N1: Attribute Transfer SOP, one float attribute per bone (%d attributes), point to point ==" % B)
    for key, (tn, truth, label) in targets.items():
        for count, thr in ((1, 0.108), (4, 0.108)):
            at = root.createNode("attribtransfer")
            at.setInput(0, tn)
            at.setInput(1, src)
            at.parm("pointattribs").set(1)
            at.parm("pointattriblist").set("w_*")
            at.parm("primitiveattribs").set(0)
            at.parm("maxsamplecount").set(count)
            at.parm("kernelradius").set(thr)
            at.parm("thresholddist").set(thr)
            t = timed_cook(at)
            W = read_weights(at.geometry(), B)
            unmatched = int((W.sum(1) < 0.5).sum())
            print("  %s | samples %d, radius %.3f m | cook %.2fs | points without weights %d" % (label, count, thr, t, unmatched))
            wt.error_report(normalise(W), truth, "N1 transferred, normalised")
            at.destroy()

    print("\n== N1b: Attribute Transfer on boneCapture (packed) and on the unpacked arrays ==")
    # Build a packed boneCapture on the source from the dense weights, through the unpacked array attributes.
    pack_code = """
import builtins, hou
d = builtins.RIG_SPIKE["src"]
names = builtins.RIG_SPIKE["names"]
geo = hou.pwd().geometry()
geo.clear()
geo.createPoints([tuple(float(x) for x in p) for p in d["P"]])
geo.createPolygons([tuple(int(i) for i in t) for t in d["tris"]])
W = d["W"]
geo.addArrayAttrib(hou.attribType.Point, "boneCapture_index", hou.attribData.Int, 1)
geo.addArrayAttrib(hou.attribType.Point, "boneCapture_data", hou.attribData.Float, 1)
geo.addArrayAttrib(hou.attribType.Global, "boneCapture_pCaptPath", hou.attribData.String, 1)
geo.setGlobalAttribValue("boneCapture_pCaptPath", tuple(names))
for i, pt in enumerate(geo.points()):
    idx = [int(j) for j in W[i].nonzero()[0]]
    pt.setAttribValue("boneCapture_index", tuple(idx))
    pt.setAttribValue("boneCapture_data", tuple(float(W[i][j]) for j in idx))
"""
    builtins.RIG_SPIKE["names"] = names
    unp = root.createNode("python", "unpacked_source")
    unp.parm("python").set(pack_code)
    try:
        t = timed_cook(unp)
        print("  built unpacked arrays on the source: %.2fs, errors=%s" % (t, unp.errors()))
        for aname in ("boneCapture_index", "boneCapture_data"):
            print("   array attr %s present: %s" % (aname, unp.geometry().findPointAttrib(aname) is not None))
        tgt_unp = root.createNode("attribtransfer")
        tgt_unp.setInput(0, targets["tgt"][0])
        tgt_unp.setInput(1, unp)
        tgt_unp.parm("pointattriblist").set("boneCapture_index boneCapture_data")
        tgt_unp.parm("primitiveattribs").set(0)
        tgt_unp.parm("maxsamplecount").set(1)
        tgt_unp.parm("kernelradius").set(0.108)
        tgt_unp.parm("thresholddist").set(0.108)
        tgt_unp.cook(force=True)
        g = tgt_unp.geometry()
        print("  Attribute Transfer on array attributes: errors=%s warnings=%s; has index=%s data=%s" % (
            tgt_unp.errors(), tgt_unp.warnings(), g.findPointAttrib("boneCapture_index") is not None,
            g.findPointAttrib("boneCapture_data") is not None))
        if g.findPointAttrib("boneCapture_index") is not None:
            p0 = g.points()[100]
            print("   sample point 100: index %s data %s" % (p0.attribValue("boneCapture_index"), p0.attribValue("boneCapture_data")))
            print("   (one nearest source point is copied; a blend of several source points is NOT possible with arrays)")
        pk = root.createNode("captureattribpack")
        pk.setInput(0, tgt_unp)
        pk.cook(force=True)
        print("  captureattribpack on the transferred arrays: errors=%s; boneCapture present=%s" % (
            pk.errors(), pk.geometry().findPointAttrib("boneCapture") is not None))
    except hou.OperationFailed as exc:
        print("  failed: %s %s" % (str(exc)[:100], unp.errors()))

    print("\n== N2: VEX wrangle, closest point on the source surface + normal check (%d bones) ==" % B)
    n2_results = {}
    for key, (tn, truth, label) in targets.items():
        wr = root.createNode("attribwrangle")
        wr.setInput(0, tn)
        wr.setInput(1, src_n)
        wr.parm("class").set(2)
        wr.parm("snippet").set(VEX_CLOSEST)
        ptg = wr.parmTemplateGroup()
        for pname, val, label_ in (("maxDistance", 0.108, "Max distance"), ("maxAngle", 30.0, "Max angle")):
            ptg.append(hou.FloatParmTemplate(pname, label_, 1, default_value=(val,)))
        ptg.append(hou.IntParmTemplate("boneCount", "Bones", 1, default_value=(B,)))
        wr.setParmTemplateGroup(ptg)
        # The per-bone attributes must exist on the target before setpointattrib can write them: declare them first.
        declare = root.createNode("python", "declare_" + key)
        declare.setInput(0, tn)
        declare.parm("python").set(
            "import hou\ngeo = hou.pwd().geometry()\nfor j in range(%d):\n    geo.addAttrib(hou.attribType.Point, 'w_%%d' %% j, 0.0)\n" % B)
        wr.setInput(0, declare)
        try:
            t = timed_cook(wr)
        except hou.OperationFailed:
            print("  wrangle failed: %s" % [e[:600] for e in wr.errors()])
            raise
        g = wr.geometry()
        accepted = np.array(g.pointIntAttribValues("accepted"))
        W = read_weights(g, B)
        print("  %s | cook %.2fs | accepted %d, rejected %d (errors=%s)" % (label, t, int(accepted.sum()), int((accepted == 0).sum()), wr.errors()[:1]))
        wt.error_report(np.where(accepted[:, None] == 1, W, 0.0), np.where(accepted[:, None] == 1, truth, 0.0), "N2 accepted points only")
        n2_results[key] = (wr, W, accepted)

    print("\n== N3: Attribute Blur SOP as the fill for rejected points (target A) ==")
    wr, W, accepted = n2_results["tgt"]
    truth = targets["tgt"][1]
    # Group of the rejected points, made by a Python SOP; a blur restricted to the group diffuses weights inward.
    stash("rejected", mask=(accepted == 0))
    grp = root.createNode("python", "group_rejected")
    grp.setInput(0, wr)
    grp.parm("python").set(
        "import builtins, hou\nm = builtins.RIG_SPIKE['rejected']['mask']\ngeo = hou.pwd().geometry()\n"
        "g = geo.createPointGroup('rejected')\nfor i, p in enumerate(geo.points()):\n    if m[i]:\n        g.add(p)\n")
    for iters in (50, 200, 800):
        blur = root.createNode("attribblur")
        blur.setInput(0, grp)
        blur.parm("group").set("rejected")
        blur.parm("attributes").set(" ".join("w_%d" % j for j in range(B)))
        blur.parm("iterations").set(iters)
        blur.parm("pinborder").set(0)
        t = timed_cook(blur)
        Wb = read_weights(blur.geometry(), B)
        print("  iterations %d: cook %.2fs, errors=%s warnings=%s" % (iters, t, blur.errors()[:1], blur.warnings()[:1]))
        wt.error_report(normalise(Wb), truth, "N2 + N3 blur(%d)" % iters)
    # The numpy CG fill on the same rejected set, for comparison:
    ei, ej, w = wt.cotan_edges(tgt_P, src_tris)
    lap = wt.Laplacian(len(tgt_P), ei, ej, w)
    t0 = time.perf_counter()
    Wc, it, res = wt.inpaint(lap, accepted == 1, W)
    print("  numpy CG fill on the same points: %.2fs, %d iterations" % (time.perf_counter() - t0, it))
    wt.error_report(normalise(np.maximum(Wc, 0)), truth, "N2 + numpy CG inpaint")

    print("\n== N5: fit first. Topo Transfer SOP (non-rigid, no landmarks) deforms the sample body onto the target ==")
    print("  Then the weights ride on the deformed template and the closest-point transfer starts from an aligned mesh.")
    # Topo Transfer needs one connected component: keep the biggest piece of the body (3103 of 3125 points).
    ids, tris_main, _ = wt.drop_small_pieces(np.arange(len(src_P), dtype=np.float64)[:, None], src_tris, src_W, 1000)
    keep = ids[:, 0].astype(np.int64)
    main_src_P, main_W = src_P[keep], src_W[keep]
    main_tgt_P = tgt_P[keep]
    stash("msrc", P=main_src_P, tris=tris_main, W=main_W)
    stash("mtgt", P=main_tgt_P, tris=tris_main)
    msrc = python_sop(root, "msrc", "msrc")
    mtgt = python_sop(root, "mtgt", "mtgt")
    tt = root.createNode("topotransfer")
    tt.setInput(0, msrc)
    tt.setInput(1, mtgt)
    tt.parm("enablelandmarks").set(0)
    try:
        t = timed_cook(tt)
    except hou.OperationFailed:
        print("  topotransfer failed: %s" % [e[:300] for e in tt.errors()])
        t = None
    if t is not None:
        flat = np.array(tt.geometry().pointFloatAttribValues("P")).reshape(-1, 3)
        before = np.linalg.norm(main_src_P - main_tgt_P, axis=1)  # same index = same body point
        after = np.linalg.norm(flat - main_tgt_P, axis=1)
        print("  scaled copy, %d points: cook %.2fs warnings=%s" % (len(flat), t, [w[:100] for w in tt.warnings()]))
        print("   distance of each template point to its true counterpart (m): before mean %.4f max %.4f | after mean %.4f max %.4f"
              % (before.mean(), before.max(), after.mean(), after.max()))
        for label, template_P in (("no fit", main_src_P), ("Topo Transfer fit", flat)):
            W, info = wt.transfer(template_P, tris_main, main_W, main_tgt_P, tris_main, max_distance=0.05 * diag)
            print("   closest-point transfer, template = %s: matched %d, inpainted %d, transfer %.2fs" % (
                label, info["matched"], info["n"] - info["matched"], info["t_total"]))
            wt.error_report(W, main_W, "%s + inpaint" % label)
    tt.destroy()

    print("\n== N4: Joint Capture Proximity / Biharmonic from the pivots skeleton, no sample weights ==")
    addons = sample_facts.ADDONS
    bones = sample_facts.parse_skeleton(os.path.join(addons, "Test_Character_01", "model.cfg"), "OFP2_ManSkeleton")
    pv = read_p3d_file(os.path.join(addons, "TemplateRTM", "ManSkeleton_Pivots.p3d"), strict=False).lods[0]
    piv = {}
    for sel in pv.selection_names():
        member = pv.selection(sel).members()
        pos = np.mean([pv.point(m) for m in member], axis=0)
        piv[sel.lower()] = tuple(float(x) for x in pos)
    skel = []
    for bone, parent in bones.items():
        if bone.lower() in piv and bone.lower() != "hips":
            skel.append((bone, piv[bone.lower()], parent))
    print("  skeleton from model.cfg + pivots: %d joints with a pivot" % len(skel))
    stash("skeleton", data=None)
    builtins.RIG_SPIKE["skeleton"] = skel
    sk = root.createNode("python", "skeleton")
    sk.parm("python").set(SKELETON_CODE)
    sk.cook(force=True)
    print("  skeleton SOP: %d points %d prims, errors=%s" % (len(sk.geometry().points()), len(sk.geometry().prims()), sk.errors()[:1]))
    src_geo_node = python_sop(root, "src_plain", "src")  # triangle mesh without weights attributes is fine too
    name_to_col = {n.lower(): j for j, n in enumerate(names)}
    for typename, label in (("kinefx::jointcaptureproximity", "proximity"), ("kinefx::jointcapturebiharmonic", "biharmonic")):
        cap = root.createNode(typename)
        cap.setInput(0, src_geo_node)
        cap.setInput(1, sk)
        t = timed_cook(cap)
        print("  %s: cook %.2fs errors=%s warnings=%s" % (label, t, [e[:160] for e in cap.errors()], [w[:100] for w in cap.warnings()]))
        if cap.errors():
            continue
        unp2 = root.createNode("captureattribunpack")
        unp2.setInput(0, cap)
        unp2.cook(force=True)
        g = unp2.geometry()
        paths = g.attribValue("boneCapture_pCaptPath")
        idx = g.pointIntListAttribValues("boneCapture_index") if hasattr(g, "pointIntListAttribValues") else None
        W2 = np.zeros((len(g.points()), B))
        for i, pt in enumerate(g.points()):
            ids = pt.attribValue("boneCapture_index")
            data = pt.attribValue("boneCapture_data")
            for k, d in zip(ids, data):
                col = name_to_col.get(os.path.basename(paths[k]).lower())
                if col is not None:
                    W2[i, col] += d
        print("   captured joints: %d, example path %r" % (len(paths), paths[0] if paths else None))
        # Only the main-component points exist in src (proxies were dropped), so the order matches src_W.
        wt.error_report(normalise(W2), src_W, "%s vs Bohemia weights" % label)
    root.destroy()


if __name__ == "__main__":
    main()

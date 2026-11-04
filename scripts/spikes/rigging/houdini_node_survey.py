# Spike for task #60 (character rigging design): which KineFX / APEX nodes and Python modules exist in this Houdini.
# Prints node type names with descriptions, whether each cooks headless in Indie, and which Python modules import.
#
# Run (headless hython only, never the GUI):
#   (no HOUDINI_PACKAGE_SKIP: with it the KineFX/APEX packages do not load and 181 node types go missing)
#   "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/spikes/rigging/houdini_node_survey.py
#
# SPDX-License-Identifier: GPL-3.0-or-later

import importlib
import re

import hou

KEYWORDS = re.compile(
    r"capture|rig|skeleton|joint|bone|kinefx|apex|autorig|pose|ik|skin|deform|landmark|biharmonic|mappoints|map ?points|"
    r"transfer|proximity|weight|fbik|fullbody|ragdoll|motionclip|mixamo|ml|segment",
    re.I)
CATEGORY_NAMES = ("Sop", "Object", "Dop")


def survey_types():
    print("== Houdini %s, node types whose name or label matches a rigging keyword ==" % hou.applicationVersionString())
    for cat_name in CATEGORY_NAMES:
        cat = hou.nodeTypeCategories()[cat_name]
        rows = []
        for name, nt in cat.nodeTypes().items():
            label = nt.description()
            text = name + " " + label
            if re.search(r"^kinefx|^apex|rig|capture|skeleton|joint|bone|autorig|biharmonic|fbik|fullbody|ragdoll|skin|"
                         r"mappoints|map ?points|motionclip|proximity|landmark|^ml_|topo", text, re.I):
                if nt.hidden():
                    continue
                rows.append((name, label))
        print("-- category %s: %d matches" % (cat_name, len(rows)))
        for name, label in sorted(rows):
            print("   %-52s %s" % (name, label))
    print("   (the KineFX/APEX nodes only exist when the default Houdini packages load: do NOT set HOUDINI_PACKAGE_SKIP)")


def survey_python_modules():
    print("\n== Python modules inside hython ==")
    for mod in ("numpy", "scipy", "scipy.sparse", "scipy.sparse.linalg", "scipy.spatial", "torch", "sklearn", "igl",
                "trimesh", "open3d", "pymeshlab", "numba", "cv2", "networkx", "onnxruntime", "PySide6"):
        try:
            m = importlib.import_module(mod)
            print("   %-22s OK  %s" % (mod, getattr(m, "__version__", "")))
        except Exception as exc:  # report every failure kind
            print("   %-22s NO  (%s: %s)" % (mod, type(exc).__name__, str(exc)[:70]))


def try_cook(type_name, category="Sop", make_input=None):
    """Create the node in a scratch geo, cook it, and print the result. make_input(geo) -> node to wire in, or None."""
    geo = hou.node("/obj").createNode("geo", "survey_scratch")
    try:
        node = geo.createNode(type_name)
    except Exception as exc:
        print("   %-48s cannot create: %s" % (type_name, str(exc)[:80]))
        geo.destroy()
        return
    try:
        if make_input:
            node.setInput(0, make_input(geo))
        node.cook(force=True)
        errs = node.errors()
        warns = node.warnings()
        g = node.geometry()
        print("   %-48s created; cook: %d points, %d prims, errors=%s warnings=%s" % (
            type_name, len(g.points()) if g else -1, len(g.prims()) if g else -1, errs[:1], warns[:1]))
    except Exception as exc:
        print("   %-48s created; cook raised %s" % (type_name, str(exc)[:100]))
    geo.destroy()


def main():
    survey_types()
    survey_python_modules()
    print("\n== Create + cook, Indie licence check (licence: %s) ==" % hou.licenseCategory())
    names = ("skeleton", "rigpose", "rigmatchpose", "mappoints", "skeletonblend", "fullbodyik", "rigdoctor",
             "bonedeform", "jointdeform", "capturepackage", "bonecapture", "bonecapturebiharmonic",
             "jointcapturebiharmonic", "capturepoximity", "captureproximity", "capturecorrect", "bonecapturelines",
             "capturelayerpaint", "attribtransfer", "autorigbuilder", "apex_autorig", "skeletonmirror",
             "kinefx::skeleton", "kinefx::rigpose")
    sop = hou.sopNodeTypeCategory().nodeTypes()
    for n in names:
        print("   exact type %-34s %s" % (n, "present" if n in sop else "missing"))


if __name__ == "__main__":
    main()

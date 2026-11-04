# Spike for task #60 (character rigging design): facts about the rigged sample character and the pivots model.
# Reads the Arma 3 Samples at run time from the owner's disk and prints numbers only. No Samples bytes are stored.
#
# Run (plain python, no Houdini needed):
#   python scripts/spikes/rigging/sample_facts.py
# Environment: A3OB_SAMPLES = Samples root (default: the Steam path in tests/samples_support.py).
#
# SPDX-License-Identifier: GPL-3.0-or-later

import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "python"))

from a3ob_core.p3d.model import decode_weight  # noqa: E402
from a3ob_core.p3d.reader import read_p3d_file  # noqa: E402

SAMPLES = os.environ.get("A3OB_SAMPLES") or "E:/SteamLibrary/steamapps/common/Arma 3 Samples"
ADDONS = os.path.join(SAMPLES, "Addons")


def parse_skeleton(cfg_path, class_name):
    """{bone: parent} in file order for one CfgSkeletons class (no skeletonInherit handling: a spike)."""
    text = open(cfg_path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"//[^\n]*", "", text)
    start = text.index("class " + class_name)
    body = text[text.index("skeletonBones[]", start):]
    body = body[:body.index("}")]
    names = re.findall(r'"([^"]*)"', body)
    return {names[i]: names[i + 1] for i in range(0, len(names), 2)}


def main():
    cfg = os.path.join(ADDONS, "Test_Character_01", "model.cfg")
    bones = parse_skeleton(cfg, "OFP2_ManSkeleton")
    lower_bones = {b.lower() for b in bones}  # Arma bone names are case-insensitive (selection 'Head' is bone 'head')
    body_bones = {b for b in bones if not b.startswith("Face_") and not b.startswith("Eye")}
    print("model.cfg OFP2_ManSkeleton: %d bones, %d without Face_*/Eye*" % (len(bones), len(body_bones)))

    sample = read_p3d_file(os.path.join(ADDONS, "Test_Character_01", "A3_character_example.p3d"), strict=False)
    print("\nA3_character_example.p3d: %d LODs" % len(sample.lods))
    print("%-3s %-26s %6s %6s %6s %6s %6s %7s %8s %8s %s" % (
        "i", "LOD", "points", "faces", "sels", "bones", "w>0", "maxinf", "sum_min", "sum_max", "distinct weight bytes"))
    for i, lod in enumerate(sample.lods):
        names = lod.selection_names()
        bone_sels = [lod.selection(n) for n in names if n.lower() in lower_bones]
        n = lod.point_count
        count = [0] * n
        total = [0.0] * n
        steps = collections.Counter()
        for sel in bone_sels:
            vw = sel.vertex_weights
            for p in range(n):
                if vw[p]:
                    count[p] += 1
                    total[p] += decode_weight(vw[p])
                    steps[vw[p]] += 1
        weighted = [p for p in range(n) if count[p]]
        maxinf = max(count) if n else 0
        sums = [total[p] for p in weighted]
        print("%-3d %-26s %6d %6d %6d %6d %6d %7d %8s %8s %s" % (
            i, lod.name, n, lod.face_count, len(names), len(bone_sels), len(weighted), maxinf,
            "%.4f" % min(sums) if sums else "-", "%.4f" % max(sums) if sums else "-",
            "%d (lowest byte step: 1/254)" % len(steps) if steps else "-"))
        if bone_sels:
            influence_hist = collections.Counter(count[p] for p in weighted)
            print("      points per influence count: %s; unweighted points: %d"
                  % (dict(sorted(influence_hist.items())), n - len(weighted)))
            others = [x for x in names if x.lower() not in lower_bones]
            print("      non-bone selections (%d): %s" % (len(others), ", ".join(others[:12]) + (" ..." if len(others) > 12 else "")))
            unused = sorted(b for b in body_bones if b.lower() not in {s.name.lower() for s in bone_sels})
            print("      body bones with no selection here: %d" % len(unused))
        else:
            print("      no bone selections; selections: %s" % ", ".join(names[:12]))

    pivots = read_p3d_file(os.path.join(ADDONS, "TemplateRTM", "ManSkeleton_Pivots.p3d"), strict=False)
    print("\nManSkeleton_Pivots.p3d: %d LODs" % len(pivots.lods))
    for i, lod in enumerate(pivots.lods):
        names = lod.selection_names()
        print("  LOD %d %s: %d points, %d faces, %d selections" % (i, lod.name, lod.point_count, lod.face_count, len(names)))
        if lod.point_count and names:
            single = 0
            matched = 0
            for nm in names:
                m = lod.selection(nm).members()
                if len(m) == 1:
                    single += 1
                if nm.lower() in lower_bones:
                    matched += 1
            print("    selections with exactly one point: %d; names that are model.cfg bones: %d" % (single, matched))
            print("    names that are not bones (case-insensitive): %s" % [x for x in names if x.lower() not in lower_bones])
            print("    bones (body) with no pivot selection: %s" % sorted(b for b in body_bones if b.lower() not in {x.lower() for x in names}))
            first = names[0]
            m = lod.selection(first).members()
            print("    e.g. %s -> point %s" % (first, [tuple(round(v, 3) for v in lod.point(p)) for p in m[:2]]))

    print("\nOther sample models in Test_Character_01 (LOD list, bone-weighted LODs)")
    for fname in ("A3_character_template.p3d", "A3_vest_example.p3d", "A3_headgear_example.p3d"):
        m = read_p3d_file(os.path.join(ADDONS, "Test_Character_01", fname), strict=False)
        row = []
        for lod in m.lods:
            nb = len([n for n in lod.selection_names() if n.lower() in lower_bones])
            row.append("%s(%dpts,%dbonesel)" % (lod.name, lod.point_count, nb))
        print("  %s: %s" % (fname, "; ".join(row)))


if __name__ == "__main__":
    main()

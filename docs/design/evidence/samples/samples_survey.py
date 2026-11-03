# SPDX-License-Identifier: GPL-3.0-or-later
"""Survey of the Arma 3 Samples through the Houdini nodes (task #29, D6, OQ-5, OQ-11). Run with hython from the repo root:

    "<Houdini>/bin/hython.exe" docs/design/evidence/samples/samples_survey.py > samples_survey_out.txt

Needs the Samples (A3OB_SAMPLES, default the Steam path). Writes counts, timings, rule ids and our own messages. It prints no
Samples bytes: quoted names inside messages are replaced by <name>, and files are named by their path under the Samples root.
Sections: 1 TAGG classes and strings (M1), 2 Lossless round trip per file, 3 Extract LOD + Assemble LODs, 4 Arma 3 release
export (warnings per rule and per file), 5 OQ-11 survival of blob TAGGs, 6 timings.
"""

import collections
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path[:0] = [os.path.join(REPO, "python"), REPO]

from a3ob_core import p3d  # noqa: E402
from tests import samples_support as ss  # noqa: E402
from a3ob_houdini import messages  # noqa: E402
from a3ob_houdini.p3d_export import flag_check, type_rules  # noqa: E402

if not ss.AVAILABLE:
    sys.exit(ss.SKIP_REASON)


def scrub(text):
    """A message without the names it quotes, and with numbers as N, so that equal complaints group."""
    text = re.sub(r" \(rule [^)]*\)", "", text)  # the rule id is printed on its own
    text = re.sub(r"'[^']*'", "'<name>'", text)
    text = re.sub(r"[A-Za-z]:\\[^ ]*", "<path>", text)
    text = re.sub(r"^[^:]*p3d_(import|export)\d*: ", "", text)
    return re.sub(r"\d+(\.\d+)?", "N", text)


def rule_of(text):
    found = re.search(r"\(rule (T2-[A-Z0-9-]+)\)", text)
    if found:
        return found.group(1)
    if "face_flags that Object Builder does not know" in text:
        return "FLAGS-UNKNOWN"
    if "lowercase makes the" in text:
        return "LOWERCASE-COLLISION"
    if "differ only in case" in text:
        return "CASE-COLLISION"
    if "not sorted" in text:
        return "LODS-NOT-SORTED"
    return "OTHER: " + scrub(text)[:90]


files = ss.all_files()
print("Arma 3 Samples survey, %d files" % len(files))

# -- 1. M1 level -------------------------------------------------------------------------------------------------------
classes = collections.Counter()
non_ascii = 0
strings = 0
identical_m1 = 0
unknown_flags = collections.Counter()
unknown_flag_files = collections.defaultdict(set)
quads = collections.Counter()
quad_lods = collections.defaultdict(set)
contig_types = type_rules.GEOMETRY_TYPES | type_rules.SHADOW_TYPES
for rel in files:
    data = open(ss.path(rel), "rb").read()
    model = p3d.read_p3d(data, strict=True)
    identical_m1 += p3d.write_p3d(model) == data
    for lod in model.lods:
        for value in lod.face_flags:
            if flag_check.unknown_face_flags(value):
                unknown_flags["0x%08x" % value] += 1
                unknown_flag_files["0x%08x" % value].add(rel)
        if lod.lod_type in contig_types:
            count = sum(1 for sides in lod.face_sides if sides == 4)
            if count:
                quads["faces"] += count
                quad_lods[rel].add(lod.name)
        for tagg in lod.taggs:
            classes[type(tagg).__name__] += 1
            if not tagg.active:
                classes["inactive"] += 1
        found = [t.name for t in lod.taggs] + list(lod.face_textures) + list(lod.face_materials)
        found += [t.key for t in lod.taggs if isinstance(t, p3d.PropertyTagg)]
        found += [t.value for t in lod.taggs if isinstance(t, p3d.PropertyTagg)]
        for text in found:
            strings += 1
            non_ascii += not text.isascii()
print("\n== 1. M1 layer ==")
print("strict read and byte-identical write: %d of %d files" % (identical_m1, len(files)))
print("TAGG classes (all LODs): " + ", ".join("%s %d" % item for item in sorted(classes.items())))
print("face flag values that flag_check calls unknown (value: faces, files): " + ", ".join(
    "%s: %d, %d" % (value, unknown_flags[value], len(unknown_flag_files[value])) for value in sorted(unknown_flags)))
print("quads in Geometry-family and Shadow Volume LODs (T2-TRI): %d faces in %d LODs in %d files" % (
    quads["faces"], sum(len(v) for v in quad_lods.values()), len(quad_lods)))
print("strings checked (TAGG names, textures, materials, property keys and values): %d; non-ASCII: %d" % (strings, non_ascii))

# -- 2. and 3. Houdini round trips ---------------------------------------------------------------------------------------
rig = ss.Rig()


def clear():
    rig.container.deleteItems(rig.container.children())


print("\n== 2. P3D Import -> P3D Export (Lossless) ==")
class_count = collections.Counter()
unexplained_total = 0
identical = 0
timings = []
survival = collections.Counter()
direct_bytes = {}
interleaved_total = 0
for rel in files:
    clear()
    r = rig.roundtrip(rel)
    direct_bytes[rel] = r["written"]
    source, written = r["source"], r["written"]
    interleaved = ss.interleaved_lod_names(r["import_warnings"])
    timings.append((rel, r["import_seconds"], r["export_seconds"]))
    errors = r["import_errors"] + r["report"].texts(messages.ERROR)
    if written == source:
        identical += 1
        print("IDENTICAL  %s  (%d LODs, import %.2f s, export %.2f s)" % (rel, r["lods"], r["import_seconds"],
                                                                         r["export_seconds"]))
        continue
    rows, unexplained = ss.compare_files(source, written, interleaved)
    unexplained_total += len(unexplained)
    first = ss.first_difference(written, source)
    print("DIFFERENT  %s  (%d LODs, import %.2f s, export %.2f s) errors=%s" % (
        rel, r["lods"], r["import_seconds"], r["export_seconds"], errors))
    print("    first differing byte %d: %s" % (first, ss.locate(source, first)))
    print("    LODs that differ: %d of %d; interleaved-proxy LODs: %d; unexplained: %d" % (
        len(rows), r["lods"], len(interleaved), len(unexplained)))
    for index, name, found in rows:
        print("      LOD %d (%s): %s" % (index, name, "; ".join(found)))
        for item in found:
            class_count[item] += 1
    interleaved_total += len(interleaved)
    # OQ-11: do the opaque TAGGs come back?
    model_a, model_b = p3d.read_p3d(source, strict=False), p3d.read_p3d(written, strict=False)
    for lod_a, lod_b in zip(model_a.lods, model_b.lods):
        for tagg in lod_a.taggs:
            if isinstance(tagg, (p3d.RawTagg, p3d.MaskTagg)):
                kind = "Raw" if isinstance(tagg, p3d.RawTagg) else "Mask"
                same = any(ss._tagg_view(tagg) == ss._tagg_view(other) for other in lod_b.taggs)
                survival[(kind, "bytes kept" if same else ("bytes changed, LOD interleaved" if lod_a.name in interleaved
                                                           else "bytes changed"))] += 1
for rel in files:
    if direct_bytes[rel] == open(ss.path(rel), "rb").read():
        model = p3d.read_p3d(direct_bytes[rel], strict=False)
        for lod in model.lods:
            for tagg in lod.taggs:
                if isinstance(tagg, (p3d.RawTagg, p3d.MaskTagg)):
                    survival[("Raw" if isinstance(tagg, p3d.RawTagg) else "Mask", "bytes kept")] += 1
print("\nbyte-identical: %d of %d files" % (identical, len(files)))
print("LOD differences by class (a LOD can have several): " + ", ".join("%s %d" % item for item in sorted(class_count.items())))
print("LODs with a difference that is not in the known list: %d" % unexplained_total)

print("\n== 3. Import -> Extract LOD k (all k) -> Assemble LODs (no edits) -> Lossless ==")
same_as_direct = 0
for rel in files:
    clear()
    r = rig.roundtrip(rel, via_assemble=True)
    same = r["written"] == direct_bytes[rel]
    same_as_direct += same
    print("%s  %s  (%d LODs, assemble %.2f s)" % ("SAME AS DIRECT" if same else "DIFFERENT     ", rel, r["lods"],
                                                 r["assemble_seconds"]))
print("same bytes as the direct round trip: %d of %d" % (same_as_direct, len(files)))

# -- 4. release export ---------------------------------------------------------------------------------------------------
print("\n== 4. Arma 3 release export ==")
per_rule = collections.defaultdict(collections.Counter)
per_rule_text = {}
failed = 0
for rel in files:
    clear()
    r = rig.roundtrip(rel, "Arma 3 release")
    errors = r["report"].texts(messages.ERROR)
    if errors or r["written"] is None:
        failed += 1
        print("FAILED %s: %s" % (rel, [scrub(e) for e in errors]))
    for kind, text in r["report"].items:
        if kind == messages.WARNING:
            rule = rule_of(text)
            per_rule[rule][rel] += 1
            per_rule_text.setdefault(rule, scrub(text))
print("files exported with no error: %d of %d" % (len(files) - failed, len(files)))
print("\nWarnings per rule (count = number of warnings; files = number of files):")
for rule in sorted(per_rule):
    print("  %-22s %4d warnings in %2d files   e.g. %s" % (rule, sum(per_rule[rule].values()), len(per_rule[rule]),
                                                        per_rule_text[rule][:150]))
print("\nWarnings per file:")
by_file = collections.defaultdict(collections.Counter)
for rule, counter in per_rule.items():
    for rel, count in counter.items():
        by_file[rel][rule] = count
for rel in files:
    print("  %s: %s" % (rel, ", ".join("%s x%d" % item for item in sorted(by_file[rel].items())) or "no warnings"))

# -- 5. OQ-11 ----------------------------------------------------------------------------------------------------------
print("\n== 5. OQ-11: opaque TAGGs (Raw = unknown name, Mask = #Hidden# / #Lock# / #Selected#) after the Lossless round trip ==")
for key, count in sorted(survival.items()):
    print("  %-5s %-34s %d" % (key[0], key[1], count))
moved = sum(count for name, count in class_count.items() if "MaskTagg" in name)
print("LODs whose editor masks kept their bytes but now sit on other points (interleaved proxies): %d" % moved)

# -- 6. timings --------------------------------------------------------------------------------------------------------
print("\n== 6. Time per file (seconds) ==")
slow = [item for item in timings if max(item[1], item[2]) > 10]
for rel, imp, exp in timings:
    print("  import %6.2f  export %6.2f  %s" % (imp, exp, rel))
print("files with an import or export over 10 s: %d" % len(slow))
print("total import %.1f s, total export %.1f s" % (sum(t[1] for t in timings), sum(t[2] for t in timings)))
rig.close()

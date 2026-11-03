# Arma 3 Samples: real-file testing (task #29, D6, OQ-5, OQ-11)

What was run: the 34 MLOD `.p3d` files of Bohemia's "Arma 3 Samples" (Steam, `Addons/`, 13 folders, 261 LODs) through the
real nodes in hython 22.0.466. Raw output: `samples_survey_out.txt` (made by `samples_survey.py`). Tests:
`tests/test_samples_p3d.py` (CPython), `tests/test_samples_hou.py` (hython), `tests/test_real_file_layout.py` (synthetic
reproductions, no Samples bytes). Shared lookup: `tests/samples_support.py` (`A3OB_SAMPLES`, default the Steam path; skips
with a reason when absent; `A3OB_SAMPLES_FULL=1` for all 34 files in the Houdini tests).

## 1. Licence

The Samples are not ours. Read before any use: `Arma 3 Samples/readme.txt`, and `Content_Licensing/Mod_Presentation/Docs/
License.txt` (the only licence text in the folder; the other licence is a link, `Bohemia Interactive's Licenses @
bistudio.com.url`).

`readme.txt`: "(c) 2013-2017 Bohemia Interactive a.s. All rights reserved." and "Addons: Samples of Addons that be packed
with Addon Builder and tested ingame".

`License.txt` is the Arma Public License (APL), dated 2/12/2013. Terms that apply to this work:

- Summary (stated to have "no legal effect"): "Attribution", "Noncommercial - You may not use this material for any
  commercial purposes." and "Arma Only - You may not convert or adapt this material to be used in other games than Arma."
- 1.c: "ArmaOnly means primarily intended for or directed towards the use in any of existing and future Arma games".
- 2.a.1: the licence is to "reproduce and Share the Licensed Material, in whole or in part, for NonCommercial and ArmaOnly
  purposes only" and to "produce, reproduce, and Share Adapted Material for NonCommercial and ArmaOnly purposes only".
- 2.a.4: "technical modifications necessary to do so" are allowed, and "simply making modifications authorized by this
  Section 2(a)(4) never produces Adapted Material."
- 3.a.1: if you Share the material (also modified) you must keep the credit and copyright notices, "indicate if You modified
  the Licensed Material", and say that it is under this licence.
- 9.b: the Arma and Bohemia Interactive names and logos may not be used to imply endorsement.

How the work stays inside it: the files stay on the owner's disk and are read in place. Nothing from a Samples file (bytes,
names of selections, textures or proxies) is committed: the evidence holds counts, our own messages with quoted names
replaced by `<name>`, LOD names (type and resolution, not content) and paths under the Samples root. The converted copies
exist only as temporary files that the tests delete. The converted data is Houdini geometry made to be written back as `.p3d`
for Arma (ArmaOnly), by a non-commercial open-source tool. The test code asks the owner's local copy and skips without it. This
is the reading of the maintainers, not legal advice; the owner should confirm it.

## 2. Result in one table

| Check | Result |
|---|---|
| M1 strict read and write | 34 of 34 files byte-identical (`test_samples_p3d`). |
| Houdini Import then Lossless export | 4 of 34 byte-identical (the four smallest templates). 30 differ in every LOD that has one of the causes in section 3; none differs in anything else (0 unexplained LODs of 261). |
| Import, Extract LOD k (all k), Assemble LODs, Lossless | 34 of 34 give the same bytes as Import then Lossless export. Extract and Assemble add no change. |
| Arma 3 release export | 34 of 34 export with no error. Warnings per rule in section 4. |
| Time | largest file (Male.p3d): import 1.8 s, export 2.4 s. No file over 10 s. Total for 34: import 6.4 s, export 9.9 s. |
| Non-ASCII strings | none in 384 104 strings (OQ-5). |

Criterion 3 asks for sha256-identical bytes. That holds for 4 files only. The other 30 differ for the reasons below, which are
properties of what Bohemia's Object Builder writes and what Houdini geometry cannot hold. Fixing them needs a new place in the
data model (see "Proposal"), which the task does not allow ("never invent attributes"), so they are written up here.

## 3. Findings: what comes back different, and why

First differing byte of the 30 files: 27 at byte 28 (the normal count in the first LOD header), the rest later in the file at
the first LOD that has a cause (`samples_survey_out.txt` gives LOD and record per file). LODs with a difference by cause (a LOD
can have several):

| Id | Cause | LODs | What the content does |
|---|---|---|---|
| F-1 | Normal table layout | 159 of 261 | Every corner keeps its normal vector. The file has entries that repeat a vector (Object Builder writes one per point and smoothing group, with no simple rule: first-appearance by value matches 11 of 189 LODs with faces, by point and value 93); the exporter writes one entry per distinct vector, so the count in the LOD header and the indices change. |
| F-2 | Unused slot of triangles | 34 | The fourth vertex, normal and UV slot of a triangle holds old values in Object Builder's files (for example a vertex index). The exporter writes zero. Not visible in any tool. |
| F-3 | Sharp edge pair order | 46 of 187 with sharp edges | Object Builder keeps the pairs sorted, including pairs (a, a). The importer puts those in `a3ob_sharp_extra`; the exporter writes the edge group first and the extras last. Same set of pairs. |
| F-4 | Interleaved proxies | 36 LODs warned at import; 17 points and faces rewritten in another order, 19 more with stale indices | The spec says L1 (13.6, "proxies are not the last points and faces"). New finding on top of it: what the file keeps as indices does not follow the moved points. 4 LODs with an editor mask and 2 with a mask and sharp pairs have the mask bytes unchanged but on other points now; 15 have sharp pairs that differ after the reorder (most likely the pairs that touch a proxy point, kept as file indices in `a3ob_sharp_extra`; not traced pair by pair). See OQ-11. |
| F-5 | Signed zero | 1 | A point with z = +0.0 comes back as -0.0: the unpack of a packed LOD turns the -0.0 the import made into +0.0, and the export negates z again. Equal value, other bits. (`test_zero_z_comes_back_as_negative_zero`.) |

**F-4 is fixed (task #36).** Masks, `#Hide#` and sharp pairs are remapped through the import permutation, so in the
Samples the interleaved LODs differ only in point and face order (the 6 mask LODs and the 15 sharp pair LODs are
content-correct). The pin in `tests/test_real_file_layout.py` now checks this.

Each of F-1 to F-5 has a minimal synthetic reproduction in `tests/test_real_file_layout.py` (no Samples bytes) that pins
today's behaviour, so a later fix flips it on purpose. The Samples test fails on any LOD that differs in something else.

**Proposal (for the owner, not done):** lossless output of real Object Builder files needs the file layout kept next to the
Houdini data, for F-1, F-2, F-3 and F-4 alike: for example a per-LOD detail blob (base64, with the point, face and corner
counts it is valid for, like the other blob TAGGs) that holds the normal table, the unused slots, the sharp pair list and the
point and face permutation of interleaved proxies. Without it the export is content-lossless but not byte-lossless on these
files. That is a data model change (spec 14, new attribute), so it is a finding for the owner, filed as an Aesir finding
(rule `samples-l0`).

Things that did not differ: points, faces, flags, UVs (including NaN and denormal UV values in one shadow volume, which come
back bit-identical), textures, materials, selections, properties, mass, UV sets, `#Selected#` and `#Hide#` blobs (bytes), LOD
signatures and order.

## 4. Validator rules that fire on Bohemia's own files (Arma 3 release export)

These are all warnings; none stopped an export. The owner reviews the verdicts. "Warnings" is the number of messages, "files"
the number of files of 34. The messages are in `samples_survey_out.txt` (names removed).

| Rule | Warnings | Files | Verdict | Reason |
|---|---|---|---|---|
| T2-TRI (not triangulated) | 26 | 20 | likely false positive | 3 760 quads in Geometry-family and Shadow Volume LODs of Bohemia's own files. The rule is A3OB's; Object Builder and Binarize accept convex quads there. Keep for non-convex or 5+ sided polygons, or as an info. |
| T2-MASS (no mass) | 6 | 6 | likely false positive | Items and templates (headgear, vest, scope) ship without mass. The rule is right for physical objects only. |
| T2-MASSALL (a point without mass) | 3 | 3 | needs owner review | Test_Car_01, a plane part and the scope have points with no mass. Could be a real gap in a sample, or points outside the components. |
| T2-SHADOWOFF (LODNoShadow = 1) | 7 | 5 | likely false positive | Bohemia sets this on purpose to switch a shadow volume off; it is a valid use, not an error. |
| T2-SHADOWRES (unconventional resolution) | 3 | 3 | likely false positive | Weapon and lamp shadow volumes use resolution 1; A3OB's usual list (0, 10, 1000, 1010, 1020) is not complete. |
| T2-NOMAT (shadow volume with texture) | 2 | 1 | likely false positive | The plant's foliage shadow volumes carry texture and material (alpha shadows). |
| T2-CONTIG (flipped polygons) | 1 | 1 | needs owner review | One foliage shadow volume. Double-sided foliage may flip on purpose. |
| T2-CONVEX (not convex) | 1 | 1 | needs owner review | A plane wreck PhysX geometry. The rule tests the whole mesh, so separate hulls that touch could trigger it. |
| T2-RW-LIMIT (255 roadway points) | 1 | 1 | needs owner review | The house roadway in a sample is over the limit. Might apply to animated roadways only. |
| Unknown face flags (tier 1, `flag_check`) | 14 | 2 | false positive (our table) | The male and the plant use face flag values 0x1000 (3 321 faces) and 0x2000 (1 291 faces), which A3OB's face table lacks (they are in its vertex fog field). They are Object Builder's own files, so the table is incomplete. Proposal: add 0x1000 and 0x2000 to the known face flags. |
| Lowercase makes two selections equal | 1 | 1 | real, harmless | The tank memory LOD has `X` and `x` selections that differ only in case. Both are written; Object Builder lowercases anyway. |

No rule fired for T2-UV2, T2-COMP (auto-components is on in the release preset), T2-SHARP, the point-cloud and ground-layer rules.

## 5. The spec's "Still open" questions

**OQ-11, blob TAGG coverage.** The Samples contain 74 `#Selected#` editor masks and 6 raw `#Hide#` TAGGs (Object Builder's own
state, an unknown TAGG), none inactive, no `#Animation#`, `#Lock#` or `#Hidden#`. After import and Lossless export all 80 come
back with the same bytes. The catch is F-4: in 6 LODs with interleaved proxies the `#Selected#` bytes are unchanged but the points
moved, so the mask marks other points. They are bound to the counts, not to the point order. Recommend: close OQ-11 as "blobs
survive when the point and face order is kept", and carry F-4 forward (an interleaved LOD should drop a count-bound blob with a
warning, or the data model should keep the permutation). The owner decides.

**OQ-5, code page.** 384 104 strings (selection and TAGG names, textures, materials, property keys and values) in all 34 files:
none is non-ASCII. The Samples cannot settle the code page. Recommend: keep cp1252 and close OQ-5 unless the owner has a file with
non-ASCII names. The owner decides.

## 6. Gate

The default Houdini run (one smallest file per Addons folder, 13 files) takes about 8 s on top of the quick gate; the CPython
test (all 34 files, strict read and write) about 0.5 s. All 34 files in the Houdini tests: `A3OB_SAMPLES_FULL=1`, about 71 s. Proposed
profile text for `aesir.toml` (outside this task's scope), a slower profile that runs the full set:

    [gate.samples]
    env = { A3OB_SAMPLES_FULL = "1" }
    commands = ["python scripts/run_tests.py test_samples_p3d", "\"E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe\" scripts/run_tests.py test_samples_hou test_real_file_layout"]

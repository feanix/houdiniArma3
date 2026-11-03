# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: evidence for the revision 5 spec (D17 export options), houdini-data-model.md section 18.
# Run from the repository root:  python docs/design/evidence/d17/d17_survey.py > docs/design/evidence/d17/d17_survey_out.txt
"""Surveys behind section 18: the A3OB translation table, and what the three options would do to the two fixtures.

Pure Python. Reads ``.reference/Arma3ObjectBuilder`` (A3OB v2.5.1 @ 9f2b8bc) and ``tests/fixtures/p3d``.
"""

import ast
import collections
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "python"))

from a3ob_core import p3d  # noqa: E402

# The A3OB checkout is not part of this repository (it is ignored); A3OB_REFERENCE may point at it.
A3OB = Path(os.environ.get("A3OB_REFERENCE", str(ROOT / ".reference" / "Arma3ObjectBuilder"))) / "Arma3ObjectBuilder"
FIXTURES = ROOT / "tests" / "fixtures" / "p3d"

# A3OB utilities/data.py lines 150: lod_allow_uvs = visuals + shadows + groundlayer.
ALLOW_UVS = {0, 1, 2, 3, 18, -1, 4, 26, 27, 28, 31}


def table_survey() -> None:
    tree = ast.parse((A3OB / "utilities" / "data.py").read_text(encoding="utf8"))
    table = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "translations_czech_english":
            table = ast.literal_eval(node.value)
    print("translation table entries:", len(table))
    inverse = collections.defaultdict(list)
    for czech, english in table.items():
        inverse[english].append(czech)
    print("distinct English names:", len(inverse))
    for english, czech in inverse.items():
        if len(czech) > 1:
            print("  many-to-one:", english, "<-", czech, "=> the inverse dict keeps", czech[-1])
    print("English names that are also Czech keys:", [e for e in inverse if e in table])
    print("keys or values with upper case:", [k for k in table if k != k.lower()] + [v for v in table.values() if v != v.lower()])
    print("keys with a space:", sum(1 for k in table if " " in k))
    english = {e: c[-1] for e, c in inverse.items()}
    for name in ("sample_1_character", "sample_2_crate"):
        model = p3d.read_p3d_file(str(FIXTURES / (name + ".p3d")))
        hits = sorted({t.name for lod in model.lods for t in lod.taggs
                       if p3d.is_selection_name(t.name) and t.name.lower() in english})
        print("%s: selection names that are English table names (export would rename): %s" % (name, hits))


def fixture_survey() -> None:
    for name in ("sample_1_character", "sample_2_crate"):
        model = p3d.read_p3d_file(str(FIXTURES / (name + ".p3d")))
        for index, lod in enumerate(model.lods):
            kind, resolution = p3d.decode_signature(lod.resolution_signature)
            components = [t.name for t in lod.taggs if re.match(r"component\d+", t.name, re.IGNORECASE)]
            dense = components == ["component%02d" % n for n in range(1, len(components) + 1)]
            uv_sets = [t.set_id for t in lod.taggs if isinstance(t, p3d.UVSetTagg)]
            nonzero = sum(1 for v in lod.face_uvs if v != 0.0)
            drops = int(kind) not in ALLOW_UVS and (bool(uv_sets) or nonzero > 0)
            print("%s LOD %d %s %s: components %d (identity renumber: %s), UVSet ids %s, non-zero face UV values %d, "
                  "drop UVs changes it: %s" % (name, index, kind.name, resolution, len(components),
                                               dense if components else "n/a", uv_sets, nonzero, drops))


if __name__ == "__main__":
    table_survey()
    fixture_survey()

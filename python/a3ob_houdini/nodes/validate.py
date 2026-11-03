# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB ui/tool_validation.py and utilities/validator.py, v2.5.1 (9f2b8bc): the validator is the export
# core's collection and build checks, plus A3OB's per-LOD-type rules as switchable warnings (spec 17.1.1). A3OB: MrClock
# (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""The Validate node (ux-tools.md section 5.8): runs every check of spec 17.1 and shows the result as node messages.

It changes nothing: the output is the input. It runs the same code as P3D Export (``_export_common.run_checks``), so a
model that passes one passes the other.
"""

from ..messages import Report
from ..params import NodeSpec, read_parms
from . import _export_common as common

__all__ = ["SPEC", "cook", "BUTTONS"]

NAME = "Validate"

SPEC = NodeSpec(
    kind="validate",
    label="Validate",
    summary="Checks a P3D geometry or a LOD geometry the way the export does and shows what is wrong. Changes nothing.",
    inputs=(1, 1),
    input_labels=("P3D geometry or LOD geometry",),
    input_help=("The model to check: a whole P3D from Assemble LODs, or a single LOD.",),
    parms=common.level_parms() + common.rule_parms(),
)


def cook(node, report: Report) -> None:
    """The output is the input (a Python-type SOP starts with a copy of it). Only the messages are new."""
    values = read_parms(node, SPEC)
    checked = common.run_checks(node, values, report, NAME)
    report.info("%d LODs checked: %d errors, %d warnings." % (checked.result.lod_count, checked.errors, checked.warnings))


BUTTONS = {}

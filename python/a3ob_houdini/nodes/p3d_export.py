# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/export_p3d.py, v2.5.1 (9f2b8bc): the export dialog options become node parameters and the
# export runs on the node's input geometry (ux-tools.md 5.9). A3OB: MrClock (present add-on), Hans-Joerg "Alwarren"
# Frieden (original ArmaToolbox).
"""The P3D Export node (ux-tools.md section 5.9): checks its input like Validate and writes the ``.p3d`` on Save to Disk.

Cooking never writes a file. The output is the input, so the display flag can stay on this node.

Save to Disk runs the checks again on the input as it is now, stops on errors (D14 Q-13), builds the bytes, writes them
to a temporary file next to the target and renames it (D13). Houdini ignores errors that a button callback raises, so
``save_button`` catches everything itself. It keeps the result and cooks the node again; the cook then shows it as node
messages (a node can only add messages while it cooks).

Not offered yet (ux-tools.md 5.9, "deferred" in the task report): Renumber components, Translate names to Czech and
Drop UVs, which the export core does not implement, and the Preset menu, which task #17 adds. The option parameters
are all read through ``options_from`` from this node's own values, so a preset only has to set parameters.
"""

import logging
import traceback
from typing import Dict

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from .. import p3d_export
from ..errors import UserError
from ..messages import ERROR, INFO, Report, format_problem
from ..params import NodeSpec, Parm, read_parms
from . import _export_common as common

__all__ = ["SPEC", "cook", "BUTTONS", "OPTION_PARMS", "run_save", "save", "save_button", "DEFAULT_PROJECT_ROOT"]

NAME = "P3D Export"
LOGGER = logging.getLogger("a3ob")

#: A3OB's default project root. Task #17 puts the shelf setting in front of it (ux-tools.md 7.2).
DEFAULT_PROJECT_ROOT = "P:\\"

#: The parameters that make the exporter options. A preset (task #17) fills these.
OPTION_PARMS = ("selmask", "lowercase", "relpaths", "projectroot", "sortsections", "sortlods", "autocomponents",
                "dupes", "backup")

SPEC = NodeSpec(
    kind="p3d_export",
    label="P3D Export",
    summary="Checks a P3D geometry or a LOD geometry like Validate and writes it as a .p3d file when Save to Disk is "
            "pressed. The output is the input.",
    inputs=(1, 1),
    input_labels=("P3D geometry or LOD geometry",),
    parms=(
        Parm("file", "Output File", "file", "$HIP/$HIPNAME.p3d", "The .p3d file to write.", file_pattern="*.p3d",
             file_mode="write"),
        Parm("save", "Save to Disk", "button", "",
             "Check the input again and write the file. Nothing is written when there are errors.", action="save"),
        Parm("check", "Check", "menu", "cook",
             "Every cook: check whenever the node cooks. When saving: check only on Save to Disk (for large models).",
             items=(("cook", "Every cook"), ("save", "When saving"))),
    ) + common.level_parms() + (
        Parm("selmask", "Selection Mask", "string", "* ^__*",
             "Which point groups become selections: names separated by spaces, * and ? match, ^ before a name leaves "
             "it out. Groups named in a3ob_tagg_order are always selections."),
        Parm("lowercase", "Lowercase", "toggle", False,
             "Write texture and material paths, selection names (so proxy paths too) and property names and values in "
             "lower case, as Object Builder does. Warns when two names become equal."),
        Parm("relpaths", "Relative Paths", "toggle", False,
             "Strip the project root (or at least the drive letter) from texture, material and proxy paths."),
        Parm("projectroot", "Project Root", "string", "",
             "Used with Relative Paths. Empty means the root from the shelf settings, or P:\\ without one."),
        Parm("sortsections", "Sort Sections", "toggle", False,
             "Order the faces by texture and material, as Object Builder does. This changes the face order."),
        Parm("sortlods", "Sort LODs", "toggle", False, "Write the LODs sorted by resolution. Off keeps your order."),
        Parm("autocomponents", "Auto-Components", "toggle", False,
             "Geometry LODs without a component selection get one per connected piece."),
        Parm("backup", "Keep a .bak", "toggle", False, "When a file is overwritten, keep the old one as .bak."),
    ) + common.rule_parms(),
)

#: The result of the last Save to Disk of each node, shown by the next cook: node path -> raw messages.
_PENDING: Dict[str, Report] = {}


def _values(node) -> Dict[str, object]:
    values = read_parms(node, SPEC)
    if not str(values["projectroot"]).strip():
        values["projectroot"] = DEFAULT_PROJECT_ROOT
    return values


def cook(node, report: Report) -> None:
    """Pass the input through and show the checks. Never writes a file."""
    values = _values(node)
    pending = _PENDING.pop(node.path(), None)
    if pending is not None:  # a Save to Disk has just run its own, fresh checks
        common.input_geometry(node, NAME)
        common.show(report, pending.items, values)
        return
    if values["check"] == "save":
        common.input_geometry(node, NAME)
        report.max_messages = int(values["maxmessages"])
        report.info("Not checked while cooking (Check is When saving). Press Save to Disk.")
        return
    checked = common.run_checks(node, values, report, NAME)
    if checked.result.data is not None:
        report.info("%d LODs, %s if saved now." % (checked.result.lod_count, _size(len(checked.result.data))))
    else:
        report.info("%d LODs checked: %d errors, %d warnings." % (
            checked.result.lod_count, checked.errors, checked.warnings))


def _size(count: int) -> str:
    return "%d KB" % max(1, (count + 1023) // 1024)


def run_save(node) -> Report:
    """Check the input, and if nothing blocks, write the file. Returns every message (raw severities). Never raises:
    a problem is an error in the report, and then nothing was written."""
    report = Report()
    try:
        values = _values(node)
        geo = node.inputGeometry(0)
        if geo is None:
            report.error(format_problem(NAME, "there is no input", "Connect a P3D geometry or a LOD geometry"))
            return report
        result = p3d_export.run([geo], common.options_from(values), report)
        if common.is_blocked(report.items, bool(values["strict"])):
            return report
        if result.data is None:
            report.error(format_problem(NAME, "nothing could be built", "Please report this"))
            return report
        path = str(values["file"]).strip()
        if p3d_export.write_file(path, result.data, report, bool(values["backup"])):
            report.info("Saved %d LODs, %s to %s." % (result.lod_count, _size(len(result.data)), path))
    except Exception as ex:  # a bug: a button callback must not raise (Houdini would only print it)
        LOGGER.error("Save to Disk failed:\n%s", traceback.format_exc())
        report.error("%s: Save to Disk failed with %s: %s. This is a bug in the A3OB tools. "
                     "Copy the console output into a bug report." % (node.path(), type(ex).__name__, ex))
    return report


def save(node) -> Report:
    """Save to Disk for scripts and tests: the same work as the button, without a dialog. Raises ``UserError`` when
    nothing was written (ux-tools.md 5.9.1)."""
    report = run_save(node)
    errors = report.texts(ERROR)
    if errors:
        raise UserError("\n".join(errors))
    return report


def save_button(kwargs) -> None:
    """The Save to Disk button. It catches its own errors and shows them on the node."""
    node = kwargs["node"]
    report = run_save(node)
    _PENDING[node.path()] = report
    try:
        node.cook(force=True)  # the cook shows the messages
    except Exception:  # hou.OperationFailed when the node has errors: that is the point
        pass
    _tell_user(report)


def _tell_user(report: Report) -> None:
    """In the GUI: a status line, and a dialog when something went wrong. Nothing happens in hython."""
    try:
        if hou is None or not hou.isUIAvailable():
            return
        errors = report.texts(ERROR)
        if errors:
            hou.ui.displayMessage("Nothing was written.\n\n" + "\n".join(errors[:10]), title="P3D Export",
                                  severity=hou.severityType.Error)
        else:
            saved = [text for kind, text in report.items if kind == INFO and text.startswith("Saved ")]
            if saved:
                hou.ui.setStatusMessage(saved[0])
    except Exception:
        LOGGER.error("Save to Disk could not show its result:\n%s", traceback.format_exc())


BUTTONS = {"save": save_button}

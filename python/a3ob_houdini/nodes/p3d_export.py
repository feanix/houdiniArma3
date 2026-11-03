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
Drop UVs, which the export core does not implement. The option parameters are all read through ``options_from`` from
this node's own values, so a preset only has to set parameters: the Preset menu and the Apply button do that
(``apply_preset``). The names of the option parameters do not change, so old scenes still load.
"""

import logging
import traceback
from typing import Dict

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from .. import p3d_export, settings as settings_module
from ..errors import UserError
from ..messages import ERROR, INFO, Report, format_problem
from ..p3d_export.type_rules import RULES
from ..params import NodeSpec, Parm, read_parms
from ..project_settings_dialog import display_path  # pure function; the module needs no hou at import
from . import _export_common as common

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS", "OPTION_PARMS", "run_save", "save", "save_button",
           "DEFAULT_PROJECT_ROOT", "apply_preset", "preset_values"]

NAME = "P3D Export"
LOGGER = logging.getLogger("a3ob")

#: A3OB's default project root. The shelf setting (ux-tools.md 7.2) comes before it: see ``_values``.
DEFAULT_PROJECT_ROOT = settings_module.DEFAULT_ROOT

#: The parameters that make the exporter options. A preset fills these (except the root and the backup switch).
OPTION_PARMS = ("selmask", "lowercase", "relpaths", "projectroot", "sortsections", "sortlods", "autocomponents",
                "renumber", "czech", "dropuvs", "dupes", "backup")

SPEC = NodeSpec(
    kind="p3d_export",
    label="P3D Export",
    summary="Checks a P3D geometry or a LOD geometry like Validate and writes it as a .p3d file when Save to Disk is "
            "pressed. The output is the input.",
    inputs=(1, 1),
    input_labels=("P3D geometry or LOD geometry",),
    input_help=("The model to check and write: a whole P3D from Assemble LODs, or a single LOD.",),
    parms=(
        Parm("file", "Output File", "file", "$HIP/$HIPNAME.p3d", "The .p3d file to write.", file_pattern="*.p3d",
             file_mode="write"),
        Parm("save", "Save to Disk", "button", "",
             "Check the input again and write the file. Nothing is written when there are errors.", action="save"),
        Parm("check", "Check", "menu", "cook",
             "Every cook: check whenever the node cooks. When saving: check only on Save to Disk (for large models).",
             items=(("cook", "Every cook"), ("save", "When saving"))),
    ) + common.level_parms() + (
        Parm("preset", "Preset", "menu", settings_module.LOSSLESS,
             "A saved set of the options below: Lossless, Arma 3 release, or one you saved with the Project root and "
             "presets shelf tool. Pick one and press Apply. Choosing does not change anything by itself.",
             menu_action="presets"),
        Parm("apply", "Apply", "button", "",
             "Fill the options below from the chosen preset. Project Root and Keep a .bak stay as they are.",
             action="apply"),
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
        Parm("renumber", "Renumber Components", "toggle", False,
             "Rename every selection that starts with component and digits to component01, component02, ... in the "
             "order they are written, per LOD. The name is lowercased and any text after the digits is lost."),
        Parm("czech", "Translate Names to Czech", "toggle", False,
             "Rename selections that have an English name from Object Builder's table (body becomes telo, weapon becomes "
             "zbran) to the Czech name the game uses. Whole name, any case. Warns when two selections end up equal."),
        Parm("dropuvs", "Drop UVs", "toggle", False,
             "LODs of a type that does not use UVs (Geometry, Memory, Roadway, the fire and view geometry types) are "
             "written without UV sets and with zero face UVs, as Object Builder does. The geometry on the node is not "
             "changed."),
        Parm("backup", "Keep a .bak", "toggle", False, "When a file is overwritten, keep the old one as .bak."),
    ) + common.rule_parms(),
)

#: The result of the last Save to Disk of each node, shown by the next cook: node path -> raw messages.
_PENDING: Dict[str, Report] = {}


def _values(node) -> Dict[str, object]:
    values = read_parms(node, SPEC)
    if not str(values["projectroot"]).strip():
        # An empty root means the shelf setting (or A3OB_PROJECT_ROOT); without either, P:\ (ux-tools.md 5.9).
        settings, _ = settings_module.load()
        values["projectroot"] = settings_module.effective_root(settings) or DEFAULT_PROJECT_ROOT
    return values


def preset_values(settings, name: str) -> Dict[str, object]:
    """The parameter values a preset sets, by parameter name. A preset that leaves an option out means its default,
    as in ``settings.options_for``. Raises ``UserError`` for an unknown preset."""
    options = settings_module.options_for(settings, name)
    values: Dict[str, object] = {
        "selmask": options.selection_mask, "lowercase": options.lowercase, "relpaths": options.relative_paths,
        "sortsections": options.sort_sections, "sortlods": options.sort_lods,
        "autocomponents": options.auto_components, "renumber": options.renumber_components,
        "czech": options.czech_names, "dropuvs": options.drop_uvs, "dupes": options.duplicate_lods,
        "typerules": True,
    }
    for rule in RULES:
        values[common.rule_parm_name(rule.id)] = rule.id not in options.rules_off
    return values


def apply_preset(node, name: str = "", settings=None) -> None:
    """Set the option parameters of ``node`` from a preset (default: the node's Preset menu). Parameter names stay as
    they are. Raises ``UserError`` for an unknown preset."""
    if settings is None:
        settings, _ = settings_module.load()
    name = name or str(node.parm("preset").eval())
    for parm_name, value in preset_values(settings, name).items():
        parm = node.parm(parm_name)
        if parm is not None:
            parm.set(value)


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
        path = p3d_export.with_p3d_extension(str(values["file"]).strip())  # the message names the real file
        if p3d_export.write_file(path, result.data, report, bool(values["backup"])):
            report.info("Saved %d LODs, %s to %s." % (result.lod_count, _size(len(result.data)),
                                                                  display_path(path)))
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


def apply_button(kwargs) -> None:
    """The Apply button. A bad preset name is shown to the artist; nothing is raised."""
    node = kwargs["node"]
    try:
        apply_preset(node)
    except UserError as ex:
        if hou is not None and hou.isUIAvailable():
            hou.ui.displayMessage(str(ex), title=NAME, severity=hou.severityType.Error)
        else:
            LOGGER.error("%s", ex)


def _menu_presets(kwargs):
    """The Preset menu: the built-in presets, then the artist's own (read when the menu opens)."""
    settings, _ = settings_module.load()
    return [(name, name) for name in settings_module.preset_names(settings)]


BUTTONS = {"save": save_button, "apply": apply_button}
MENUS = {"presets": _menu_presets}

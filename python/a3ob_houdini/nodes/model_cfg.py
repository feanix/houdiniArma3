# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Model Cfg node (task #95): writes or updates the ``model.cfg`` that a character P3D needs.

Cooking never writes a file. It reads the model.cfg that is there (if any), works out what the Write button would do,
and shows that as node messages: the changes, the warnings and the whole text. **Write model.cfg** writes it (a
``.bak`` of an existing file first, through a temporary file). The node passes its input through.

The text work is in ``model_cfg_writer`` (no ``hou``); the file is read with ``a3ob_core.rig.model_cfg``.
"""

import logging
import os
import traceback
from typing import Dict, List, Optional

try:  # the parameter table is also read under plain Python (HDA generation, tests)
    import hou
except ImportError:
    hou = None

from a3ob_core.rig.model_cfg import CfgError, read_cfg_file

from .. import model_cfg_writer as writer
from ..errors import UserError
from ..messages import ERROR, INFO, Report, format_problem
from ..p3d_export.write import write_bytes
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS", "plan_for", "run_write", "write", "find_export", "selection_names"]

NAME = "Model Cfg"
LOGGER = logging.getLogger("a3ob")
FILE_NAME = "model.cfg"

SPEC = NodeSpec(
    kind="model_cfg",
    label="Model Cfg",
    summary="Writes the model.cfg a character P3D needs (uniform, vest, headgear and other gear on the man skeleton) "
            "into a folder, or adds the P3D's class to the model.cfg that is already there. The messages show what "
            "Write model.cfg would do. The output is the input.",
    inputs=(0, 1),
    input_labels=("P3D geometry or LOD geometry (optional)",),
    input_help=("Optional. Its selections are scanned for section names (Add Selections of the Input).",),
    parms=(
        Parm("folder", "Folder", "dir", "",
             "The folder to write model.cfg into. Normally the folder of the P3D, or a folder above it. The file is "
             "named model.cfg."),
        Parm("model", "Model Names", "string", "",
             "The P3D file names without .p3d, separated by spaces or commas. Each gets one class in CfgModels, named "
             "exactly like the file. Capitals matter to the build tools."),
        Parm("fromexport", "From P3D Export", "button", "",
             "Take Folder and Model Names from the Output File of the P3D Export node in this network (the one that "
             "shares this node's input, or the only one).", action="fromexport"),
        Parm("base", "Base Class", "string", "ArmaMan",
             "The CfgModels class that holds the shared settings and sections of man models. Each model class "
             "inherits from it. It is added when the file has none."),
        Parm("skeleton", "Skeleton", "string", "OFP2_ManSkeleton",
             "The CfgSkeletons class with the Arma 3 man skeleton (103 bones and their parents). It is added when the "
             "file has none; a class of that name that is there is kept as it is."),
        Parm("extra", "Extra Sections", "string", "",
             "More section names (selections that a config can retexture or hide, for example camo3), separated by "
             "spaces or commas. They are added to the sections of the base class, on top of the usual man list "
             "(camo, camo1, camo2, personality, hl, injury_*, insignia ...)."),
        Parm("scan", "Add Selections of the Input", "toggle", False,
             "Also add the selections of the input geometry (visual LODs) that are not bones, proxies or a3ob_ helper "
             "groups, for example camo1. Off: they are only listed in the messages. Does nothing without an input. "
             "Check the list first: working groups of your own show up here too."),
        Parm("write", "Write model.cfg", "button", "",
             "Create model.cfg, or update the one in the folder: only the model classes are added or replaced (and "
             "Default, the base class and the skeleton if missing). Everything else in the file stays. An old file is "
             "kept as model.cfg.bak. A binarized file or a file that cannot be read is refused.", action="write"),
    ),
)

#: The result of the last Write of each node, shown by the next cook: node path -> messages.
_PENDING: Dict[str, Report] = {}


def selection_names(geo) -> Optional[List[str]]:
    """The selection names of the input that can be sections' sources: the point groups of each visual LOD of a P3D
    geometry, of a visual LOD geometry, or of any other geometry. None without a geometry. Other LOD types (memory,
    geometry, shadow, ...) hold no sections."""
    if geo is None:
        return None
    from .. import lod_container, selection_browser
    kind = lod_container.classify(geo)
    if kind == lod_container.P3D:
        sources = [lod_container.lod_content(prim) for prim in geo.prims()]
    else:
        sources = [geo]
    found: List[str] = []
    for source in sources:
        lod_type = lod_container.detail_int(source, lod_container.TYPE_ATTRIB)
        if lod_type is not None and lod_type != 0:  # LodType.VISUAL is 0
            continue
        found += [entry.name for entry in selection_browser.list_groups(source) if entry.name]
    return found


def _input_geometry(node):
    inputs = node.inputs()
    return node.inputGeometry(0) if inputs and inputs[0] is not None else None


def _read_existing(path: str) -> Optional[str]:
    """The text of the model.cfg at ``path``, None when there is none. A binarized file or a file the parser cannot
    read raises ``UserError`` (the line is named)."""
    if not os.path.isfile(path):
        return None
    try:
        read_cfg_file(path)  # refuses a binarized file; names the line of a syntax problem
    except CfgError as ex:
        raise UserError(format_problem(NAME, "%s cannot be updated: %s" % (path, ex),
                                       "Fix the file by hand, or move it away so a new one is written")) from None
    with open(path, "rb") as file:
        return file.read().decode("utf-8", errors="surrogateescape")


def plan_for(node, report: Report):
    """Work out what Write would do: ``(path, Plan)``. Messages (warnings, infos) go to ``report``. Raises ``UserError``
    for a problem the artist can fix."""
    values = read_parms(node, SPEC)
    folder = os.path.expandvars(str(values["folder"]).strip())
    if not folder:
        raise UserError(format_problem(NAME, "no folder is set", "Set Folder, or press From P3D Export"))
    path = os.path.join(os.path.abspath(folder), FILE_NAME)
    try:
        models = writer.parse_model_names(str(values["model"]))
        if not models:
            raise ValueError("no model name is set. Type the P3D file names, or press From P3D Export")
        extras = writer.parse_sections(str(values["extra"]))
        selections = selection_names(_input_geometry(node))
        found = writer.section_candidates(selections or []) if values["scan"] and selections is not None else []
        existing = _read_existing(path)
        result = writer.plan(existing, models, str(values["base"]), str(values["skeleton"]),
                             writer.merge_sections(extras, found))
    except ValueError as ex:  # also CfgError of a bad name
        raise UserError(format_problem(NAME, str(ex))) from None
    except CfgError as ex:
        raise UserError(format_problem(NAME, "%s cannot be updated: %s" % (path, ex))) from None
    for text in writer.case_warnings(os.path.dirname(path), models):
        report.warning(format_problem(NAME, text))
    for text in result.warnings:
        report.warning(format_problem(NAME, text))
    if selections is not None:
        missing = writer.unknown_sections(extras, selections)
        if missing:
            report.info("%s %s not a selection of the input. That is fine when another P3D that uses this file has "
                        "%s." % (", ".join(missing), "is" if len(missing) == 1 else "are",
                                 "it" if len(missing) == 1 else "them"))
        candidates = writer.section_candidates(selections)
        if found:
            report.info("Selections of the input used as sections: %s." % ", ".join(found))
        else:
            listed = {name.lower() for name in writer.merge_sections(writer.MAN_SECTIONS, extras)}
            others = [name for name in candidates if name.lower() not in listed]
            if others:
                report.info("Selections of the input that could be sections: %s. Turn on Add Selections of the Input "
                            "or type them in Extra Sections." % ", ".join(others))
    for text in result.infos:
        report.info(text)
    if not os.path.isdir(folder):
        report.info("The folder %s does not exist yet. Write model.cfg creates it." % folder)
    return path, result


def cook(node, report: Report) -> None:
    """Pass the input through and show what Write model.cfg would do. Never writes a file."""
    pending = _PENDING.pop(node.path(), None)
    if pending is not None:  # a Write has just run
        report.items.extend(pending.items)
        return
    path, result = plan_for(node, report)
    if result.changed:
        report.info("Write model.cfg would %s: %s." % ("create" if result.created else "update", "; ".join(result.changes)))
    else:
        report.info("%s is up to date. Nothing would be written." % path)
    report.info("%s would read:\n%s" % (path, result.text))


def run_write(node) -> Report:
    """Write the file. Returns every message. Never raises: a problem is an error in the report, and then nothing was
    written."""
    report = Report()
    try:
        path, result = plan_for(node, report)
        if not result.changed:
            report.info("%s is up to date. Nothing was written." % path)
            return report
        existed = os.path.isfile(path)
        data = result.text.encode("utf-8", errors="surrogateescape")
        if write_bytes(path, data, report, backup=existed):
            report.info("Wrote %s: %s.%s" % (path, "; ".join(result.changes),
                                             " The old file is %s.bak." % path if existed else ""))
    except UserError as ex:
        report.error(str(ex))
    except Exception as ex:  # a bug: a button callback must not raise
        LOGGER.error("Write model.cfg failed:\n%s", traceback.format_exc())
        report.error("%s: Write model.cfg failed with %s: %s. This is a bug in the A3OB tools. Copy the details from "
                     "Windows > Log Viewer (source A3OB) into a bug report." % (node.path(), type(ex).__name__, ex))
    return report


def write(node) -> Report:
    """Write model.cfg for scripts and tests: the same work as the button. Raises ``UserError`` when nothing was written
    because of an error."""
    report = run_write(node)
    errors = report.texts(ERROR)
    if errors:
        raise UserError("\n".join(errors))
    return report


def _show(node, report: Report) -> None:
    try:
        if hou is None or not hou.isUIAvailable():
            return
        errors = report.texts(ERROR)
        if errors:
            hou.ui.displayMessage("Nothing was written.\n\n" + "\n".join(errors[:10]), title=NAME,
                                  severity=hou.severityType.Error)
        else:
            done = [text for kind, text in report.items if kind == INFO and text.startswith("Wrote ")]
            if done:
                hou.ui.setStatusMessage(done[0].split(":")[0])
    except Exception:
        LOGGER.error("Write model.cfg could not show its result:\n%s", traceback.format_exc())


def write_button(kwargs) -> None:
    node = kwargs["node"]
    report = run_write(node)
    _PENDING[node.path()] = report
    try:
        node.cook(force=True)  # the cook shows the messages
    except Exception:  # hou.OperationFailed when the node has errors: that is the point
        pass
    _show(node, report)


def find_export(node):
    """The P3D Export node this node belongs to: in the same network, the one that shares an input ancestor with this
    node, else the only one. Raises ``UserError`` when there is none or when it cannot be told."""
    found = [child for child in node.parent().children() if child.type().name().startswith("a3ob_p3d_export")]
    if not found:
        raise UserError(format_problem(NAME, "there is no P3D Export node in this network", "Type Folder and Model Names"))
    if len(found) > 1:
        mine = {item.path() for item in node.inputAncestors()}
        near = [item for item in found if mine & {other.path() for other in item.inputAncestors()}]
        if len(near) == 1:
            return near[0]
        raise UserError(format_problem(NAME, "there are %d P3D Export nodes in this network" % len(found),
                                       "Connect this node's input to the same node as the one you mean, or type "
                                       "Folder and Model Names"))
    return found[0]


def take_from_export(node) -> None:
    """Folder (as typed in the Output File, so ``$HIP`` stays) and Model Names from the P3D Export node."""
    export = find_export(node)
    typed = export.parm("file").unexpandedString().strip()
    shown = str(export.parm("file").eval()).strip()
    if not typed:
        raise UserError(format_problem(NAME, "%s has no Output File" % export.path()))
    folder = os.path.dirname(typed.replace("\\", "/"))
    name = os.path.basename(shown.replace("\\", "/"))
    if name.lower().endswith(".p3d"):
        name = name[:-4]
    node.parm("folder").set(folder)
    node.parm("model").set(name)


def from_export_button(kwargs) -> None:
    node = kwargs["node"]
    try:
        take_from_export(node)
    except UserError as ex:
        if hou is not None and hou.isUIAvailable():
            hou.ui.displayMessage(str(ex), title=NAME, severity=hou.severityType.Error)
        else:
            LOGGER.error("%s", ex)


BUTTONS = {"write": write_button, "fromexport": from_export_button}
MENUS: dict = {}

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Quick Export without a dialog (ux-tools.md section 7.1).

Exports the geometry of a SOP to a ``.p3d`` with a preset. It calls the export core (``p3d_export.export_geometry``)
directly and adds no node. The last output path is kept as user data on the exported node, not as an attribute.
``quick_export_dialog`` is the thin ``hou.ui`` layer on top. Needs ``hou`` (a SOP node).
"""

import os
from typing import List, Optional, Tuple

from . import messages, p3d_export, settings as settings_module
from .errors import UserError
from .messages import format_problem
from .project_settings_dialog import display_path  # pure function; the module needs no hou at import

__all__ = ["USER_DATA_KEY", "choose_node", "default_path", "remembered_path", "export_node", "summary_lines"]

USER_DATA_KEY = "a3ob_quick_export_path"


def choose_node(selected, network=None):
    """The SOP to export: the one selected SOP node, else the display node of ``network``. Raises ``UserError``."""
    import hou
    sops = [node for node in selected if node.type().category() == hou.sopNodeTypeCategory()]
    if len(sops) == 1 and len(selected) == 1:
        return sops[0]
    if len(selected) > 1:
        raise UserError(format_problem("Quick Export", "more than one node is selected", "Select one SOP node"))
    if network is not None and hasattr(network, "displayNode") and network.displayNode() is not None:
        return network.displayNode()
    raise UserError(format_problem("Quick Export", "no SOP node is selected and the current network has no "
                                   "display node", "Select a SOP node, or turn on a display flag"))


def remembered_path(node) -> str:
    """The path used last time for this node, or an empty string."""
    return node.userData(USER_DATA_KEY) or ""


def default_path(node) -> str:
    """Where a first export goes: next to the scene, named after the node. The user can change it."""
    import hou
    return os.path.join(hou.text.expandString("$HIP"), node.name() + ".p3d")


def export_node(node, path: str, preset: Optional[str] = None, settings=None) -> Tuple[bool, messages.Report]:
    """Export ``node``'s geometry to ``path`` with a preset (default: the active one). Never raises for data or disk
    problems: they are errors in the report. A bad preset name raises ``UserError``. On success the path is remembered."""
    if settings is None:
        settings, _ = settings_module.load()
    options = settings_module.options_for(settings, preset)
    report = messages.Report()
    geo = node.geometry()
    if geo is None:
        report.error(format_problem("Quick Export", "%s has no geometry" % node.path(), "Check the node for errors"))
        return False, report
    ok = p3d_export.export_geometry([geo], path, options, report)
    if ok:
        path = p3d_export.with_p3d_extension(path)  # the file written has the .p3d ending (task #26)
        node.setUserData(USER_DATA_KEY, path)
        report.info("Saved %s (%d bytes)." % (display_path(path), os.path.getsize(os.path.expandvars(path))))
    return ok, report


def summary_lines(report: messages.Report, max_messages: int = messages.MAX_MESSAGES) -> List[str]:
    """The report as text lines for a dialog: ``Error: ...``, ``Warning: ...``, ``...``."""
    prefix = {messages.ERROR: "Error: ", messages.WARNING: "Warning: ", messages.INFO: ""}
    return [prefix[kind] + text for kind, text in report.lines(max_messages)]

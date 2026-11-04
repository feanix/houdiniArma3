# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the Quick Export shelf tool (ux-tools.md section 7.1).

Only the thin layer of ``hou.ui`` calls. It needs the Houdini GUI, so it has no tests. The work is in ``quick_export``.
"""

import logging
import traceback

from . import quick_export as qe, settings as settings_module
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")
TITLE = "A3OB Quick Export"


def _network():
    import hou
    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    return pane.pwd() if pane is not None else None


def run(kwargs) -> None:
    """The shelf tool: pick the SOP, the preset and the file, export, and show the result."""
    import hou
    try:
        node = qe.choose_node(hou.selectedNodes(), _network())
        settings, problems = settings_module.load()
        names = settings_module.preset_names(settings)
        initial = names.index(settings.active_preset) if settings.active_preset in names else 0
        picked = hou.ui.selectFromList(names, default_choices=(initial,), exclusive=True, title=TITLE,
                                       message="Preset for %s" % node.path(), clear_on_cancel=True)
        if not picked:
            return
        path = qe.remembered_path(node)
        if not path:
            path = hou.ui.selectFile(start_directory=hou.text.expandString("$HIP"), title=TITLE, file_type=hou.fileType.Any,
                                     pattern="*.p3d", default_value=qe.default_path(node), chooser_mode=hou.fileChooserMode.Write)
            if not path:
                return
            path = hou.text.expandString(path)
        ok, report = qe.export_node(node, path, names[picked[0]], settings)
        text = "\n".join(problems + qe.summary_lines(report))
        if ok:
            hou.ui.setStatusMessage(text.splitlines()[-1] if text else "Saved")
            if any(kind != "info" for kind, _ in report.items):
                hou.ui.displayMessage(text, title=TITLE, severity=hou.severityType.Warning)
        else:
            hou.ui.displayMessage(text, title=TITLE, severity=hou.severityType.Error)
    except UserError as ex:
        hou.ui.displayMessage(str(ex), severity=hou.severityType.Error, title=TITLE)
    except Exception:
        LOGGER.error("Quick Export failed:\n%s", traceback.format_exc())
        hou.ui.displayMessage("Quick Export failed. This is a bug in the A3OB tools. See Windows > Log Viewer (source A3OB).",
                              severity=hou.severityType.Error, title=TITLE)

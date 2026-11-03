# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the Batch folder convert shelf tool (ux-tools.md section 7.4).

Only the thin layer of ``hou.ui`` calls. It needs the Houdini GUI, so it has no tests. The work is in
``batch_convert``.
"""

import logging
import traceback

from . import batch_convert, settings as settings_module
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")
TITLE = "A3OB Batch folder convert"


def _folder(message, start=""):
    import hou
    path = hou.ui.selectFile(start_directory=start or hou.text.expandString("$HIP"), title=message,
                             file_type=hou.fileType.Directory, chooser_mode=hou.fileChooserMode.Read)
    return hou.text.expandString(path).strip() if path else ""


def run(kwargs) -> None:
    """The shelf tool: ask for the folders and the preset, convert with a progress bar, show the summary."""
    import hou
    try:
        settings, problems = settings_module.load()
        if problems:
            hou.ui.displayMessage("\n".join(problems), title=TITLE, severity=hou.severityType.Warning)
        source = _folder("Input folder with .p3d files")
        if not source:
            return
        target = _folder("Output folder (the sources are never changed). Cancel for %s" % batch_convert.default_output(source))
        target = target or batch_convert.default_output(source)
        recursive = hou.ui.displayMessage("Include subfolders?", buttons=("No", "Yes"), title=TITLE) == 1
        names = settings_module.preset_names(settings)
        initial = names.index(settings.active_preset) if settings.active_preset in names else 0
        picked = hou.ui.selectFromList(names, default_choices=(initial,), exclusive=True, title=TITLE,
                                       message="Preset", clear_on_cancel=True)
        if not picked:
            return
        with hou.InterruptableOperation("Converting", open_interrupt_dialog=True) as operation:
            def progress(done, total, name):
                try:
                    operation.updateProgress(done / float(max(total, 1)))
                except hou.OperationInterrupted:
                    return False
                return True
            result = batch_convert.convert_folder(source, target, recursive, names[picked[0]], settings, progress)
        text = "%d files: %d identical, %d different, %d failed.%s\n\nReport: %s" % (
            len(result.files), result.count(batch_convert.IDENTICAL), result.count(batch_convert.DIFFERENT),
            result.count(batch_convert.FAILED), " Cancelled." if result.cancelled else "", result.report_path)
        severity = hou.severityType.Warning if result.count(batch_convert.FAILED) else hou.severityType.Message
        hou.ui.displayMessage(text, title=TITLE, severity=severity)
    except UserError as ex:
        hou.ui.displayMessage(str(ex), severity=hou.severityType.Error, title=TITLE)
    except Exception:
        LOGGER.error("Batch convert failed:\n%s", traceback.format_exc())
        hou.ui.displayMessage("Batch convert failed. This is a bug in the A3OB tools. See the console.",
                              severity=hou.severityType.Error, title=TITLE)

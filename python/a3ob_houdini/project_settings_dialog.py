# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the Project root and presets shelf tool (ux-tools.md section 7.2).

Only the thin layer of ``hou.ui`` calls. It needs the Houdini GUI, so it has no tests. The work is in ``settings``.
The tool changes nothing in a scene.
"""

import dataclasses
import logging
import traceback

from . import settings as sm
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")
TITLE = "A3OB Project root and presets"
_ACTIONS = ("Project root", "Active preset", "Edit preset", "Delete preset", "Common data file", "Close")


def _describe(settings) -> str:
    lines = ["Settings file: %s" % sm.settings_path(),
             "Project root: %s%s" % (sm.effective_root(settings),
                                     "  (from %s)" % sm.ENV_ROOT if sm.effective_root(settings) != settings.project_root else ""),
             "Active preset: %s" % settings.active_preset,
             "Common data file: %s" % (settings.common_data or "(none)")]
    return "\n".join(lines)


def run(kwargs) -> None:
    import hou
    try:
        settings, problems = sm.load()
        if problems:
            hou.ui.displayMessage("\n".join(problems), title=TITLE, severity=hou.severityType.Warning)
        while True:
            action = hou.ui.displayMessage(_describe(settings), buttons=_ACTIONS, title=TITLE,
                                           close_choice=len(_ACTIONS) - 1)
            name = _ACTIONS[action]
            if name == "Close":
                return
            if _act(settings, name):
                sm.save(settings)
    except UserError as ex:
        hou.ui.displayMessage(str(ex), severity=hou.severityType.Error, title=TITLE)
    except Exception:
        LOGGER.error("Project settings failed:\n%s", traceback.format_exc())
        hou.ui.displayMessage("The settings tool failed. This is a bug in the A3OB tools. See the console.",
                              severity=hou.severityType.Error, title=TITLE)


def _pick_preset(settings, message):
    import hou
    names = sm.preset_names(settings)
    picked = hou.ui.selectFromList(names, exclusive=True, title=TITLE, message=message, clear_on_cancel=True)
    return names[picked[0]] if picked else None


def _act(settings, action: str) -> bool:
    """Do one action. True when the settings changed and must be saved."""
    import hou
    if action == "Project root":
        ok, text = hou.ui.readInput("Project root", initial_contents=settings.project_root, buttons=("OK", "Cancel"))
        if ok != 0:
            return False
        sm.set_project_root(settings, text)
        return True
    if action == "Active preset":
        name = _pick_preset(settings, "Preset that Quick Export offers first")
        if name is None:
            return False
        sm.set_active_preset(settings, name)
        return True
    if action == "Edit preset":
        name = _pick_preset(settings, "Start from this preset")
        if name is None:
            return False
        values = sm.get_preset(settings, name)
        ok, new_name = hou.ui.readInput("Name of the preset (a built-in name cannot be replaced)",
                                        initial_contents=name if name not in sm.BUILTIN_PRESETS else name + " copy",
                                        buttons=("OK", "Cancel"))
        if ok != 0:
            return False
        options = [field for field in sm.PRESET_KEYS if isinstance(values.get(field, getattr(sm.ExportOptions(), field)), bool)]
        chosen = hou.ui.selectFromList(options, default_choices=[i for i, f in enumerate(options) if values.get(f)],
                                       exclusive=False, title=TITLE, message="Options that are on")
        for index, field in enumerate(options):
            values[field] = index in chosen
        sm.save_preset(settings, new_name, values)
        return True
    if action == "Delete preset":
        name = _pick_preset(settings, "Preset to delete")
        if name is None:
            return False
        sm.delete_preset(settings, name)
        return True
    if action == "Common data file":
        path = hou.ui.selectFile(title=TITLE, pattern="*.json", file_type=hou.fileType.Any)
        if path is None:
            return False
        settings.common_data = hou.text.expandString(path) if path else ""
        return True
    return False

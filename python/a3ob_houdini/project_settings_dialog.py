# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the Project root and presets shelf tool (ux-tools.md section 7.2).

A thin layer of ``hou.ui`` calls (no tests, they need the GUI). The text and index logic is in plain functions that
the tests cover. The work is in ``settings``.
The tool changes nothing in a scene.
"""

import dataclasses
import logging
import os
import traceback

from . import settings as sm
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")
TITLE = "A3OB Project root and presets"
_ACTIONS = ("Project root", "Active preset", "Edit preset", "Delete preset", "Common data file", "Arma 3 Samples folder", "Close")


def display_path(path: str) -> str:
    """A path as shown to the artist: the native slash everywhere. Display only; the JSON keeps the stored text."""
    return os.path.normpath(path) if path else path


def bool_options(values) -> list:
    """The preset fields that are on/off switches, in PRESET_KEYS order."""
    defaults = sm.ExportOptions()
    return [field for field in sm.PRESET_KEYS if isinstance(values.get(field, getattr(defaults, field)), bool)]


def values_from_indices(options, chosen) -> dict:
    """Every switch in ``options`` mapped to True when its index is in ``chosen``, else False."""
    picked = set(chosen)
    return {field: index in picked for index, field in enumerate(options)}


def preset_summary(name: str, options, chosen) -> str:
    """The text of the Save / Cancel step: the preset name and the options that will be on."""
    picked = set(chosen)
    on = [field for index, field in enumerate(options) if index in picked]
    return "Save preset \"%s\"?\n\nOptions that will be on:\n%s" % (name, "\n".join(on) if on else "(all off)")


def _describe(settings) -> str:
    lines = ["Settings file: %s" % display_path(sm.settings_path()),
             "Project root: %s%s" % (display_path(sm.effective_root(settings)),
                                     "  (from %s)" % sm.ENV_ROOT if sm.effective_root(settings) != settings.project_root else ""),
             "Active preset: %s" % settings.active_preset,
             "Common data file: %s" % (display_path(settings.common_data) or "(none)"),
             "Arma 3 Samples folder: %s" % (display_path(sm.effective_samples(settings)) or "(none found)")]
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
        hou.ui.displayMessage("The settings tool failed. This is a bug in the A3OB tools. See Windows > Log Viewer (source A3OB).",
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
        options = bool_options(values)
        # Houdini returns default_choices on Cancel, so the list cannot tell Cancel from Accept. The confirm step can.
        chosen = hou.ui.selectFromList(options, default_choices=[i for i, f in enumerate(options) if values.get(f)],
                                       exclusive=False, title=TITLE, message="Options that are on",
                                       clear_on_cancel=True)
        summary = preset_summary(new_name, options, chosen)
        # Save is button 0. Cancel is the default and the close button, so closing the window writes nothing.
        if hou.ui.displayMessage(summary, buttons=("Save", "Cancel"), default_choice=1, close_choice=1,
                                 title=TITLE) != 0:
            return False
        values.update(values_from_indices(options, chosen))
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
        settings.common_data = hou.text.expandString(path) if path else ""  # stored as picked; only the display changes
        return True
    if action == "Arma 3 Samples folder":
        ok, text = hou.ui.readInput("Arma 3 Samples folder (the one that holds Addons). Leave empty to use the Steam folder.",
                                    initial_contents=settings.samples_dir, buttons=("OK", "Cancel"))
        if ok != 0:
            return False
        settings.samples_dir = text.strip()
        return True
    return False

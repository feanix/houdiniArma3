# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the Selection browser shelf tool (ux-tools.md section 7.3, version 1).

This is only the thin layer of ``hou.ui`` calls. It needs the Houdini GUI, so it has no tests. All the work is in
``selection_browser``.
"""

import logging
import traceback

from . import selection_browser as browser
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")

_ACTIONS = ("Copy name", "Create selection", "Delete selection", "Rename selection", "Filter", "Close")


def _chosen_node(kwargs):
    import hou
    selected = hou.selectedNodes()
    if len(selected) != 1 or selected[0].type().category() != hou.sopNodeTypeCategory():
        raise UserError("Selection browser: select one SOP node first. Then run the tool again.")
    return selected[0]


def run(kwargs) -> None:
    """The shelf tool: show the groups of the selected SOP node and offer the actions."""
    import hou
    try:
        _loop(_chosen_node(kwargs))
    except UserError as ex:
        hou.ui.displayMessage(str(ex), severity=hou.severityType.Error, title="A3OB Selection browser")
    except Exception:
        LOGGER.error("Selection browser failed:\n%s", traceback.format_exc())
        hou.ui.displayMessage("The Selection browser failed. This is a bug in the A3OB tools. See the console.",
                              severity=hou.severityType.Error, title="A3OB Selection browser")


def _loop(node) -> None:
    import hou
    filter_text = ""
    while True:
        geo = node.geometry()
        entries = browser.list_groups(geo, filter_text) if geo is not None else []
        lines = [browser.describe(entry) for entry in entries]
        title = "Selections of %s%s" % (node.path(), " (filter: %s)" % filter_text if filter_text else "")
        if not entries and geo is not None and browser.has_packed_lods(geo):
            hou.ui.displayMessage(
                "The groups of a P3D geometry are inside its packed LODs. Add an Extract LOD node, then select it.",
                title=title)
            return
        # The list is only a view. The buttons below it are the actions.
        picked = hou.ui.selectFromList(lines, message=title, title="A3OB Selection browser", exclusive=True,
                                       clear_on_cancel=True) if lines else ()
        if lines and not picked:
            return
        entry = entries[picked[0]] if picked else None
        action = hou.ui.displayMessage("Action for %s" % (entry.name if entry else "this geometry"),
                                       buttons=_ACTIONS, title="A3OB Selection browser",
                                       close_choice=len(_ACTIONS) - 1)
        if _ACTIONS[action] == "Close":
            return
        if _ACTIONS[action] == "Filter":
            ok, text = hou.ui.readInput("Show names that contain", initial_contents=filter_text,
                                        buttons=("OK", "Cancel"))
            if ok == 0:
                filter_text = text
            continue
        _act(node, entry, _ACTIONS[action])


def _act(node, entry, action: str) -> None:
    import hou
    if action == "Create selection":
        ok, name = hou.ui.readInput("P3D name of the new selection", buttons=("OK", "Cancel"))
        if ok != 0:
            return
        ok, expression = hou.ui.readInput("Point numbers (e.g. 0-9, empty: all points)", buttons=("OK", "Cancel"))
        if ok != 0:
            return
        new = browser.create_selection(node, name, expression)
    elif entry is None:
        raise UserError("Selection browser: this geometry has no groups to work on.")
    elif action == "Copy name":
        hou.ui.copyTextToClipboard(entry.encoded)
        hou.ui.setStatusMessage("Copied %s" % entry.encoded)
        return
    elif action == "Delete selection":
        new = browser.delete_selection(node, entry.name)
    else:
        ok, name = hou.ui.readInput("New P3D name", initial_contents=entry.name, buttons=("OK", "Cancel"))
        if ok != 0:
            return
        new = browser.rename_selection(node, entry.name, name)
    new.setSelected(True, clear_all_selected=True)
    hou.ui.setStatusMessage("Added %s" % new.path())

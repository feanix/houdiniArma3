# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The dialogs of the New P3D shelf tool (ux-tools.md section 7.5).

Only the thin layer of ``hou.ui`` calls. It needs the Houdini GUI, so it has no tests. The work is in ``new_p3d``.
"""

import logging
import traceback

from . import new_p3d
from .errors import UserError

__all__ = ["run"]

LOGGER = logging.getLogger("a3ob")
TITLE = "A3OB New P3D"


def _current_sop_network():
    """The SOP network open in the network editor (a Geometry object or a subnet), or None."""
    import hou
    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    network = pane.pwd() if pane is not None else None
    if network is not None and network.childTypeCategory() == hou.sopNodeTypeCategory():
        return network
    return None


def run(kwargs) -> None:
    """The shelf tool: build the starter network in the open SOP network, or in a new Geometry object."""
    import hou
    try:
        parent = None
        current = _current_sop_network()
        if current is not None:
            choice = hou.ui.displayMessage("Build the starter network in %s, or in a new Geometry object?" % current.path(),
                                           buttons=("This network", "New object", "Cancel"), close_choice=2,
                                           default_choice=1, title=TITLE)
            if choice == 2:
                return
            parent = current if choice == 0 else None
        name = "new_p3d"
        if parent is None:
            clicked, name = hou.ui.readInput("Name of the new Geometry object", buttons=("OK", "Cancel"),
                                             initial_contents=name, close_choice=1, title=TITLE)
            if clicked != 0:
                return
        made = new_p3d.build_starter(parent, name)
        hou.ui.setStatusMessage("Built the starter network in %s. Replace the three Box and Add nodes with your model." %
                                made.network.path())
    except UserError as ex:
        hou.ui.displayMessage(str(ex), severity=hou.severityType.Error, title=TITLE)
    except Exception:
        LOGGER.error("New P3D failed:\n%s", traceback.format_exc())
        hou.ui.displayMessage("New P3D failed. This is a bug in the A3OB tools. See the console.",
                              severity=hou.severityType.Error, title=TITLE)

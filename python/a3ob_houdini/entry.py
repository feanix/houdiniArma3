# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The frozen entry functions (D13). HDA files and `.hip` files call only these.

Once a node has shipped, the names and arguments here must not change. Everything behind them may be renamed.

    cook(kind, node)             -- the PythonCook of a node
    button(kind, action, kwargs) -- a button callback

``cook`` turns every exception into a node error: a ``UserError`` shows its own text (already in the ux-tools.md
section 2 form); any other exception is a bug, so it shows a short text and the traceback goes to the console.
Callbacks never raise either (Houdini ignores errors in callbacks, so they report their own, spike finding Q4).
"""

import logging
import traceback

from . import messages, nodes
from .errors import UserError

__all__ = ["cook", "button"]

LOGGER = logging.getLogger("a3ob")


def cook(kind: str, node) -> None:
    """Cook a node of the given kind. Errors become node errors; the output is cleared on error."""
    report = messages.Report()
    try:
        nodes.load(kind).cook(node, report)
    except UserError as ex:
        report.error(str(ex))
    except Exception as ex:  # a bug in A3OB tools: show it on the node, keep the details on the console
        LOGGER.error("%s", traceback.format_exc())
        report.error("%s: %s failed with %s: %s. This is a bug in the A3OB tools. "
                     "Copy the console output into a bug report." % (
                         kind, node.path(), type(ex).__name__, ex))
    if report.has_errors:
        node.geometry().clear()
    messages.show_on_node(node, report)


def button(kind: str, action: str, kwargs) -> None:
    """Run the callback of a button parameter. Never raises."""
    try:
        nodes.load(kind).BUTTONS[action](kwargs)
    except Exception:  # Houdini would only print it; put the traceback on the console with our logger
        LOGGER.error("button %s/%s failed:\n%s", kind, action, traceback.format_exc())

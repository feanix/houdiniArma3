# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The frozen entry functions (D13). HDA files and `.hip` files call only these.

Once a node has shipped, the names and arguments here must not change. Everything behind them may be renamed.

    cook(kind, node)             -- the PythonCook of a node
    button(kind, action, kwargs) -- a button callback
    menu(kind, name, kwargs)     -- the items of a menu that is made when it opens (flat list: token, label, ...)

``cook`` turns every exception into a node error: a ``UserError`` shows its own text (already in the ux-tools.md
section 2 form); any other exception is a bug, so it shows a short text and the traceback goes to the console.
Callbacks never raise either (Houdini ignores errors in callbacks, so they report their own, spike finding Q4).
"""

import logging
import traceback

from . import messages, nodes
from .errors import UserError
from .messages import format_problem

__all__ = ["cook", "button", "menu"]

LOGGER = logging.getLogger("a3ob")


def _load(kind: str):
    """The module of a node kind. A kind this session does not know means stale code: say how to fix it."""
    try:
        return nodes.load(kind)
    except KeyError:
        raise UserError(format_problem(
            None, "The node kind %r is not in the A3OB code that is loaded in this Houdini session" % kind,
            "The package was probably updated while Houdini was running. Restart Houdini, or run the "
            "Reload A3OB shelf tool")) from None


def cook(kind: str, node) -> None:
    """Cook a node of the given kind. Errors become node errors; the output is cleared on error."""
    report = messages.Report()
    try:
        _load(kind).cook(node, report)
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


def menu(kind: str, name: str, kwargs) -> list:
    """The items of a dynamic menu parameter, flat as Houdini wants them. Never raises: a failure gives no items."""
    try:
        return [text for pair in getattr(nodes.load(kind), "MENUS", {})[name](kwargs) for text in pair]
    except Exception:
        LOGGER.error("menu %s/%s failed:\n%s", kind, name, traceback.format_exc())
        return []

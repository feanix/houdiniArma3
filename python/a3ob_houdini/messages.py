# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Messages for the artist (ux-tools.md section 2).

Houdini has three kinds of node message. A message is blue info. A warning turns the node yellow and geometry keeps
flowing. An error turns the node red and stops everything below it. This module collects messages in a ``Report``
while a node works, and shows them on the node at the end.

Text shape: ``<LOD name>: <what is wrong>. <how to fix it>.`` Element numbers are listed (first 5, then
"and 12 more"). At most ``MAX_MESSAGES`` are shown on the node; the rest are counted in one summary line. The full
list always goes to the console through the ``a3ob`` logger.

The helpers ``message``, ``warning`` and ``error`` act on a node straight away. They need a Houdini node, but this
module does not import ``hou``, so the text helpers can be tested under plain Python.
"""

import logging
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "MAX_MESSAGES", "INFO", "WARNING", "ERROR", "Report", "format_problem", "list_elements", "message", "warning",
    "error", "show_on_node",
]

LOGGER = logging.getLogger("a3ob")

MAX_MESSAGES = 25
INFO = "info"
WARNING = "warning"
ERROR = "error"

#: How many element numbers a message lists before it says "and N more".
LISTED_ELEMENTS = 5


def list_elements(label: str, numbers: Sequence[int], limit: int = LISTED_ELEMENTS) -> str:
    """``prims 12, 15, 40`` or ``prims 1, 2, 3, 4, 5 and 12 more``."""
    shown = ", ".join(str(number) for number in numbers[:limit])
    extra = len(numbers) - limit
    if extra > 0:
        shown += " and %d more" % extra
    return "%s %s" % (label, shown)


def format_problem(lod_name: Optional[str], what: str, fix: Optional[str] = None) -> str:
    """Build ``<LOD name>: <what is wrong>. <how to fix it>.`` (the LOD name and the fix are optional)."""
    text = what.rstrip(". ")
    if lod_name:
        text = "%s: %s" % (lod_name, text)
    text += "."
    if fix:
        text += " " + fix.rstrip(". ") + "."
    return text


class Report:
    """Messages collected while a node cooks."""

    def __init__(self) -> None:
        self.items: List[Tuple[str, str]] = []

    def info(self, text: str) -> None:
        self.items.append((INFO, text))

    def warning(self, text: str) -> None:
        self.items.append((WARNING, text))

    def error(self, text: str) -> None:
        self.items.append((ERROR, text))

    def texts(self, severity: str) -> List[str]:
        return [text for kind, text in self.items if kind == severity]

    @property
    def has_errors(self) -> bool:
        return any(kind == ERROR for kind, _ in self.items)

    def lines(self, max_messages: int = MAX_MESSAGES) -> List[Tuple[str, str]]:
        """The messages to show: errors first, then warnings, then info, cut to ``max_messages``.

        When messages are cut, a last info line says how many."""
        rank = {ERROR: 0, WARNING: 1, INFO: 2}
        ordered = sorted(self.items, key=lambda item: rank[item[0]])  # stable: keeps the order inside a kind
        if len(ordered) <= max_messages:
            return ordered
        shown = ordered[:max_messages - 1]
        shown.append((INFO, "%d more messages are not shown. See the console for the full list."
                      % (len(ordered) - len(shown))))
        return shown


def show_on_node(node, report: Report, max_messages: int = MAX_MESSAGES) -> None:
    """Put the report on a Houdini node and the full list on the console."""
    for kind, text in report.items:
        level = {INFO: logging.INFO, WARNING: logging.WARNING, ERROR: logging.ERROR}[kind]
        LOGGER.log(level, "%s: %s", node.path(), text)
    for kind, text in report.lines(max_messages):
        if kind == ERROR:
            node.addError(text)
        elif kind == WARNING:
            node.addWarning(text)
        else:
            node.addMessage(text)


def message(node, text: str) -> None:
    """Show an info message on a node."""
    node.addMessage(text)


def warning(node, text: str) -> None:
    """Show a warning on a node. Geometry keeps flowing."""
    node.addWarning(text)


def error(node, text: str) -> None:
    """Show an error on a node. The node and everything below it stops."""
    node.addError(text)

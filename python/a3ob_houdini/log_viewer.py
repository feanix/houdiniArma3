# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Send the ``a3ob`` logger to Houdini's Log Viewer (task #46).

Without a handler, Python writes warnings and errors to stderr, and Houdini on Windows opens its console window for
that. So ``install()`` puts one handler on the ``a3ob`` logger. In Houdini it sends each record to the log source
``A3OB`` (Windows > Log Viewer). Outside Houdini it is a ``NullHandler`` and nothing is written.

Houdini 22.0.466 API (checked live): ``hou.logging.createSource(name)`` makes the source; ``hou.logging.log(entry,
name)`` sends a ``hou.logging.LogEntry(message=..., severity=hou.severityType.X)``; ``hou.logging.sources()`` returns
plain strings. ``install()`` is safe to call again (Reload A3OB re-runs this module): a marker on the logger itself
stops a second handler, because a module global would be reset by the reload.
"""

import logging

__all__ = ["SOURCE", "install"]

SOURCE = "A3OB"
_MARKER = "_a3ob_log_viewer_handler"


class _LogViewerHandler(logging.Handler):
    """Hand each record to ``hou.logging``. Never raises."""

    def __init__(self, hou) -> None:
        super().__init__()
        self._hou = hou
        hou.logging.createSource(SOURCE)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            hou = self._hou
            if record.levelno >= logging.ERROR:
                severity = hou.severityType.Error
            elif record.levelno >= logging.WARNING:
                severity = hou.severityType.Warning
            else:
                severity = hou.severityType.Message
            entry = hou.logging.LogEntry(message=self.format(record), source=SOURCE, severity=severity)
            hou.logging.log(entry, SOURCE)
        except Exception:  # a log handler must not break a cook, and must not print
            pass


def install() -> None:
    """Put the one handler on the ``a3ob`` logger, unless it already has it."""
    logger = logging.getLogger("a3ob")
    if getattr(logger, _MARKER, None) is not None:
        return
    try:
        import hou
        handler = _LogViewerHandler(hou)
    except Exception:  # no Houdini (plain Python): stay silent
        handler = logging.NullHandler()
    logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)  # Python's default (warnings only) would drop the info lines of the full list
    setattr(logger, _MARKER, handler)

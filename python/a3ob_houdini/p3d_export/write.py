# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Write the P3D bytes to disk (D13): to a temporary file next to the target, then rename. No ``hou`` here.

A failed write leaves the old file as it was. The function catches its own errors and reports them: a Houdini button
callback swallows exceptions, so nothing may escape from here.
"""

import os
import shutil
from typing import Optional

from .. import messages

__all__ = ["write_file"]


def write_file(path: str, data: bytes, report: messages.Report, backup: bool = False) -> bool:
    """Write ``data`` to ``path`` through a temporary file. Returns True on success; failures go to ``report``.

    With ``backup`` an existing file is kept as ``<path>.bak`` first (A3OB's "Create Backups")."""
    path = os.path.abspath(os.path.expandvars(os.fspath(path))) if path else ""
    if not path:
        report.error(messages.format_problem(None, "no output file is set", "Set the file parameter"))
        return False
    temp: Optional[str] = None
    try:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        temp = "%s.%d.tmp" % (path, os.getpid())
        with open(temp, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if backup and os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        os.replace(temp, path)
        temp = None
        return True
    except OSError as ex:
        report.error(messages.format_problem(None, "could not write %s (%s)" % (path, ex.strerror or ex),
                                             "Check that the folder exists, is writable and the file is not open elsewhere"))
        return False
    finally:
        if temp is not None:
            try:
                os.remove(temp)
            except OSError:
                pass

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

__all__ = ["write_file", "with_p3d_extension"]


def with_p3d_extension(path: str) -> str:
    """``path`` with ``.p3d`` added unless it already ends in ``.p3d`` (any case). An empty path stays empty.

    Same as Blender's ``ExportHelper.check_extension`` with A3OB's ``filename_ext = ".p3d"``
    (Arma3ObjectBuilder/ui/import_export_p3d.py:235): ``model`` -> ``model.p3d``, ``model.v2`` -> ``model.v2.p3d``.
    ``write_file`` applies it, so the backup and temp names follow the final name.

    The check reads the path with environment variables expanded (``$MODEL`` may already end in ``.p3d``), but the
    path comes back as typed, so a message or a remembered path shows what the user wrote."""
    path = os.fspath(path) if path else ""
    if path and not os.path.expandvars(path).lower().endswith(".p3d"):
        return path + ".p3d"
    return path


def write_file(path: str, data: bytes, report: messages.Report, backup: bool = False) -> bool:
    """Write ``data`` to ``path`` (plus ``.p3d`` when missing) through a temporary file. Returns True on success;
    failures go to ``report``.

    With ``backup`` an existing file is kept as ``<path>.bak`` first (A3OB's "Create Backups")."""
    path = os.path.abspath(os.path.expandvars(with_p3d_extension(path))) if path else ""
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

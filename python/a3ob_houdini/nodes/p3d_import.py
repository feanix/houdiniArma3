# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The P3D Import node (ux-tools.md section 5.1): its parameter table and its cook.

Per D14 (Q-11) none of A3OB's extra import options are offered. Parameters: File, Reload, LODs, Explicit face groups.
"""

import os
import time

from a3ob_core import p3d

from ..errors import UserError
from ..messages import Report, format_problem
from ..p3d_import import import_model, parse_lod_filter
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS"]

SPEC = NodeSpec(
    kind="p3d_import",
    label="P3D Import",
    summary="Reads an Arma 3 .p3d file (MLOD) and outputs one packed primitive per LOD.",
    inputs=(0, 0),
    parms=(
        Parm("file", "File", "file", "", "The .p3d file to read. Must be an MLOD file, not a binarized one.",
             file_pattern="*.p3d"),
        Parm("reload", "Reload", "button", "", "Read the file again, even if nothing changed.", action="reload"),
        Parm("lods", "LODs", "string", "*",
             "Which LODs to import, by position in the file: * for all, or numbers and ranges like 0 2-4."),
        Parm("facegroups", "Explicit Face Groups", "toggle", False,
             "Always make a primitive group for every selection, not only where the face data needs one."),
    ),
)


def cook(node, report: Report) -> None:
    """Read the file and write the P3D geometry into the node's output."""
    values = read_parms(node, SPEC)
    path = _file_path(values["file"])
    model = _read(path, report)
    indices = parse_lod_filter(values["lods"], len(model.lods))
    started = time.perf_counter()
    result = import_model(model, node.geometry(), node.path(), report, indices, bool(values["facegroups"]))
    if result.skipped_lods:
        report.warning(format_problem(
            None, "%d of %d LODs were not imported" % (result.skipped_lods, len(model.lods)),
            "They are missing from the P3D geometry, so exporting it will not write them"))
    report.info("%d LODs, %d packed proxies, %d inline proxies (%.2f s)." % (
        result.lod_count, result.packed_proxies, result.inline_proxies, time.perf_counter() - started))


def _file_path(text: str) -> str:
    if not text.strip():
        raise UserError(format_problem("P3D Import", "no file is chosen", "Pick a .p3d file in the File parameter"))
    if not os.path.isfile(text):
        raise UserError(format_problem("P3D Import", "the file %r does not exist" % text,
                                       "Check the File parameter"))
    return text


def _read(path: str, report: Report) -> p3d.P3DModel:
    try:
        model = p3d.read_p3d_file(path, strict=False)
    except p3d.P3DError as ex:
        raise UserError(format_problem("P3D Import", "the file cannot be read: %s" % ex,
                                       "Is it a binarized (ODOL) file? Only MLOD files can be imported")) from None
    except OSError as ex:
        raise UserError(format_problem("P3D Import", "the file cannot be opened: %s" % ex)) from None
    for text in model.warnings:
        report.warning(format_problem("P3D Import", text))
    return model


def reload(kwargs) -> None:
    """The Reload button: cook the node again."""
    kwargs["node"].cook(force=True)


BUTTONS = {"reload": reload}

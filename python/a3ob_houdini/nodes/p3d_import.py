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
from ..p3d_import.lod_filter import drop_star
from ..p3d_import.lod_list import read_lod_names
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS", "lod_menu_items"]

_ODOL_HELP = ("Only the editable MLOD version that Object Builder saves can be opened. Files taken out of the game's "
              "packed PBOs (binarized, ODOL) can't be")

SPEC = NodeSpec(
    kind="p3d_import",
    label="P3D Import",
    summary="Reads an Arma 3 .p3d file (MLOD) and outputs one packed primitive per LOD.",
    inputs=(0, 0),
    parms=(
        Parm("file", "File", "file", "",
             "The .p3d model to open. Use the editable version Object Builder saves (MLOD). Files taken out of the "
             "game's packed PBOs (binarized, ODOL) can't be opened.",
             file_pattern="*.p3d"),
        Parm("reload", "Reload", "button", "",
             "Read the file again from disk, for example after you saved it in Object Builder.", action="reload"),
        Parm("lods", "LODs", "menu", "*",
             "Which LODs to bring in. Pick them from the menu, or type numbers or names. * means all. "
             "Picking from the menu replaces a * in the field, and a * next to other entries still means all.",
             action="lods_changed", menu_action="lod_choices", menu_toggle=True),
        Parm("facegroups", "Explicit Face Groups", "toggle", False,
             "A face group is a named set of faces. Every selection comes in as a group of points. Normally a face "
             "group is added only when the selection's faces are not just the faces whose points are all in it. "
             "Turn this on to get a face group for every selection, for example when you want to select or "
             "blast faces by selection name."),
    ),
)


def cook(node, report: Report) -> None:
    """Read the file and write the P3D geometry into the node's output."""
    values = read_parms(node, SPEC)
    path = _file_path(values["file"])
    model = _read(path, report)
    problems = []
    indices = parse_lod_filter(values["lods"], len(model.lods), [lod.name for lod in model.lods], problems)
    for text in problems:
        report.warning(format_problem("P3D Import", text))
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
        if "ODOL" in str(ex):
            raise UserError(format_problem("P3D Import", "this is a binarized (ODOL) file, not an editable MLOD file",
                                           _ODOL_HELP)) from None
        raise UserError(format_problem("P3D Import", "the file cannot be read: %s" % ex, _ODOL_HELP)) from None
    except OSError as ex:
        raise UserError(format_problem("P3D Import", "the file cannot be opened: %s" % ex)) from None
    for text in model.warnings:
        report.warning(format_problem("P3D Import", text))
    return model


def reload(kwargs) -> None:
    """The Reload button: cook the node again."""
    kwargs["node"].cook(force=True)


def lods_changed(kwargs) -> None:
    """The LODs field changed (a menu pick or typing): a * next to other entries is dropped, so a pick starting
    from the default * selects just that LOD."""
    parm = kwargs["node"].parm("lods")
    text = parm.eval()
    fixed = drop_star(text)
    if fixed != text:
        parm.set(fixed)


def lod_menu_items(names) -> list:
    """``[(token, label)]`` for the LODs menu: the token is the position, the label reads ``5  Geometry``."""
    return [(str(index), "%d  %s" % (index, name)) for index, name in enumerate(names)]


def _menu_lods(kwargs) -> list:
    """The LODs menu: the LODs of the chosen file. No file, or a file that cannot be read, gives an empty menu."""
    path = kwargs["node"].parm("file").eval().strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        return lod_menu_items(read_lod_names(path))
    except (p3d.P3DError, OSError):
        return []


BUTTONS = {"reload": reload, "lods_changed": lods_changed}
MENUS = {"lod_choices": _menu_lods}

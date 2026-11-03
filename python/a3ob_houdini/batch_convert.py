# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Batch folder convert without a dialog (ux-tools.md section 7.4, D14 Q-5).

"Convert" means re-export with a report. Every ``.p3d`` under a folder is imported, exported again with a preset into
an output folder, and compared with its source. The report says for each file:

    identical   the new file has the same bytes as the source (L0)
    different   the bytes differ; the report gives the first differing offset and the sizes
    failed      the file could not be read, imported or exported; the report gives the error

The sources are never written to. The output folder must not be the source folder or inside it, and a file is never
written onto the file it came from. One model is held at a time. ``batch_convert_dialog`` is the thin ``hou.ui``
layer. Needs ``hou`` (the importer builds geometry).
"""

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from a3ob_core import p3d

from . import messages, p3d_export, settings as settings_module
from .errors import UserError
from .messages import format_problem

__all__ = ["REPORT_NAME", "IDENTICAL", "DIFFERENT", "FAILED", "FileResult", "BatchResult", "default_output",
           "find_files", "convert_folder", "report_text"]

TOOL = "Batch convert"
REPORT_NAME = "a3ob_batch_report.txt"
IDENTICAL = "identical"
DIFFERENT = "different"
FAILED = "failed"


@dataclass
class FileResult:
    """The outcome for one file. ``name`` is the path under the input folder."""

    name: str
    status: str
    #: Failed: the error. Different: where and how. Identical: empty.
    detail: str = ""
    #: Warnings the import or export gave (plain texts).
    warnings: List[str] = field(default_factory=list)


@dataclass
class BatchResult:
    input_folder: str
    output_folder: str
    preset: str
    files: List[FileResult] = field(default_factory=list)
    report_path: str = ""
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(1 for item in self.files if item.status == status)


def default_output(input_folder: str) -> str:
    """The input folder plus ``_converted`` (ux-tools.md 7.4)."""
    return os.path.normpath(input_folder).rstrip("\\/") + "_converted"


def find_files(folder: str, recursive: bool = False) -> List[str]:
    """The ``.p3d`` files of ``folder`` as paths under it (``a.p3d``, ``sub/b.p3d``), sorted."""
    found: List[str] = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".p3d"):
                found.append(os.path.relpath(os.path.join(root, name), folder))
        if not recursive:
            break
    return found


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, folder: str) -> bool:
    """True when ``path`` is ``folder`` or below it."""
    path, folder = _real(path), _real(folder)
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # another drive
        return False


def _check_folders(input_folder: str, output_folder: str) -> None:
    if not os.path.isdir(input_folder):
        raise UserError(format_problem(TOOL, "the input folder %r does not exist" % input_folder,
                                       "Choose a folder that holds .p3d files"))
    if _inside(output_folder, input_folder):
        raise UserError(format_problem(TOOL, "the output folder %r is the input folder or inside it" % output_folder,
                                       "Choose another output folder; the sources are never overwritten"))


def convert_folder(input_folder: str, output_folder: Optional[str] = None, recursive: bool = False,
                   preset: Optional[str] = None, settings=None,
                   progress: Optional[Callable[[int, int, str], bool]] = None) -> BatchResult:
    """Re-export every ``.p3d`` of ``input_folder`` into ``output_folder`` and write ``a3ob_batch_report.txt`` there.

    ``output_folder`` defaults to the input folder plus ``_converted``. ``preset`` is a preset name (default: the active
    one of ``settings``). ``progress(done, total, name)`` is called before each file; return False to cancel.
    Problems with a file are results, not exceptions. Raises ``UserError`` for a missing input folder, an output folder
    that is (or is inside) the input folder, an unknown preset or an output folder that cannot be made."""
    if settings is None:
        settings, _ = settings_module.load()
    input_folder = os.path.abspath(input_folder)
    output_folder = os.path.abspath(output_folder or default_output(input_folder))
    _check_folders(input_folder, output_folder)
    preset = preset or settings.active_preset
    options = settings_module.options_for(settings, preset)  # raises for an unknown preset
    result = BatchResult(input_folder, output_folder, preset)
    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as ex:
        raise UserError(format_problem(TOOL, "the output folder %r cannot be made (%s)" % (
            output_folder, ex.strerror or ex), "Choose another output folder")) from None
    names = find_files(input_folder, recursive)
    started = time.perf_counter()
    for number, name in enumerate(names):
        if progress is not None and progress(number, len(names), name) is False:
            result.cancelled = True
            break
        result.files.append(_convert_one(os.path.join(input_folder, name), os.path.join(output_folder, name), name,
                                         options))
    result.report_path = os.path.join(output_folder, REPORT_NAME)
    try:
        with open(result.report_path, "w", encoding="utf-8") as file:
            file.write(report_text(result, time.perf_counter() - started))
    except OSError as ex:
        raise UserError(format_problem(TOOL, "the report %r cannot be written (%s)" % (
            result.report_path, ex.strerror or ex), "Check that the output folder is writable")) from None
    return result


def _convert_one(source: str, target: str, name: str, options) -> FileResult:
    """Import ``source``, export it to ``target`` and compare. Never raises."""
    import hou
    from . import p3d_import

    if _real(source) == _real(target):  # cannot happen with the folder check; a second lock on the sources
        return FileResult(name, FAILED, "the output file is the source file")
    report = messages.Report()
    try:
        model = p3d.read_p3d_file(source, strict=False)
        geo = hou.Geometry()
        p3d_import.import_model(model, geo, source, report)
        del model
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not p3d_export.export_geometry([geo], target, options, report):
            return FileResult(name, FAILED, _error_text(report), report.texts(messages.WARNING))
        geo.clear()
        return _compare(source, target, name, report.texts(messages.WARNING))
    except p3d.P3DError as ex:
        return FileResult(name, FAILED, "the file cannot be read: %s" % ex, report.texts(messages.WARNING))
    except UserError as ex:
        return FileResult(name, FAILED, str(ex), report.texts(messages.WARNING))
    except Exception as ex:  # a bug or a disk problem: this file fails, the batch goes on
        return FileResult(name, FAILED, "%s: %s" % (type(ex).__name__, ex), report.texts(messages.WARNING))


def _error_text(report: messages.Report) -> str:
    errors = report.texts(messages.ERROR)
    return " | ".join(errors[:3]) + (" (and %d more errors)" % (len(errors) - 3) if len(errors) > 3 else "")


def _compare(source: str, target: str, name: str, warnings: List[str]) -> FileResult:
    with open(source, "rb") as file:
        old = file.read()
    with open(target, "rb") as file:
        new = file.read()
    if old == new:
        return FileResult(name, IDENTICAL, "", warnings)
    first = next((index for index, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    return FileResult(name, DIFFERENT, "first difference at byte %d; source %d bytes, new %d bytes" % (
        first, len(old), len(new)), warnings)


def report_text(result: BatchResult, seconds: float = 0.0) -> str:
    """The text of ``a3ob_batch_report.txt``."""
    lines = [
        "A3OB batch convert",
        "Input folder:  %s" % result.input_folder,
        "Output folder: %s" % result.output_folder,
        "Preset:        %s" % result.preset,
        "",
        "%d files: %d identical, %d different, %d failed%s (%.1f s)" % (
            len(result.files), result.count(IDENTICAL), result.count(DIFFERENT), result.count(FAILED),
            ", cancelled" if result.cancelled else "", seconds),
        "",
    ]
    for item in result.files:
        lines.append("%s: %s%s" % (item.status.upper(), item.name, " -- " + item.detail if item.detail else ""))
        lines.extend("    warning: " + text for text in item.warnings)
    return "\n".join(lines) + "\n"

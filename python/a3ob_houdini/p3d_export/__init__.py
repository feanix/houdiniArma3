# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""P3D export: turn Houdini geometry into an ``a3ob_core`` ``P3DModel`` and bytes (spec sections 3, 13 and 16).

The entry functions for the Validate and P3D Export nodes:

    report = messages.Report()
    collection = p3d_export.collect([geo], report)               # needs hou: spec 16 step 1
    model = p3d_export.build(collection, options, report)        # spec 16 steps 2 to 6; None on errors
    data = p3d_export.to_bytes(model, report)                    # the file bytes
    ok = p3d_export.write_file("a.p3d", data, report)            # temp file, then rename

``export_geometry`` does all four. ``collect_lods`` and ``read_geo`` need ``hou``; everything else works on the plain data
of ``data`` and runs under plain Python, so the decisions can be tested there.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from a3ob_core.p3d import P3DModel
from a3ob_core.p3d.model import SelectionTagg

from .. import messages
from .data import Collection
from .model_build import build_model as build
from .model_build import to_bytes
from .options import ExportOptions
from .write import with_p3d_extension, write_file

__all__ = [
    "ExportOptions", "Collection", "collect", "build", "to_bytes", "write_file", "with_p3d_extension", "export_geometry",
    "build_bytes", "run", "BuildResult",
]


def collect(geometries: Sequence, report: messages.Report) -> Collection:
    """Spec 16 step 1 (needs ``hou``): the LODs of the geometries, in order."""
    from .collect_lods import collect as _collect
    return _collect(geometries, report)


@dataclass
class BuildResult:
    """What ``run`` found: the file bytes (None when there are errors) and how many LODs were collected."""

    data: Optional[bytes] = None
    lod_count: int = 0
    #: ``(LOD name, selections written)`` for each LOD, when the file was built.
    selection_counts: List[Tuple[str, int]] = field(default_factory=list)


def run(geometries: Sequence, options: Optional[ExportOptions], report: messages.Report) -> BuildResult:
    """Collect, validate and build. The one place the Validate and P3D Export nodes get their messages from."""
    options = options or ExportOptions()
    collection = collect(geometries, report)
    result = BuildResult(None, len(collection.lods))
    if report.has_errors:
        return result
    model: Optional[P3DModel] = build(collection, options, report)
    if model is not None:
        result.data = to_bytes(model, report)
        result.selection_counts = [(lod.name, sum(1 for tagg in lod.taggs if isinstance(tagg, SelectionTagg)))
                                   for lod in model.lods]
    return result


def build_bytes(geometries: Sequence, options: Optional[ExportOptions], report: messages.Report) -> Optional[bytes]:
    """Collect, validate and build; return the file bytes, or None when ``report`` has errors."""
    return run(geometries, options, report).data


def export_geometry(geometries: Sequence, path: str, options: Optional[ExportOptions], report: messages.Report,
                    backup: bool = False) -> bool:
    """Collect, validate, build and write a file. Never raises for problems of the data or the disk: they are errors in
    ``report``, and the result is False (D13)."""
    data = build_bytes(geometries, options, report)
    if data is None:
        return False
    return write_file(path, data, report, backup)

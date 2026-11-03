# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""P3D import: turn an ``a3ob_core`` ``P3DModel`` into the P3D geometry (spec sections 3 and 16).

    from a3ob_houdini.p3d_import import import_model
    import_model(model, geo, source_key="/obj/geo1/p3d_import1")

The result is one packed prim per LOD, in file order, each with the outer attribute ``name``. Each LOD is built by
``lod_builder`` from the plan of ``plan``. ``import_model`` needs ``hou``; ``plan`` and ``lod_filter`` do not.
"""

import base64
from typing import List, Optional, Sequence

from a3ob_core.p3d import P3DModel

from .. import messages, names
from ..errors import UserError
from .lod_filter import parse_lod_filter
from .plan import LodPlan, make_plan

__all__ = ["ImportResult", "import_model", "parse_lod_filter", "make_plan"]


class ImportResult:
    """What an import did, for the summary message."""

    def __init__(self) -> None:
        self.lod_count = 0
        self.packed_proxies = 0
        self.inline_proxies = 0
        self.skipped_lods = 0


def import_model(model: P3DModel, geo, source_key: str, report: Optional[messages.Report] = None,
                 lod_indices: Optional[Sequence[int]] = None, facegroups: bool = False) -> ImportResult:
    """Write the P3D geometry of ``model`` into the empty ``hou.Geometry`` ``geo`` (spec 16, steps 1 to 10).

    ``source_key`` makes the LOD uids (spec 3.5): the importer node's path. ``lod_indices`` picks LODs by file
    position (default all). Problems that the artist can fix go to ``report``."""
    import hou
    from . import lod_builder

    report = report if report is not None else messages.Report()
    indices: List[int] = list(range(len(model.lods))) if lod_indices is None else list(lod_indices)
    options = lod_builder.ImportOptions(facegroups)
    result = ImportResult()
    geo.addAttrib(hou.attribType.Prim, "name", "")
    for index in indices:
        plan = _plan_lod(model, index)
        for text in plan.warnings:
            report.warning(text)
        for text in plan.notes:
            report.info(text)
        inner = lod_builder.build_lod_geometry(plan, names.lod_uid(source_key, index), options)
        _pack_lod(geo, inner, plan.lod_name)
        result.lod_count += 1
        result.packed_proxies += len(plan.proxies)
        result.inline_proxies += plan.inline_proxy_count
    result.skipped_lods = len(model.lods) - len(indices)
    if model.trailing:
        geo.addAttrib(hou.attribType.Global, "a3ob_file_trailing", "")
        geo.setGlobalAttribValue("a3ob_file_trailing", base64.b64encode(model.trailing).decode("ascii"))
    return result


def _plan_lod(model: P3DModel, index: int) -> LodPlan:
    try:
        return make_plan(model.lods[index], index)
    except ValueError as ex:  # a damaged file; the text is finished
        raise UserError(str(ex)) from None


def _pack_lod(geo, inner, lod_name: str) -> None:
    """Spec 16 step 10: one packed prim per LOD, with the outer attribute ``name``."""
    prim = geo.createPacked("PackedGeometry")
    prim.setEmbeddedGeometry(inner.freeze())
    prim.setAttribValue("name", lod_name)

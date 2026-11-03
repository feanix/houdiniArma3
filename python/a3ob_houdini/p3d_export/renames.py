# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_p3d.py ``renumber_components`` (line 651) and io/export_p3d.py ``translate_selections``
# (line 591) and ``cleanup_uvs`` (line 275), v2.5.1 (9f2b8bc): they work on the built TAGG items of a LOD instead of
# Blender objects. A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
"""The three D17 export options (spec 18): renumber components, Czech names, drop UVs. No ``hou`` here.

Renumber and Czech act on the live selection TAGGs of a LOD after they are put in file order. Drop UVs acts on a built
LOD, so it also works for a LOD copy that is written with another type than its source.
"""

import re
from array import array
from typing import Dict, List, Sequence, Tuple

from a3ob_core import p3d
from a3ob_core.p3d import LOD

from .. import messages
from .czech_names import translate_name
from .options import UV_LOD_TYPES, ExportOptions, keeps_uvs
from .taggs import Item

__all__ = ["UV_LOD_TYPES", "keeps_uvs", "renumber_components", "translate_names", "apply_name_options", "strip_uvs"]

_COMPONENT = re.compile(r"component\d+", re.IGNORECASE)


def _live_selections(items: Sequence[Item]) -> List[Item]:
    """Selections made from the Houdini data. Blobs (opaque TAGGs) are never renamed."""
    return [item for item in items if not item.blob and isinstance(item.tagg, p3d.SelectionTagg)
            and not item.tagg.name.lower().startswith("proxy:")]


def renumber_components(items: Sequence[Item]) -> int:
    """Spec 18.2: names that start with ``component<digits>`` become ``component01``, ``component02``... in file order.
    Returns how many names changed."""
    changed = 0
    counter = 1
    for item in _live_selections(items):
        tagg = item.tagg
        if not _COMPONENT.match(tagg.name):
            continue
        new = "component%02d" % counter
        counter += 1
        if tagg.name != new:
            tagg.name = new
            changed += 1
    return changed


def translate_names(items: Sequence[Item]) -> List[Tuple[str, str]]:
    """Spec 18.3: English names become Czech. Returns ``(before, after)`` for every name that changed."""
    done: List[Tuple[str, str]] = []
    for item in _live_selections(items):
        tagg = item.tagg
        new = translate_name(tagg.name)
        if new != tagg.name:
            done.append((tagg.name, new))
            tagg.name = new
    return done


def _warn_czech_collisions(label: str, before: List[str], after: List[str], translated: List[bool],
                           report: messages.Report) -> None:
    """Two selections that now share a name (case-insensitive), where translation made at least one of them."""
    first: Dict[str, int] = {}
    for index, name in enumerate(after):
        folded = name.lower()
        if folded not in first:
            first[folded] = index
            continue
        other = first[folded]
        if translated[index] or translated[other]:
            report.warning(messages.format_problem(
                label, "Czech translation makes the selection names %r and %r equal. Both are still written" % (
                    before[other], before[index]), "Rename one of them"))


def apply_name_options(items: Sequence[Item], options: ExportOptions, label: str, report: messages.Report) -> None:
    """Steps 5 and 6 of spec 18.1: renumber, then Czech names. The caller has put ``items`` in file order."""
    if options.renumber_components:
        count = renumber_components(items)
        if count:
            report.info(messages.format_problem(label, "renamed %d component selections" % count))
    if options.czech_names:
        selections = _live_selections(items)
        before = [item.tagg.name for item in selections]
        done = translate_names(items)
        if done:
            report.info(messages.format_problem(label, "translated %d selection names to Czech" % len(done)))
            after = [item.tagg.name for item in selections]
            _warn_czech_collisions(label, before, after, [a != b for a, b in zip(before, after)], report)


def strip_uvs(lod: LOD) -> bool:
    """Spec 18.4: zero the face UVs and remove every ``#UVSet#`` TAGG (blobs too). True if anything was removed."""
    removed = any(tagg.name == p3d.UVSET for tagg in lod.taggs) or any(value != 0.0 for value in lod.face_uvs)
    lod.face_uvs = array(lod.face_uvs.typecode, bytes(lod.face_uvs.itemsize * len(lod.face_uvs)))
    lod.taggs = [tagg for tagg in lod.taggs if tagg.name != p3d.UVSET]
    return removed

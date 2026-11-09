# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Extract LOD node (ux-tools.md section 5.2): its parameter table and its cook.

It takes one packed LOD out of a P3D geometry and outputs it as a LOD geometry (spec sections 3.1 and 3.2). The work
is the Unpack verb on a geometry that holds only that LOD, so the detail attributes stay detail attributes (T7e).
"""

from typing import List, Sequence

from ..errors import UserError
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS", "resolve_lod", "lod_menu_items"]

NAME = "Extract LOD"

SPEC = NodeSpec(
    kind="extract_lod",
    label=NAME,
    summary="Takes one LOD out of a P3D geometry and outputs it as a LOD geometry that you can edit.",
    inputs=(1, 1),
    input_labels=("P3D geometry",),
    input_help=("The geometry from P3D Import, with every LOD packed into its own primitive.",),
    parms=(
        Parm("lod", "LOD", "menu", "0",
             "Which LOD to extract: pick one from the menu, or type its position in the P3D geometry "
             "(0 is the first) or its name, for example Geometry or Resolution 1.",
             menu_action="lods"),
    ),
)


def lod_menu_items(names: Sequence[str]) -> List[tuple]:
    """``[(token, label)]`` for the LOD menu: the token is the position, the label reads ``0: Resolution 1``."""
    return [(str(index), "%d: %s" % (index, name)) for index, name in enumerate(names)]


def _menu_lods(kwargs) -> List[tuple]:
    """The menu of the LOD parameter: the LODs of the connected input (empty when there is none)."""
    from .. import lod_container as lc
    inputs = kwargs["node"].inputs()
    source = inputs[0].geometry() if inputs and inputs[0] is not None else None
    if source is None or lc.classify(source) != lc.P3D:
        return []
    return lod_menu_items([lc.prim_name(prim, lc.lod_content(prim)) for prim in source.prims()])


def resolve_lod(text: str, names: Sequence[str]) -> int:
    """The position of the LOD that ``text`` means. ``text`` is a number or a LOD name.

    A menu-style text like ``4: Geometry`` also works (the number before the colon is used)."""
    wanted = text.strip()
    if not wanted:
        raise UserError(format_problem(NAME, "no LOD is chosen", "Type a LOD number or name in the LOD parameter"))
    head = wanted.split(":", 1)[0].strip()
    if head.isdigit():
        index = int(head)
        if index >= len(names):
            raise UserError(format_problem(
                None, "The input has %d LODs, LOD %d does not exist" % (len(names), index),
                "Pick a number from 0 to %d" % (len(names) - 1) if names else None))
        return index
    matches: List[int] = [index for index, name in enumerate(names) if name.lower() == wanted.lower()]
    if not matches:
        raise UserError(format_problem(
            NAME, "the input has no LOD named %r" % wanted, "The LODs are: " + ", ".join(names)))
    if len(matches) > 1:
        raise UserError(format_problem(
            NAME, "%d LODs are named %r (positions %s)" % (len(matches), wanted, ", ".join(map(str, matches))),
            "Type the position instead of the name"))
    return matches[0]


def cook(node, report: Report) -> None:
    """Unpack the chosen LOD of the input into the node's output."""
    import hou
    from .. import lod_container as lc

    values = read_parms(node, SPEC)
    source = node.inputGeometry(0)
    if source is None:
        raise UserError(format_problem(NAME, "nothing is connected to the input",
                                       "Connect P3D Import or Assemble LODs"))
    if lc.classify(source) != lc.P3D:
        raise UserError(format_problem(
            None, "Input is not a P3D geometry (packed LODs)", "Connect P3D Import or Assemble LODs"))
    prims = source.prims()
    names = [lc.prim_name(prim, lc.lod_content(prim)) for prim in prims]
    index = resolve_lod(values["lod"], names)
    prim = prims[index]
    if not lc.is_identity(prim.fullTransform()):
        report.warning(format_problem(names[index], "Transform baked in",
                                      "The LOD had a transform; the extracted points carry it"))
    single = lc.only_prim(prim)
    out = node.geometry()
    hou.sopNodeTypeCategory().nodeVerb("unpack").execute(out, [single])
    polygons = sum(1 for item in out.prims() if isinstance(item, hou.Polygon))
    report.info("Extracted LOD %d: %s, %d points, %d polygons." % (index, names[index], len(out.points()), polygons))


BUTTONS = {}
MENUS = {"lods": _menu_lods}

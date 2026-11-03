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

__all__ = ["SPEC", "cook", "BUTTONS", "resolve_lod"]

NAME = "Extract LOD"

SPEC = NodeSpec(
    kind="extract_lod",
    label=NAME,
    summary="Takes one LOD out of a P3D geometry and outputs it as a LOD geometry that you can edit.",
    inputs=(1, 1),
    input_labels=("P3D geometry",),
    parms=(
        Parm("lod", "LOD", "string", "0",
             "Which LOD to extract: its position in the P3D geometry (0 is the first), or its name, "
             "for example Geometry or Resolution 1."),
    ),
)


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

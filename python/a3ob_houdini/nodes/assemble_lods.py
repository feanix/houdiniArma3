# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Assemble LODs node (ux-tools.md section 5.3): its parameter table and its cook.

Input 1 is the base P3D geometry (optional). Inputs 2 and up are LOD geometries. With ``replace`` on, an edited LOD
takes the place of the base LOD with the same stored ``a3ob_lod_signature`` (spec section 3.2, "Assemble LODs match
rule"; the rule itself is in ``lod_match``). Every other base LOD passes through as it is.
"""

from typing import Dict, List

from ..errors import UserError
from ..lod_match import match_lods
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS", "MAX_INPUTS"]

NAME = "Assemble LODs"

#: The most inputs the node takes: the base plus 31 LOD geometries. A real model has fewer than 30 LODs, and only
#: the edited ones need an input. Raising it is a new HDA text, not a code change (hda_source writes "Inputs: 0 to N").
MAX_INPUTS = 32

SPEC = NodeSpec(
    kind="assemble_lods",
    label=NAME,
    summary="Puts edited LODs back into a P3D geometry. Input 1 is the base; inputs 2 and up are LOD geometries.",
    inputs=(0, MAX_INPUTS),
    input_labels=("Base P3D geometry (optional)",) + tuple(
        "LOD geometry (input %d)" % number for number in range(2, MAX_INPUTS + 1)),
    input_help=("The original P3D geometry, or a single LOD geometry (it is then one more LOD, with no base). "
                "LODs you do not replace are kept from a P3D base.",
                "One LOD to put into the model. It replaces the base LOD of the same type and resolution."),
    parms=(
        Parm("replace", "Replace Matching LODs", "toggle", True,
             "A LOD input replaces the base LOD that has the same stored LOD signature and keeps its position. "
             "A LOD with no match is added at the end. Off: every LOD input is added at the end."),
    ),
)


def cook(node, report: Report) -> None:
    """Build the P3D geometry in the node's output."""
    import hou
    from .. import lod_container as lc

    values = read_parms(node, SPEC)
    inputs = [node.inputGeometry(number) for number in range(len(node.inputs()))]
    base = inputs[0] if inputs else None
    base_name = None
    first_lod = None
    if base is not None:
        if lc.classify(base) == lc.LOD:
            # Input 1 takes a base P3D or a single LOD. A single LOD there is one more LOD, with no base.
            first_lod, base = base, None
            inputs[0] = None
        else:
            _check_base(base, lc)
            base_name = node.inputs()[0].name()
    edited = _read_edited(inputs, lc)
    if first_lod is not None:
        edited.insert(0, (1, first_lod, lc.detail_int(first_lod, lc.SIGNATURE_ATTRIB), lc.lod_name_of(first_lod)))
    base_prims = base.prims() if base is not None else []
    base_content = [lc.lod_content(prim) for prim in base_prims]
    base_signatures = [lc.detail_int(content, lc.SIGNATURE_ATTRIB) for content in base_content]
    base_names = [lc.prim_name(prim, content) for prim, content in zip(base_prims, base_content)]
    match = match_lods(base_signatures, [item[2] for item in edited], bool(values["replace"]))

    out = node.geometry()
    out.clear()  # a Python SOP starts with a copy of input 1; start from nothing so the result is certain
    if base is not None:
        out.merge(base)
        lc.copy_detail_attributes(base, out)
    if out.findPrimAttrib("name") is None:
        out.addAttrib(hou.attribType.Prim, "name", "")
    prims = list(out.prims())
    for position in match.replaced:
        if not isinstance(prims[position], hou.PackedGeometry):
            raise UserError(format_problem(
                base_names[position], "base LOD %d is a %s prim, and only a PackedGeometry prim can be replaced" % (
                    position, prims[position].intrinsicValue("typename")),
                "Unpack it and pack it again with P3D Import, or turn Replace Matching LODs off"))
    for position, number in match.replaced.items():
        _put(prims[position], edited[number], reset_transform=True)
    for number in match.appended:
        _put(out.createPacked("PackedGeometry"), edited[number], reset_transform=False)

    _warn(report, match, edited, base_signatures, lc)
    sources = ["base prim %d" % position for position in range(len(base_prims))]
    for position, number in match.replaced.items():
        sources[position] = "input %d" % edited[number][0]
    sources.extend("input %d" % edited[number][0] for number in match.appended)
    _warn_clashes(report, [lc.lod_content(prim) for prim in out.prims()], sources, lc)
    head = "No base." if base_name is None else "Base: %s (%d LODs)." % (base_name, len(base_prims))
    report.info("%s %d replaced, %d passed through, %d added." % (
        head, len(match.replaced), len(base_prims) - len(match.replaced), len(match.appended)))


def _check_base(base, lc) -> None:
    # A LOD geometry in input 1 never gets here: cook() treats it as one more LOD.
    if lc.classify(base) == lc.OTHER:
        raise UserError(format_problem(
            None, "Input 1 is not a P3D geometry (packed LODs)", "Connect P3D Import or Assemble LODs"))


def _read_edited(inputs, lc) -> List[tuple]:
    """``[(input number from 1, geometry, signature or None, name)]`` for every connected LOD input."""
    edited = []
    for index in range(1, len(inputs)):
        geo = inputs[index]
        if geo is None:
            continue
        shape = lc.classify(geo)
        if shape == lc.P3D:
            raise UserError(format_problem(
                None, "Input %d is already a P3D geometry" % (index + 1), "Use it as the base input (input 1)"))
        if shape != lc.LOD:
            raise UserError(format_problem(
                None, "Input %d has no LOD type" % (index + 1), "Add a LOD Properties node"))
        edited.append((index + 1, geo, lc.detail_int(geo, lc.SIGNATURE_ATTRIB), lc.lod_name_of(geo)))
    return edited


def _put(prim, item, reset_transform: bool) -> None:
    """Make ``prim`` a packed prim that holds the LOD of ``item``, named by its type and resolution."""
    import hou
    prim.setEmbeddedGeometry(item[1])
    prim.setAttribValue("name", item[3])
    if reset_transform:  # the LOD geometry is already in world space
        prim.setTransform(hou.Matrix4(1))


def _warn_clashes(report: Report, contents, sources, lc) -> None:
    """Warn for every set of output LODs that share a type and resolution (the export's duplicate rule)."""
    from a3ob_core import p3d
    signatures, usable = [], []
    for number, content in enumerate(contents):
        try:
            signatures.append(p3d.encode_signature(
                lc.detail_int(content, lc.TYPE_ATTRIB),
                content.attribValue(lc.RESOLUTION_ATTRIB) if content.findGlobalAttrib(lc.RESOLUTION_ATTRIB) else 0.0))
        except (ValueError, TypeError):  # a type or resolution that cannot be encoded: Validate reports it
            continue
        usable.append(number)
    for group in p3d.signature_clashes(signatures):
        numbers = [usable[position] for position in group]
        report.warning(format_problem(
            lc.lod_name_of(contents[numbers[0]]),
            "%d LODs have the same type and resolution (%s)" % (
                len(numbers), ", ".join(sources[number] for number in numbers)),
            "P3D Export gives an error for this unless its Duplicate LODs is Skip (keeps the first one) or Ignore "
            "(writes all of them). Change the resolution of one of them in its LOD Properties"))


def _warn(report: Report, match, edited, base_signatures, lc) -> None:
    names_by_signature: Dict[int, str] = {}
    for item in edited:
        if item[2] is not None:
            names_by_signature.setdefault(item[2], item[3])
    for signature in match.ambiguous_base:
        positions = [number for number, value in enumerate(base_signatures) if value == signature]
        report.warning(format_problem(
            names_by_signature.get(signature), "the base has %d LODs with the same signature (%s)" % (
                len(positions), list_elements("prims", positions)),
            "The first one was replaced and the others were kept. Check the base geometry"))
    for signature in match.repeated_edited:
        inputs = [item[0] for item in edited if item[2] == signature]
        report.warning(format_problem(
            names_by_signature.get(signature), "%d LOD inputs have the same signature (%s)" % (
                len(inputs), list_elements("inputs", inputs)),
            "Each one replaced the next base LOD with that signature, or was added at the end. "
            "To add one as a new LOD, delete its a3ob_lod_signature detail attribute"))


BUTTONS = {}

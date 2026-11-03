# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The LOD Properties node (ux-tools.md section 5.4): LOD type, resolution, named properties and LOD copies.

It copies its input and writes only the LOD detail attributes of spec 3.3: ``a3ob_spec_version``, ``a3ob_lod_type``,
``a3ob_lod_resolution``, ``a3ob_lod_copy_types``, ``a3ob_lod_copy_resolutions``, ``a3ob_prop_names``,
``a3ob_prop_values`` and ``a3ob_prop_raw``. It also keeps a stored ``a3ob_lod_signature`` in step with the type and
resolution (spec 3.4, through ``a3ob_core``). It never touches ``a3ob_lod_flags``.
"""

import logging

from a3ob_core.p3d import lod_types

from .. import geo_edit, lod_props, names
from ..errors import UserError
from ..lod_props import Entry
from ..messages import Report, format_problem
from ..params import NodeSpec, Parm, read_parms

__all__ = ["SPEC", "cook", "BUTTONS"]

LOGGER = logging.getLogger("a3ob")
NAME = lod_props.NODE_NAME

_TYPE_HELP = "; ".join("%s: %s" % (info.display_name, info.description) for info in lod_types.LOD_TYPES.values())

SPEC = NodeSpec(
    kind="lod_properties",
    label="LOD Properties",
    summary="Sets the LOD type, resolution, named properties and LOD copies of a LOD geometry.",
    inputs=(1, 1),
    input_labels=("LOD geometry",),
    parms=(
        Parm("fromin", "Read from Input", "button", "",
             "Copy the type, resolution, properties and copies of the input into the parameters below. "
             "Use it right after Extract LOD.", action="fromin"),
        Parm("settype", "Set Type and Resolution", "toggle", True,
             "Off: leave the input's type and resolution alone and only edit properties and copies."),
        Parm("lodtype", "LOD Type", "menu", "visual", "The kind of LOD. " + _TYPE_HELP, items=lod_props.TYPE_ITEMS),
        Parm("resolution", "Resolution", "float", 1.0,
             "The resolution or index. Only types that carry one use it. For Unknown it is the signature value."),
        Parm("propmode", "Property Mode", "menu", "add",
             "Add to input: the rows below are added, and a row with the same name (any case) replaces the input's "
             "entry. Replace all: only the rows below remain. LOD copies follow the same mode.",
             items=(("add", "Add to Input"), ("replace", "Replace All"))),
        Parm("props", "Properties", "multiparm", 0, "Named properties of the LOD (at most 63 characters each).",
             children=(
                 Parm("prop_name#", "Name #", "string", ""),
                 Parm("prop_value#", "Value #", "string", ""),
             )),
        Parm("copies", "LOD Copies", "multiparm", 0,
             "The same mesh written again under another signature (A3OB's Copies).",
             children=(
                 Parm("copy_type#", "Copy Type #", "menu", "visual", "", items=lod_props.TYPE_ITEMS),
                 Parm("copy_res#", "Copy Resolution #", "float", 1.0),
             )),
    ),
)


def _stored_entries(geo):
    """The input's named properties as ``Entry`` objects (names, values and raw payloads are parallel)."""
    prop_names = geo_edit.read_detail(geo, "a3ob_prop_names", ())
    prop_values = geo_edit.read_detail(geo, "a3ob_prop_values", ())
    raw = geo_edit.read_detail(geo, "a3ob_prop_raw", ())
    return [Entry(name, value, raw[index] if index < len(raw) else "")
            for index, (name, value) in enumerate(zip(prop_names, prop_values))]


def _stored_copies(geo):
    types = geo_edit.read_detail(geo, "a3ob_lod_copy_types", ())
    resolutions = geo_edit.read_detail(geo, "a3ob_lod_copy_resolutions", ())
    return list(zip((int(value) for value in types), (float(value) for value in resolutions)))


def _write_view(geo, values, report: Report):
    """Type, resolution, spec version and the stored signature. Returns ``(type, resolution)`` of the LOD or None."""
    if not values["settype"]:
        if not geo_edit.has_detail(geo, "a3ob_lod_type"):
            return None
        return (lod_types.LodType(int(geo_edit.read_detail(geo, "a3ob_lod_type"))),
                float(geo_edit.read_detail(geo, "a3ob_lod_resolution", 0.0)))
    lod_type = lod_props.type_from_token(values["lodtype"])
    resolution = lod_props.view_resolution(lod_type, float(values["resolution"]))
    if resolution < 0 and lod_type != lod_types.LodType.UNKNOWN:
        raise UserError(format_problem(NAME, "the resolution %s is below 0" % resolution, "Use 0 or more"))
    stored = geo_edit.read_detail(geo, "a3ob_lod_signature")
    bits, changed = lod_props.signature_after_edit(None if stored is None else int(stored), lod_type, resolution)
    if not geo_edit.has_detail(geo, "a3ob_spec_version"):
        geo_edit.set_detail(geo, "a3ob_spec_version", 1)
    geo_edit.set_detail(geo, "a3ob_lod_type", int(lod_type))
    geo_edit.set_detail(geo, "a3ob_lod_resolution", float(resolution))
    if changed:
        geo_edit.set_detail(geo, "a3ob_lod_signature", bits)
        report.info("The stored signature no longer fitted the new type and resolution and was updated.")
    return lod_type, float(resolution)


def _write_properties(geo, values, report: Report) -> int:
    rows = [(row["prop_name"], row["prop_value"]) for row in values["props"]]
    replace = values["propmode"] == "replace"
    for name in lod_props.duplicate_names(rows):
        report.warning(format_problem(NAME, "the property %r appears in more than one row" % name,
                                      "Only one of them is needed"))
    existing = _stored_entries(geo)
    if not replace and not rows:
        return len(existing)
    entries = lod_props.merge_properties(existing, rows, replace, report)
    if not entries:
        for name in ("a3ob_prop_names", "a3ob_prop_values", "a3ob_prop_raw"):
            geo_edit.remove_detail(geo, name)
        return 0
    geo_edit.set_detail_array(geo, "a3ob_prop_names", "string", [entry.name for entry in entries])
    geo_edit.set_detail_array(geo, "a3ob_prop_values", "string", [entry.value for entry in entries])
    if any(entry.raw for entry in entries):
        geo_edit.set_detail_array(geo, "a3ob_prop_raw", "string", [entry.raw for entry in entries])
    else:
        geo_edit.remove_detail(geo, "a3ob_prop_raw")
    return len(entries)


def _write_copies(geo, values, view, report: Report) -> int:
    rows = []
    for number, row in enumerate(values["copies"], start=1):
        lod_type = lod_props.type_from_token(row["copy_type"])
        resolution = lod_props.view_resolution(lod_type, float(row["copy_res"]))
        lod_props.encode_view(lod_type, resolution, "Copy %d: " % number)
        rows.append((int(lod_type), resolution))
    replace = values["propmode"] == "replace"
    existing = _stored_copies(geo)
    if not replace and not rows:
        return len(existing)
    copies = lod_props.merge_copies(existing, rows, replace)
    if view is not None:
        own = lod_props.encode_view(view[0], view[1])
        for number, (lod_type, resolution) in enumerate(copies, start=1):
            if lod_props.encode_view(lod_type, resolution) == own:
                report.warning(format_problem(
                    NAME, "copy %d has the same signature as the LOD itself" % number,
                    "Validate and Export will refuse the duplicate. Change or remove the copy"))
    if not copies:
        geo_edit.remove_detail(geo, "a3ob_lod_copy_types")
        geo_edit.remove_detail(geo, "a3ob_lod_copy_resolutions")
        return 0
    geo_edit.set_detail_array(geo, "a3ob_lod_copy_types", "int", [lod_type for lod_type, _ in copies])
    geo_edit.set_detail_array(geo, "a3ob_lod_copy_resolutions", "float", [res for _, res in copies])
    return len(copies)


def cook(node, report: Report) -> None:
    """Copy the input and set the LOD detail attributes."""
    values = read_parms(node, SPEC)
    geo = geo_edit.copy_input(node, NAME)
    view = _write_view(geo, values, report)
    property_count = _write_properties(geo, values, report)
    copy_count = _write_copies(geo, values, view, report)
    if view is None:
        head = "No LOD type"
    else:
        head = lod_types.format_lod_name(view[0], view[1])
        stored = geo_edit.read_detail(geo, "a3ob_lod_signature")
        if stored is not None:
            head += ", signature %s" % ("%.7g" % names.bits_float(int(stored)))
    report.info("%s, %d %s, %d %s." % (head, property_count, "property" if property_count == 1 else "properties",
                                       copy_count, "copy" if copy_count == 1 else "copies"))


def fromin(kwargs) -> None:
    """The Read from Input button: copy the input's LOD data into the parameters."""
    node = kwargs["node"]
    inputs = node.inputs()
    source = inputs[0].geometry() if inputs and inputs[0] is not None else None
    if source is None:
        LOGGER.warning("%s: Read from Input needs a connected input.", node.path())
        return
    if geo_edit.has_detail(source, "a3ob_lod_type"):
        lod_type = lod_types.LodType(int(geo_edit.read_detail(source, "a3ob_lod_type")))
        node.parm("lodtype").set(lod_props.type_token(lod_type))
        node.parm("resolution").set(lod_props.short_f32(float(geo_edit.read_detail(source, "a3ob_lod_resolution", 0.0))))
    entries = _stored_entries(source)
    copies = _stored_copies(source)
    node.parm("props").set(len(entries))
    for number, entry in enumerate(entries, start=1):
        node.parm("prop_name%d" % number).set(entry.name)
        node.parm("prop_value%d" % number).set(entry.value)
    node.parm("copies").set(len(copies))
    for number, (lod_type, resolution) in enumerate(copies, start=1):
        node.parm("copy_type%d" % number).set(lod_props.type_token(lod_type))
        node.parm("copy_res%d" % number).set(lod_props.short_f32(resolution))


BUTTONS = {"fromin": fromin}

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Rig Skeleton node (docs/design/character-rigging.md 6.2, task #61): a ``model.cfg`` skeleton and an editable
pivots P3D become a KineFX skeleton.

The work is in ``a3ob_core.rig`` (no Houdini). This module reads the parameters, writes the points and says what
happened. A KineFX skeleton is points with a ``name`` string and a ``transform`` 3x3 matrix (the position is ``P``),
joined by 2-point polylines that run from the parent to the child (Houdini help, "Working with skeletons").
"""

import os

from a3ob_core.rig import (CfgError, check_bones, plan_skeleton, read_cfg_file, read_pivots_file, resolve_skeleton,
                           skeleton_names)

from .. import settings as settings_module
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "created", "MENUS", "BUTTONS"]

NAME = "Rig Skeleton"
#: Detail int, 1 or 0: the ``isDiscrete`` of the chosen skeleton class. Not written when the model.cfg does not say.
DISCRETE_ATTRIB = "a3ob_rig_discrete"

SPEC = NodeSpec(
    kind="rig_skeleton",
    label="Rig Skeleton",
    summary="Builds the Arma 3 character skeleton as a KineFX skeleton, from a model.cfg and a pivots .p3d file.",
    inputs=(0, 0),
    on_created=True,
    parms=(
        Parm("cfg", "Model Config", "file", "",
             "The model.cfg that holds the skeleton. This node reads exactly this one file. Arma also reads a "
             "<foldername>.cfg (which wins over model.cfg) and a <modelname>.cfg next to the model; if your skeleton "
             "is in one of those, pick that file. #include lines are not followed.",
             file_pattern="*.cfg"),
        Parm("skeleton", "Skeleton", "menu", "",
             "Which skeleton class of the file to build (the classes in CfgSkeletons, with their bone count). Empty "
             "means the first one that has bones. A class that inherits (skeletonInherit) gets the bones of the class it inherits "
             "from first.", menu_action="skeleton_choices"),
        Parm("pivots", "Pivots File", "file", "",
             "An editable pivots .p3d: its Memory LOD has one named selection per bone, and the first point of a "
             "selection is that bone's pivot. The game file named by pivotsModel in the model.cfg is packed and can't "
             "be read, so you point at the editable one here (the Samples have ManSkeleton_Pivots.p3d).",
             file_pattern="*.p3d"),
    ),
)

_SET_HINT = ("Pick the file here, or set the Arma 3 Samples folder with the Project root and presets shelf tool and "
             "make a new Rig Skeleton node, which then starts from the Samples files")


def created(node, settings=None) -> None:
    """A new node starts from the Samples files when the Samples folder is known (D37 Q1). Nothing is set otherwise."""
    if settings is None:
        settings, _ = settings_module.load()
    cfg, pivots = settings_module.samples_defaults(settings)
    if cfg:
        node.parm("cfg").set(cfg)
    if pivots:
        node.parm("pivots").set(pivots)


def _need_file(text: str, label: str, what: str) -> str:
    text = text.strip()
    if not text:
        raise UserError(format_problem(NAME, "no %s is chosen in %s" % (what, label), _SET_HINT))
    if not os.path.isfile(text):
        raise UserError(format_problem(NAME, "the file %s does not exist" % text, "Check %s" % label))
    return text


def _read_cfg(path: str):
    try:
        return read_cfg_file(path)
    except CfgError as ex:
        raise UserError(format_problem(NAME, "%s cannot be read: %s" % (os.path.basename(path), ex))) from None


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    cfg_path = _need_file(values["cfg"], "Model Config", "model.cfg")
    pivots_path = _need_file(values["pivots"], "Pivots File", "pivots .p3d file")
    cfg = _read_cfg(cfg_path)
    if cfg.includes:
        report.warning(format_problem(NAME, "%d #include line%s in the model.cfg %s not followed (%s)" % (
            len(cfg.includes), "" if len(cfg.includes) == 1 else "s", "was" if len(cfg.includes) == 1 else "were",
            ", ".join(cfg.includes[:3])), "Bones that live in an included file are missing; copy them into this file"))
    names = skeleton_names(cfg)
    if not names:
        raise UserError(format_problem(NAME, "%s has no skeleton class in CfgSkeletons" % os.path.basename(cfg_path),
                                       "Pick the model.cfg that has class CfgSkeletons"))
    chosen = values["skeleton"].strip() or _first_with_bones(cfg, names)
    try:
        skeleton = resolve_skeleton(cfg, chosen)
    except CfgError as ex:
        raise UserError(format_problem(NAME, str(ex), "Pick a skeleton in the Skeleton menu")) from None
    problems = check_bones(skeleton.bones)
    if problems:
        raise UserError(format_problem(NAME, "the skeleton %s has %d problem%s: %s" % (
            skeleton.name, len(problems), "" if len(problems) == 1 else "s", "; ".join(problems[:3])
            + ("; and %d more" % (len(problems) - 3) if len(problems) > 3 else "")), "Fix the bone list in the model.cfg"))
    try:
        pivots = read_pivots_file(pivots_path)
    except CfgError as ex:
        raise UserError(format_problem(NAME, str(ex), "Pick an editable pivots .p3d (MLOD)")) from None
    plan = plan_skeleton(skeleton.bones, pivots)
    if not plan.joints:
        raise UserError(format_problem(NAME, "no bone of %s has a pivot in %s" % (skeleton.name, os.path.basename(pivots_path)),
                                       "The pivots file needs one selection per bone in its Memory LOD, named like the bones"))
    _write_skeleton(node.geometry(), plan)
    if skeleton.is_discrete is not None:  # Weight Cleanup reads it (character-rigging.md 4.1, task #62)
        geo = node.geometry()
        geo.addAttrib(hou.attribType.Global, DISCRETE_ATTRIB, 0)
        geo.setGlobalAttribValue(DISCRETE_ATTRIB, 1 if skeleton.is_discrete else 0)
    for text in _messages(skeleton, plan):
        report.info(text)
    if plan.reparented:
        report.warning(format_problem(NAME, "%s had a parent that is left out, so %s hang%s from the nearest joint above" % (
            list_elements("joints", plan.reparented), "they" if len(plan.reparented) > 1 else "it",
            "" if len(plan.reparented) > 1 else "s")))
    if skeleton.pivots_model:
        report.info("The model.cfg names the game pivots file %s (packed, not read). The pivots come from %s." % (
            skeleton.pivots_model, os.path.basename(pivots_path)))


def _first_with_bones(cfg, names) -> str:
    """The first skeleton class that has bones (the Samples file starts with an empty ``Default`` class)."""
    for name in names:
        try:
            if resolve_skeleton(cfg, name).bones:
                return name
        except CfgError:
            continue
    return names[0]


def _messages(skeleton, plan):
    """The info lines: the counts first, then the names of what was left out."""
    lines = ["%d joints from the %d bones of %s. Left out: %d face and eye bones (Face_*, Eye*), %d with no pivot."
             " %d pivot%s ignored." % (len(plan.joints), plan.bone_count, skeleton.name, len(plan.skipped),
                                       len(plan.missing), len(plan.unknown), "" if len(plan.unknown) == 1 else "s")]
    if plan.missing:
        lines.append("No pivot for %s. They are left out of the skeleton." % list_elements("bones", plan.missing))
    if plan.unknown:
        lines.append("The pivots file has %s that are not bones of this skeleton. They are ignored."
                     % list_elements("selections", plan.unknown))
    if skeleton.dropped:
        lines.append("%s repeat a bone of an inherited skeleton and were dropped."
                     % list_elements("Bones", skeleton.dropped))
    return lines


def _write_skeleton(geo, plan) -> None:
    """Points with ``name`` and ``transform`` (identity), and one polyline from each parent to its child. The pivots
    are in file axes; Houdini's are ``(x, y, -z)`` (houdini-data-model.md section 4.3), like the imported mesh."""
    geo.clear()
    geo.addAttrib(hou.attribType.Point, "name", "")
    geo.addAttrib(hou.attribType.Point, "transform", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    points = geo.createPoints([(joint.position[0], joint.position[1], -joint.position[2]) for joint in plan.joints])
    geo.setPointStringAttribValues("name", [joint.name for joint in plan.joints])
    for joint, point in zip(plan.joints, points):
        if joint.parent >= 0:
            line = geo.createPolygon(False)  # open: a polyline
            line.addVertex(points[joint.parent])
            line.addVertex(point)


def _menu_skeletons(kwargs) -> list:
    """The Skeleton menu: the classes of the chosen model.cfg with their bone counts. No file gives an empty menu."""
    path = kwargs["node"].parm("cfg").eval().strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        cfg = read_cfg_file(path)
        items = []
        for name in skeleton_names(cfg):
            try:
                label = "%s (%d bones)" % (name, len(resolve_skeleton(cfg, name).bones))
            except CfgError:
                label = "%s (cannot be read)" % name
            items.append((name, label))
        return items
    except CfgError:
        return []


BUTTONS = {}
MENUS = {"skeleton_choices": _menu_skeletons}

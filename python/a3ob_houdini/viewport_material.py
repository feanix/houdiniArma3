# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB). New file, not derived from A3OB.
"""Viewport materials for the Material Assign node (task #54, D29).

``resolve`` (no ``hou``) turns a texture line into the picture or colour the viewport can show. ``apply`` (needs ``hou``)
makes one Houdini material per distinct picture or colour in a material network named ``a3ob_viewport_materials`` inside
the same Geometry object, reuses it on every later cook, and sets ``shop_materialpath`` on the polygons. The P3D export
ignores ``shop_materialpath``. Materials nobody uses any more stay in the network; they are small and harmless.

The material is a ``principledshader::2.0``: it reads the point or vertex attribute ``uv`` and shows in Houdini's default
viewport. The picture needs no V flip: the importer stores ``v' = 1 - v`` (spec F7), the file's V runs top-down, and
Houdini puts the top row of a picture at ``v = 1``; the decoded PNG is top-row-first (checked in the tests).
"""

import hashlib
import ntpath
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .messages import Report, format_problem

__all__ = ["MATNET_NAME", "IMAGE_EXTENSIONS", "Shown", "resolve", "apply"]

MATNET_NAME = "a3ob_viewport_materials"
#: Pictures Houdini reads, used as they are.
IMAGE_EXTENSIONS = (".png", ".tga", ".tif", ".tiff", ".jpg", ".jpeg", ".exr")
#: Pictures looked for next to a .paa, in this order (D29).
SIBLINGS = (".png", ".tga", ".tif")

_COLOR = re.compile(r"color\(\s*([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)\s*,\s*[-0-9.eE+]+", re.IGNORECASE)

# Why a line has no material.
EMPTY, CUSTOM, INGAME, MISSING, FAILED = "empty", "custom", "ingame", "missing", "failed"


@dataclass(frozen=True)
class Shown:
    """The result for one texture line: ``kind`` is ``image``, ``color`` or one of the reasons above."""
    kind: str
    path: str = ""
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    text: str = ""
    #: The texture line as written; the material is named after its file stem.
    label: str = ""


def _is_ingame(path: str) -> bool:
    """No drive letter and not a network path: a path as imported models have it."""
    drive, rest = ntpath.splitdrive(path)
    return not drive and not rest.startswith(("\\", "/"))


def _find(path: str, root: str) -> Optional[str]:
    """The file for a texture path: itself when it has a drive, else under the project root."""
    candidate = path if not _is_ingame(path) else ntpath.join(root, path)
    return candidate if os.path.isfile(candidate) else None


def resolve(text: str, project_root: str, cache_folder: Optional[str] = None) -> Shown:
    """What the viewport can show for texture line ``text``. Never raises."""
    from a3ob_core.paa import PaaError
    from . import paa_cache
    line = text.strip()
    if not line:
        return Shown(EMPTY, text=text)
    if line.startswith("#"):
        found = _COLOR.search(line)
        if found is None:
            return Shown(CUSTOM, text=text)
        try:
            rgb = tuple(min(1.0, max(0.0, float(found.group(i)))) for i in (1, 2, 3))
        except ValueError:
            return Shown(CUSTOM, text=text)
        return Shown("color", color=rgb, text=text)
    extension = ntpath.splitext(line)[1].lower()
    if extension not in IMAGE_EXTENSIONS and extension != ".paa":
        return Shown(CUSTOM, text=text)
    found_path = _find(line, project_root)
    if extension == ".paa":
        stem = ntpath.splitext(ntpath.join(project_root, line) if _is_ingame(line) else line)[0]
        for ending in SIBLINGS:
            if os.path.isfile(stem + ending):
                return Shown("image", path=stem + ending, text=text, label=line)
    if found_path is None:
        return Shown(INGAME if _is_ingame(line) else MISSING, path=line, text=text)
    if extension == ".paa":
        try:
            return Shown("image", path=paa_cache.cached_image(found_path, cache_folder), text=text,
                         label=line)
        except PaaError as ex:
            return Shown(FAILED, path=found_path, text=str(ex))
    return Shown("image", path=found_path, text=text)


def _material_name(shown: Shown) -> str:
    """Stable node name from the texture file name (or the colour)."""
    if shown.kind == "color":
        return "color_%02x%02x%02x" % tuple(round(channel * 255) for channel in shown.color)
    stem = ntpath.splitext(ntpath.basename((shown.label or shown.path).replace("/", "\\")))[0]
    return re.sub(r"[^A-Za-z0-9_]", "_", stem) or "texture"


def _key(shown: Shown) -> str:
    if shown.kind == "color":
        return "color:%.4f,%.4f,%.4f" % shown.color
    return "image:" + os.path.normcase(os.path.abspath(shown.path))


def _geometry_object(node):
    """The Geometry object the node is in (the node's own network when it is not inside one)."""
    import hou
    parent = node.parent()
    walker = parent
    while walker is not None and walker.type().category() != hou.objNodeTypeCategory():
        walker = walker.parent()
    return walker if walker is not None and walker.type().name() == "geo" else parent


def _matnet(owner):
    import hou
    net = owner.node(MATNET_NAME)
    if net is None:
        net = owner.createNode("matnet", MATNET_NAME)
        net.moveToGoodPosition()
    return net


def _material_for(net, shown: Shown):
    """The material of this picture or colour: found by its key, else made."""
    import hou
    key = _key(shown)
    base = _material_name(shown)
    for child in net.children():
        if child.userData("a3ob_key") == key:
            if shown.kind != "color":  # heal materials made with the old 0.2 tint
                child.parmTuple("basecolor").set((1.0, 1.0, 1.0))
            return child
    name = base
    taken = {child.name() for child in net.children()}
    if name in taken:  # same file name in another folder
        name = "%s_%s" % (base, hashlib.sha1(key.encode("utf-8")).hexdigest()[:6])
    material = net.createNode("principledshader::2.0", name)
    material.setUserData("a3ob_key", key)
    material.parm("basecolor_usePointColor").set(0)
    if shown.kind == "color":
        material.parmTuple("basecolor").set(shown.color)
    else:
        material.parmTuple("basecolor").set((1.0, 1.0, 1.0))  # the default 0.2 would tint the texture dark
        material.parm("basecolor_useTexture").set(1)
        material.parm("basecolor_texture").set(shown.path.replace("\\", "/"))
    return material


def apply(node, geo, numbers: List[int], report: Report, project_root: str, cache_folder: Optional[str] = None) -> None:
    """Give the polygons ``numbers`` of ``geo`` the viewport material of their ``a3ob_texture``."""
    import hou
    if geo.findPrimAttrib("a3ob_texture") is None:
        return
    textures = geo.primStringAttribValues("a3ob_texture")
    by_text: Dict[str, List[int]] = {}
    for number in numbers:
        by_text.setdefault(textures[number], []).append(number)
    results = {text: resolve(text, project_root, cache_folder) for text in by_text}
    ingame = sorted(text for text, shown in results.items() if shown.kind == INGAME)
    custom = [text for text, shown in results.items() if shown.kind == CUSTOM]
    for text, shown in results.items():
        if shown.kind == MISSING:
            report.warning(format_problem("Material Assign", "the texture file %s was not found, so its polygons show "
                                          "no viewport material" % shown.path, "Check the path"))
        elif shown.kind == FAILED:
            report.warning(format_problem("Material Assign", "the texture file %s cannot be shown (%s)" % (
                shown.path, shown.text), "Make a .png next to it, or check the file"))
    if ingame:
        report.info("%d texture%s with an in-game path (for example %s) %s not found under the project root, so "
                    "they show no viewport material. Showing in-game paths waits for path mapping." % (
                        len(ingame), "" if len(ingame) == 1 else "s", ingame[0], "was" if len(ingame) == 1 else "were"))
    if custom:
        report.info("%d texture line%s that %s not a file or a plain colour show no viewport material." % (
            len(custom), "" if len(custom) == 1 else "s", "is" if len(custom) == 1 else "are"))
    shown_now = {text: shown for text, shown in results.items() if shown.kind in ("image", "color")}
    if not shown_now:
        return
    try:
        net = _matnet(_geometry_object(node))
        paths = {text: _material_for(net, shown).path() for text, shown in shown_now.items()}
    except hou.Error as ex:
        report.warning(format_problem("Material Assign", "the viewport materials could not be made (%s)" % ex,
                                      "Turn off Show in Viewport, or unlock the network"))
        return
    if geo.findPrimAttrib("shop_materialpath") is None:
        geo.addAttrib(hou.attribType.Prim, "shop_materialpath", "")
    values = list(geo.primStringAttribValues("shop_materialpath"))
    for text, path in paths.items():
        for number in by_text[text]:
            values[number] = path
    geo.setPrimStringAttribValues("shop_materialpath", values)

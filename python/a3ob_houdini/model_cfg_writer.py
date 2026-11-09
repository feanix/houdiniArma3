# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The text work of the Model Cfg node (task #95): the ``model.cfg`` a character P3D needs, new or updated in place.
No ``hou`` here.

``plan`` takes the text of the existing file (or ``None``) and returns the text to write and a list of what changes.
A new file holds ``CfgSkeletons`` (``Default`` and the man skeleton with its bones and parents, from ``rig_bones``) and
``CfgModels`` (``Default``, the base class, one class per model name). An existing file is edited by text position:
only the classes that are missing are inserted and only the ``CfgModels`` class of each model name is replaced; every
other character stays as it is (comments, other classes, line endings, a byte order mark).

The file is read with ``a3ob_core.rig.model_cfg.parse_cfg`` first, so a file that parser cannot read is refused with
the line number. The positions come from a small scanner here, because that parser keeps no positions.

How the sample file looks is Bohemia's; nothing of its text is copied. The class names, the bone data and the
``sections[]`` names are facts of the game's config; the layout and the settings values follow the same game rules.
"""

import dataclasses
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from a3ob_core.p3d import proxy as proxy_names
from a3ob_core.rig.model_cfg import CfgClass, CfgError, CfgFile, parse_cfg, resolve_skeleton, skeleton_names

from .p3d_export.rig_bones import OFP2_MANSKELETON_PARENTS, bone_name_set

__all__ = ["MAN_SECTIONS", "PIVOTS_MODEL", "Plan", "parse_model_names", "plan", "section_candidates",
           "merge_sections", "case_warnings", "unknown_sections", "find_classes", "ClassSpan"]

#: The ``sections[]`` of ``ArmaMan``: the selections a character config can retexture or hide.
MAN_SECTIONS = (
    "osobnost", "Head_Injury", "Body_Injury", "l_leg_injury", "l_arm_injury", "r_arm_injury", "r_leg_injury",
    "injury_body", "injury_legs", "injury_hands", "clan", "clan_sign", "Camo", "CamoB", "Camo1", "Camo2",
    "personality", "hl", "injury_head", "insignia", "ghillie_hide",
)
PIVOTS_MODEL = "A3\\anims_f\\data\\skeleton\\SkeletonPivots.p3d"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAB = "\t"


@dataclasses.dataclass
class Plan:
    """What the Model Cfg node would do."""
    #: The whole file as it would be after the write. Equals the old text when nothing changes.
    text: str
    #: One line per change, for the node's message ("added class X").
    changes: List[str] = dataclasses.field(default_factory=list)
    warnings: List[str] = dataclasses.field(default_factory=list)
    infos: List[str] = dataclasses.field(default_factory=list)
    #: True when there was no file.
    created: bool = False
    #: True when ``text`` differs from the old file.
    changed: bool = False


# ---------------------------------------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------------------------------------


def parse_model_names(text: str) -> List[str]:
    """The model names of the Model Names field: separated by spaces, commas or semicolons; ``.p3d`` is cut off (any
    case); a path keeps only its file name. Repeats (any case) are dropped. Raises ``ValueError`` for a name that is not a
    config class name (letters, digits and ``_``, not starting with a digit)."""
    names: List[str] = []
    for item in re.split(r"[\s,;]+", text.strip()):
        if not item:
            continue
        item = item.replace("\\", "/").rsplit("/", 1)[-1]
        if item.lower().endswith(".p3d"):
            item = item[:-4]
        if not _IDENT.match(item):
            raise ValueError("%r cannot be a class name in a model.cfg. Use letters, digits and _, and do not start with "
                             "a digit" % item)
        if item.lower() not in [name.lower() for name in names]:
            names.append(item)
    return names


def _need_ident(value: str, what: str) -> str:
    value = value.strip()
    if not _IDENT.match(value):
        raise ValueError("%s %r cannot be a class name in a model.cfg" % (what, value))
    return value


def merge_sections(*groups: Iterable[str]) -> List[str]:
    """The names of all groups in order, once each (any case), empty names left out."""
    seen = set()
    merged: List[str] = []
    for group in groups:
        for name in group:
            name = name.strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                merged.append(name)
    return merged


def parse_sections(text: str) -> List[str]:
    """The Extra Sections field: names separated by spaces, commas or semicolons."""
    return merge_sections(re.split(r"[\s,;]+", text.strip()))


def section_candidates(names: Iterable[str]) -> List[str]:
    """Selection names of the input that can be sections: not bones (any case), proxies, ``a3ob_*`` helpers or Houdini's
    internal ``__`` groups. Sorted, once each (any case)."""
    bones = bone_name_set()
    found: Dict[str, str] = {}
    for name in names:
        if (not name or name.startswith("__") or name.lower().startswith("a3ob_") or proxy_names.is_proxy_name(name)
                or name.lower() in bones):
            continue
        found.setdefault(name.lower(), name)
    return sorted(found.values(), key=str.lower)


def unknown_sections(wanted: Iterable[str], selections: Iterable[str]) -> List[str]:
    """The names in ``wanted`` that are not a selection of the input (any case)."""
    have = {name.lower() for name in selections}
    return [name for name in wanted if name.lower() not in have]


def case_warnings(folder: str, models: Iterable[str]) -> List[str]:
    """A model name that matches a ``.p3d`` file of the folder only when case is ignored: the game takes the class name
    from the file name and compares it without case, but a build tool or another OS may not."""
    try:
        files = [name for name in os.listdir(folder) if name.lower().endswith(".p3d")]
    except OSError:
        return []
    warnings = []
    for model in models:
        if (model + ".p3d") in files:
            continue
        for name in files:
            if name[:-4].lower() == model.lower():
                warnings.append("The model name %s is written %s in the file name %s. Use the same capitalisation, or the "
                                "game may not find the class" % (model, name[:-4], name))
    return warnings


# ---------------------------------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------------------------------


def _indent(lines: List[str], depth: int) -> List[str]:
    return [(_TAB * depth + line) if line else line for line in lines]


def _quoted(names: Iterable[str]) -> str:
    return ", ".join('"%s"' % name for name in names)


def _wrapped(names: List[str], per_line: int = 6) -> List[str]:
    rows = [names[i:i + per_line] for i in range(0, len(names), per_line)]
    return [_quoted(row) + ("," if index < len(rows) - 1 else "") for index, row in enumerate(rows)]


def _default_skeleton() -> List[str]:
    return ["class Default", "{", _TAB + "isDiscrete = 1;", _TAB + 'skeletonInherit = "";',
            _TAB + "skeletonBones[] = {};", "};"]


def _man_skeleton(name: str) -> List[str]:
    lines = ["class %s" % name, "{", _TAB + "isDiscrete = 0;", _TAB + 'skeletonInherit = "";',
             _TAB + "skeletonBones[] =", _TAB + "{"]
    count = len(OFP2_MANSKELETON_PARENTS)
    for index, (bone, parent) in enumerate(OFP2_MANSKELETON_PARENTS):
        lines.append('%s"%s","%s"%s' % (_TAB * 2, bone, parent, "," if index < count - 1 else ""))
    lines += [_TAB + "};", _TAB + 'pivotsModel = "%s";' % PIVOTS_MODEL, "};"]
    return lines


def _default_model() -> List[str]:
    return ["class Default", "{", _TAB + 'sectionsInherit = "";', _TAB + "sections[] = {};",
            _TAB + 'skeletonName = "";', "};"]


def _base_model(base: str, skeleton: str, sections: List[str]) -> List[str]:
    lines = ["class %s: Default" % base, "{",
             _TAB + "htMin = 60;", _TAB + "htMax = 1800;", _TAB + "afMax = 30;", _TAB + "mfMax = 0;", _TAB + "mFact = 1;",
             _TAB + "tBody = 37;", "", _TAB + "sections[] =", _TAB + "{"]
    lines += _indent(_indent(_wrapped(sections), 1), 1)
    lines += [_TAB + "};", _TAB + 'skeletonName = "%s";' % skeleton, "};"]
    return lines


def _model_class(name: str, base: str, own_sections: Optional[List[str]]) -> List[str]:
    """``class X: ArmaMan {};``, or with a full ``sections[]`` when the base class does not hold all the names."""
    if not own_sections:
        return ["class %s: %s {};" % (name, base)]
    lines = ["class %s: %s" % (name, base), "{", _TAB + "sections[] =", _TAB + "{"]
    lines += _indent(_indent(_wrapped(own_sections), 1), 1)
    lines += [_TAB + "};", "};"]
    return lines


def _block(name: str, parts: List[List[str]]) -> List[str]:
    lines = ["class %s" % name, "{"]
    for index, part in enumerate(parts):
        if index:
            lines.append("")
        lines += _indent(part, 1)
    lines.append("};")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# Scanner: where the classes are
# ---------------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class ClassSpan:
    name: str
    base: Optional[str]
    #: Position of the word ``class``, and the position after the ``;`` that ends the class (or after the ``}``).
    start: int
    end: int
    #: Positions of ``{`` and ``}``; -1 for ``class A;``.
    open: int
    close: int
    #: Token index of ``{`` and ``}``.
    tok_open: int = -1
    tok_close: int = -1


def _tokens(text: str) -> List[Tuple[str, str, int, int]]:
    """``(kind, value, start, end)``; comments and ``#`` lines are skipped, as in ``model_cfg._tokenize``."""
    out: List[Tuple[str, str, int, int]] = []
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            line_start = True
        elif c in " \t\r\f\v":
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            line_start = False
        elif c == "#" and line_start:
            while i < n and text[i] != "\n":
                if text[i] == "\\" and i + 1 < n and text[i + 1] == "\n":
                    i += 1
                i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != "\n":
                if text[j] == '"':
                    if text.startswith('""', j):
                        j += 2
                        continue
                    break
                j += 1
            out.append(("str", text[i + 1:j], i, j + 1))
            i = j + 1
            line_start = False
        elif text.startswith("+=", i):
            out.append(("punct", "+=", i, i + 2))
            i += 2
            line_start = False
        elif c in "{}[];=:,":
            out.append(("punct", c, i, i + 1))
            i += 1
            line_start = False
        else:
            j = i
            while j < n and text[j] not in " \t\r\f\v\n{}[];=:,\"" and not text.startswith(("//", "/*"), j):
                j += 1
            out.append(("word", text[i:j], i, j))
            i = j
            line_start = False
    return out


def _match(tokens, index: int) -> int:
    """The token index of the ``}`` that closes the ``{`` at ``index``."""
    depth = 0
    for k in range(index, len(tokens)):
        if tokens[k][0] == "punct":
            if tokens[k][1] == "{":
                depth += 1
            elif tokens[k][1] == "}":
                depth -= 1
                if depth == 0:
                    return k
    raise CfgError("a { is never closed")


def _find(tokens, lo: int, hi: int) -> List[ClassSpan]:
    spans: List[ClassSpan] = []
    i = lo
    while i < hi:
        kind, value, start, _ = tokens[i]
        if kind == "word" and value.lower() == "class" and i + 1 < hi and tokens[i + 1][0] == "word":
            name = tokens[i + 1][1]
            j = i + 2
            base = None
            if j < hi and tokens[j][1] == ":" and tokens[j][0] == "punct":
                j += 1
                while j < hi and tokens[j][0] == "word" and tokens[j][1].lower() in ("public", "protected", "private"):
                    j += 1
                if j < hi and tokens[j][0] == "word":
                    base = tokens[j][1]
                    j += 1
            if j < hi and tokens[j][1] == ";" and tokens[j][0] == "punct":
                spans.append(ClassSpan(name, base, start, tokens[j][3], -1, -1))
                i = j + 1
                continue
            if j < hi and tokens[j][1] == "{" and tokens[j][0] == "punct":
                close = _match(tokens, j)
                end = tokens[close][3]
                k = close + 1
                if k < hi and tokens[k][0] == "punct" and tokens[k][1] == ";":
                    end = tokens[k][3]
                    k += 1
                spans.append(ClassSpan(name, base, start, end, tokens[j][2], tokens[close][2], j, close))
                i = k
                continue
        if kind == "punct" and value == "{":  # an array value or another braced thing: not a class
            i = _match(tokens, i) + 1
            continue
        i += 1
    return spans


def find_classes(text: str) -> List[ClassSpan]:
    """The top-level classes of a config text with their positions."""
    tokens = _tokens(text)
    return _find(tokens, 0, len(tokens))


def _children(text: str, span: ClassSpan) -> List[ClassSpan]:
    tokens = _tokens(text)
    first = next(i for i, token in enumerate(tokens) if token[2] == span.open)
    return _find(tokens, first + 1, _match(tokens, first))


# ---------------------------------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------------------------------


def _class_named(cfg: CfgFile, block: str, name: str) -> Optional[CfgClass]:
    """A defined (not reference) class ``name`` in any ``block`` of the file, else None."""
    for item in cfg.root.classes:
        if item.name.lower() == block.lower():
            for child in item.classes:
                if child.name.lower() == name.lower() and not child.is_reference:
                    return child
    return None


def _inherited_sections(cfg: CfgFile, name: str) -> List[str]:
    """The ``sections[]`` of a CfgModels class, from the nearest class that has one."""
    seen = set()
    item = _class_named(cfg, "CfgModels", name)
    while item is not None and item.name.lower() not in seen:
        seen.add(item.name.lower())
        value = item.prop("sections")
        if isinstance(value, list):
            return [str(entry) for entry in value]
        item = _class_named(cfg, "CfgModels", item.base) if item.base else None
    return []


def plan(existing: Optional[str], models: List[str], base: str = "ArmaMan", skeleton: str = "OFP2_ManSkeleton",
         extra_sections: Iterable[str] = ()) -> Plan:
    """The text of the model.cfg after the write. ``existing`` is the text of the file now, or None when there is none.

    Raises ``CfgError`` (naming the line) when ``existing`` cannot be read, ``ValueError`` for a name that is not a
    class name or when no model name is given."""
    base = _need_ident(base, "The base class")
    skeleton = _need_ident(skeleton, "The skeleton")
    if not models:
        raise ValueError("no model name is set")
    extras = merge_sections(extra_sections)
    if existing is None:
        return _plan_new(models, base, skeleton, extras)
    return _plan_update(existing, models, base, skeleton, extras)


def _skeleton_note(name: str) -> str:
    return "class %s (%d bones)" % (name, len(OFP2_MANSKELETON_PARENTS))


def _plan_new(models: List[str], base: str, skeleton: str, extras: List[str]) -> Plan:
    sections = merge_sections(MAN_SECTIONS, extras)
    lines = _block("CfgSkeletons", [_default_skeleton(), _man_skeleton(skeleton)]) + [""]
    lines += _block("CfgModels", [_default_model(), _base_model(base, skeleton, sections),
                                  [line for name in models for line in _model_class(name, base, None)]])
    result = Plan("\n".join(lines) + "\n", created=True, changed=True)
    result.changes.append("created model.cfg: CfgSkeletons (Default, %s) and CfgModels (Default, %s, %s)"
                          % (_skeleton_note(skeleton), base, ", ".join(models)))
    return result


def _block_parts(nl: str, parts: List[List[str]], depth: int = 1, blank_first: bool = False) -> str:
    """Parts (lists of lines) indented and separated by blank lines, each line ending in ``nl``."""
    lines: List[str] = [""] if blank_first else []
    for index, part in enumerate(parts):
        if index:
            lines.append("")
        lines += _indent(part, depth)
    return "".join(line + nl for line in lines)


def _plan_update(existing: str, models: List[str], base: str, skeleton: str, extras: List[str]) -> Plan:
    bom = "﻿" if existing.startswith("﻿") else ""
    body = existing[len(bom):]
    nl = "\r\n" if "\r\n" in body else "\n"
    cfg = parse_cfg(body)  # CfgError names the line
    spans = find_classes(body)
    result = Plan(existing)
    edits: List[Tuple[int, int, str]] = []  # (start, end, new text) on ``body``
    appended: List[List[str]] = []  # whole blocks for the end of the file

    def insert_in(block: ClassSpan, parts: List[List[str]]) -> None:
        """Put the parts in front of the ``}`` of a block, on their own lines."""
        text = _block_parts(nl, parts, blank_first=bool(_children(body, block)))
        line_start = body.rfind("\n", 0, block.close) + 1
        if body[line_start:block.close].strip() == "":
            edits.append((line_start, line_start, text))
        else:
            edits.append((block.close, block.close, nl + text))

    # --- CfgSkeletons: only what is missing ---
    skeleton_blocks = [s for s in spans if s.name.lower() == "cfgskeletons" and s.open >= 0]
    skeleton_parts: List[List[str]] = []
    added: List[str] = []
    if _class_named(cfg, "CfgSkeletons", "Default") is None:
        skeleton_parts.append(_default_skeleton())
        added.append("class Default")
    have_skeleton = skeleton.lower() in [name.lower() for name in skeleton_names(cfg)]
    if have_skeleton:
        try:
            result.infos.append("%s is already in the file (%d bones). It is kept as it is."
                                % (skeleton, len(resolve_skeleton(cfg, skeleton).bones)))
        except CfgError as ex:
            result.warnings.append("%s is in the file but its bones cannot be read (%s). It is kept as it is"
                                   % (skeleton, ex))
    else:
        skeleton_parts.append(_man_skeleton(skeleton))
        added.append(_skeleton_note(skeleton))
    if skeleton_parts:
        if skeleton_blocks:
            insert_in(skeleton_blocks[0], skeleton_parts)
            result.changes.append("added %s to CfgSkeletons" % " and ".join(added))
        else:
            appended.append(_block("CfgSkeletons", skeleton_parts))
            result.changes.append("added CfgSkeletons with %s" % " and ".join(added))

    # --- CfgModels: the base classes if missing, and the class of each model name ---
    model_blocks = [s for s in spans if s.name.lower() == "cfgmodels" and s.open >= 0]
    child_spans: List[ClassSpan] = []
    for block in model_blocks:
        child_spans += _children(body, block)
    have_base = _class_named(cfg, "CfgModels", base) is not None
    base_sections = _inherited_sections(cfg, base) if have_base else merge_sections(MAN_SECTIONS, extras)
    own: Optional[List[str]] = None
    if have_base:
        have = {entry.lower() for entry in base_sections}
        missing = [name for name in extras if name.lower() not in have]
        if missing:  # the file's base class stays; each model class then lists its own sections
            own = merge_sections(base_sections, missing)
            result.infos.append("%s in the file does not list %s in sections[]. Each model class lists its own sections."
                                % (base, ", ".join(missing)))
    model_parts: List[List[str]] = []
    added = []
    if _class_named(cfg, "CfgModels", "Default") is None:
        model_parts.append(_default_model())
        added.append("class Default")
    if not have_base:
        model_parts.append(_base_model(base, skeleton, base_sections))
        added.append("class %s" % base)
    new_models: List[str] = []
    for name in models:
        found = [s for s in child_spans if s.name.lower() == name.lower() and s.open >= 0]
        if not found:
            new_models.append(name)
            added.append("class %s" % name)
            continue
        span = found[0]
        line_start = body.rfind("\n", 0, span.start) + 1
        indent = body[line_start:span.start] if not body[line_start:span.start].strip() else ""
        text = (nl + indent).join(_model_class(name, base, own))  # the lines after the first follow the class's indent
        if span.name != name:
            result.warnings.append("The class %s in the file is written %s here. It is replaced and written as %s"
                                   % (span.name, name, name))
        if body[span.start:span.end] == text:
            result.infos.append("class %s is already up to date." % name)
        else:
            edits.append((span.start, span.end, text))
            result.changes.append("replaced class %s in CfgModels" % name)
    if new_models:
        model_parts.append([line for name in new_models for line in _model_class(name, base, own)])
    if model_parts:
        if model_blocks:
            insert_in(model_blocks[0], model_parts)
            result.changes.append("added %s to CfgModels" % ", ".join(added))
        else:
            appended.append(_block("CfgModels", model_parts))
            result.changes.append("added CfgModels with %s" % ", ".join(added))
    if appended:
        lead = "" if (not body.strip() or body.endswith("\n")) else nl
        tail = (nl + nl).join(nl.join(block) + nl for block in appended)
        edits.append((len(body), len(body), lead + (nl if body.strip() else "") + tail))
    new_body = body
    for start, end, text in sorted(edits, key=lambda edit: edit[0], reverse=True):
        new_body = new_body[:start] + text + new_body[end:]
    result.text = bom + new_body
    result.changed = result.text != existing
    if not result.changed:
        result.changes = []
    return result



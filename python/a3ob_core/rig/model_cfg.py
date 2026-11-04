# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""A small ``model.cfg`` reader: the ``CfgSkeletons`` classes and their bones.

It is our own parser. It reads what a skeleton needs and nothing more: classes (``class A: B { ... };`` and the
forward form ``class A;``), string, number and array properties (``name = value;``, ``name[] = {...};``,
``name[] += {...};``), ``//`` and ``/* */`` comments. Preprocessor lines (``#include``, ``#define``) are not run:
``#include`` lines are listed in ``CfgFile.includes`` and other ``#`` lines in ``CfgFile.directives``, so the caller can
say that part of the file was not read.

How A3OB does the same job (read-only reference, not copied): ``io/config/tokenizer.py`` and ``parser.py`` (a full
config parser, plus a reader for binarized configs), ``io/import_mcfg.py`` ``get_bones`` (pairs of a flat array) and
``get_bones_compiled`` (``skeletonInherit``: inherited bones first, no duplicates). Binarized (rapified) configs are
not read here.

Names are case-insensitive, as in the game: ``CfgSkeletons`` finds ``cfgskeletons``.
"""

import dataclasses
import os
from typing import Dict, List, Optional, Tuple, Union

__all__ = ["CfgError", "CfgClass", "CfgFile", "Skeleton", "parse_cfg", "read_cfg_file", "skeleton_names",
           "resolve_skeleton"]

Value = Union[str, int, float, list]

_PUNCT = "{}[];=:,"
_SPACE = " \t\r\f\v"


class CfgError(ValueError):
    """The file cannot be read or does not say what a skeleton needs. The text is meant for the artist."""


@dataclasses.dataclass
class CfgClass:
    name: str
    base: Optional[str] = None
    #: ``class A;`` with no body: a reference, not a definition.
    is_reference: bool = False
    #: lower-case property name -> (name as written, value)
    props: Dict[str, Tuple[str, Value]] = dataclasses.field(default_factory=dict)
    classes: List["CfgClass"] = dataclasses.field(default_factory=list)

    def prop(self, name: str) -> Optional[Value]:
        found = self.props.get(name.lower())
        return None if found is None else found[1]

    def child(self, name: str) -> Optional["CfgClass"]:
        """The first child class with this name (any case) that has a body, else a reference to it, else None."""
        lower = name.lower()
        reference = None
        for item in self.classes:
            if item.name.lower() == lower:
                if not item.is_reference:
                    return item
                reference = reference or item
        return reference


@dataclasses.dataclass
class CfgFile:
    root: CfgClass
    #: The argument of every ``#include`` line, as written without the quotes. They were not followed.
    includes: List[str] = dataclasses.field(default_factory=list)
    #: Other ``#`` lines (``#define``, ``#ifdef`` ...), as written. They were not run.
    directives: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Skeleton:
    """One ``CfgSkeletons`` class with ``skeletonInherit`` applied."""
    name: str
    #: ``(bone, parent)`` pairs in file order, inherited bones first. An empty parent marks a root.
    bones: List[Tuple[str, str]]
    #: ``isDiscrete``: 1 means 0 or 100 % per vertex, 0 means partial weights. None when the file does not say.
    is_discrete: Optional[int] = None
    pivots_model: str = ""
    #: The skeleton classes this one inherits from, nearest first.
    inherits: List[str] = dataclasses.field(default_factory=list)
    #: Bones of this class dropped because an inherited skeleton already has a bone of that name.
    dropped: List[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------------------------------

_STR, _WORD, _PUN = "str", "word", "punct"


def _tokenize(text: str, includes: List[str], directives: List[str]) -> List[Tuple[str, str, int]]:
    tokens: List[Tuple[str, str, int]] = []
    i, n, line = 0, len(text), 1
    line_start = True
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            line_start = True
        elif c in _SPACE:
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise CfgError("line %d: a /* comment is never closed" % line)
            line += text.count("\n", i, end)
            i = end + 2
            line_start = False
        elif c == "#" and line_start:
            end = i
            while end < n and text[end] != "\n":
                if text[end] == "\\" and end + 1 < n and text[end + 1] == "\n":  # a continued line
                    end += 1
                    line += 1
                end += 1
            _directive(text[i + 1:end].strip(), includes, directives)
            i = end
        elif c == '"':
            j, parts = i + 1, []
            while True:
                if j >= n or text[j] == "\n":
                    raise CfgError("line %d: a string is never closed" % line)
                if text[j] == '"':
                    if text.startswith('""', j):  # "" inside a string is one quote
                        parts.append('"')
                        j += 2
                        continue
                    break
                parts.append(text[j])
                j += 1
            tokens.append((_STR, "".join(parts), line))
            i = j + 1
            line_start = False
        elif text.startswith("+=", i):
            tokens.append((_PUN, "+=", line))
            i += 2
            line_start = False
        elif c in _PUNCT:
            tokens.append((_PUN, c, line))
            i += 1
            line_start = False
        else:
            j = i
            while j < n and text[j] not in _SPACE and text[j] not in _PUNCT and text[j] not in '"\n':
                if text.startswith("//", j) or text.startswith("/*", j):
                    break
                j += 1
            tokens.append((_WORD, text[i:j], line))
            i = j
            line_start = False
    return tokens


def _directive(text: str, includes: List[str], directives: List[str]) -> None:
    if text.lower().startswith("include"):
        argument = text[len("include"):].strip()
        includes.append(argument.strip('"<>').strip())
    else:
        directives.append("#" + text)


# ---------------------------------------------------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str, int]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Tuple[str, str, int]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> Tuple[str, str, int]:
        token = self.peek()
        if token is None:
            raise CfgError("the file ends in the middle of a class or property")
        self.pos += 1
        return token

    def is_punct(self, value: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == _PUN and token[1] == value

    def expect(self, value: str) -> None:
        kind, text, line = self.take()
        if kind != _PUN or text != value:
            raise CfgError("line %d: expected %r but found %r" % (line, value, text))

    def body(self, owner: CfgClass, nested: bool) -> None:
        """Read statements into ``owner`` until the closing brace (``nested``) or the end of the file."""
        while True:
            token = self.peek()
            if token is None:
                if nested:
                    raise CfgError("the file ends before the class %s is closed with }" % owner.name)
                return
            kind, text, line = token
            if kind == _PUN:
                if text == ";":
                    self.pos += 1
                elif text == "}" and nested:
                    self.pos += 1
                    return
                else:
                    raise CfgError("line %d: unexpected %r" % (line, text))
            elif kind == _WORD and text.lower() == "class":
                self.pos += 1
                self.class_statement(owner)
            elif kind == _WORD and text.lower() == "delete":
                self.skip_statement()
            elif kind == _WORD and text.lower() == "enum":
                self.skip_statement()
            elif kind == _WORD:
                self.pos += 1
                self.property_statement(owner, text, line)
            else:
                raise CfgError("line %d: unexpected string %r" % (line, text))

    def skip_statement(self) -> None:
        """Skip to the ``;`` that ends this statement, over any braces."""
        depth = 0
        while True:
            kind, text, _ = self.take()
            if kind == _PUN and text == "{":
                depth += 1
            elif kind == _PUN and text == "}":
                depth -= 1
            elif kind == _PUN and text == ";" and depth <= 0:
                return

    def class_statement(self, owner: CfgClass) -> None:
        kind, name, line = self.take()
        if kind != _WORD:
            raise CfgError("line %d: expected a class name but found %r" % (line, name))
        item = CfgClass(name)
        if self.is_punct(":"):
            self.pos += 1
            kind, base, line = self.take()
            while kind == _WORD and base.lower() in ("public", "protected", "private") and not self.is_punct("{"):
                kind, base, line = self.take()
            if kind != _WORD:
                raise CfgError("line %d: expected a base class name but found %r" % (line, base))
            item.base = base
        if self.is_punct(";"):
            self.pos += 1
            item.is_reference = True
        else:
            self.expect("{")
            self.body(item, nested=True)
            if self.is_punct(";"):
                self.pos += 1
        owner.classes.append(item)

    def property_statement(self, owner: CfgClass, name: str, line: int) -> None:
        is_array = False
        if self.is_punct("["):
            self.pos += 1
            self.expect("]")
            is_array = True
        kind, text, at = self.take()
        if kind != _PUN or text not in ("=", "+="):
            raise CfgError("line %d: expected = after %s but found %r" % (at, name, text))
        value = self.value()
        # Anything left before the ; (an expression such as 1+2) is skipped: skeletons never need it.
        while not self.is_punct(";"):
            if self.is_punct("}") or self.peek() is None:
                raise CfgError("line %d: the property %s has no ; at its end" % (line, name))
            self.pos += 1
        self.pos += 1
        key = name.lower()
        if text == "+=" and is_array and isinstance(value, list):
            old = owner.props.get(key)
            if old is not None and isinstance(old[1], list):
                value = old[1] + value
        owner.props[key] = (name, value)

    def value(self) -> Value:
        kind, text, line = self.take()
        if kind == _STR:
            return text
        if kind == _WORD:
            return _number(text)
        if kind == _PUN and text == "{":
            items: List[Value] = []
            while True:
                if self.is_punct("}"):
                    self.pos += 1
                    return items
                items.append(self.value())
                if self.is_punct(","):
                    self.pos += 1
                elif not self.is_punct("}"):
                    token = self.peek()
                    raise CfgError("line %d: expected , or } in an array but found %r" % (token[2] if token else line,
                                                                                         token[1] if token else "end"))
        raise CfgError("line %d: expected a value but found %r" % (line, text))


def _number(text: str) -> Union[int, float, str]:
    for convert in (int, float):
        try:
            return convert(text)
        except ValueError:
            pass
    return text


def parse_cfg(text: str) -> CfgFile:
    """Parse the text of a ``model.cfg``. Raises ``CfgError`` (with a line number) for text it cannot read."""
    includes: List[str] = []
    directives: List[str] = []
    parser = _Parser(_tokenize(text, includes, directives))
    root = CfgClass("")
    parser.body(root, nested=False)
    return CfgFile(root, includes, directives)


def read_cfg_file(path: str) -> CfgFile:
    """Read and parse a ``model.cfg`` file. A binarized config (``raP`` header) is refused with a clear text."""
    try:
        with open(path, "rb") as file:
            raw = file.read()
    except OSError as ex:
        raise CfgError("the file %s cannot be opened (%s)" % (path, ex.strerror or ex)) from None
    if raw[:4] == b"\0raP":
        raise CfgError("%s is a binarized config. Use the text model.cfg" % os.path.basename(path))
    return parse_cfg(raw.decode("utf-8-sig", errors="replace"))


# ---------------------------------------------------------------------------------------------------------------------
# Skeletons
# ---------------------------------------------------------------------------------------------------------------------


def _skeleton_classes(cfg: CfgFile) -> Dict[str, CfgClass]:
    """lower-case name -> class, over every ``CfgSkeletons`` block. A definition wins over a reference."""
    found: Dict[str, CfgClass] = {}
    for block in cfg.root.classes:
        if block.name.lower() != "cfgskeletons":
            continue
        for item in block.classes:
            key = item.name.lower()
            if key not in found or (found[key].is_reference and not item.is_reference):
                found[key] = item
    return found


def skeleton_names(cfg: CfgFile) -> List[str]:
    """The names of the skeleton classes (references left out), in file order."""
    names: List[str] = []
    seen = set()
    scope = _skeleton_classes(cfg)
    for block in cfg.root.classes:
        if block.name.lower() != "cfgskeletons":
            continue
        for item in block.classes:
            if item.is_reference or item.name.lower() in seen or scope[item.name.lower()].is_reference:
                continue
            seen.add(item.name.lower())
            names.append(item.name)
    return names


def _inherited_prop(item: CfgClass, key: str, scope: Dict[str, CfgClass]) -> Optional[Value]:
    """A property of a class, or of its ``class A: B`` base (nearest first)."""
    seen = set()
    while item is not None and item.name.lower() not in seen:
        seen.add(item.name.lower())
        value = item.prop(key)
        if value is not None:
            return value
        item = scope.get(item.base.lower()) if item.base else None
    return None


def _pairs(name: str, value: Optional[Value]) -> List[Tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CfgError("skeletonBones of %s must be a list {...} of names" % name)
    if len(value) % 2:
        raise CfgError("skeletonBones of %s has %d entries. Bones come as pairs: \"bone\", \"parent\" (an empty "
                       "parent for a root)" % (name, len(value)))
    return [(str(value[i]), str(value[i + 1])) for i in range(0, len(value), 2)]


def resolve_skeleton(cfg: CfgFile, name: str) -> Skeleton:
    """The skeleton class ``name`` (any case) with ``skeletonInherit`` applied: the inherited bones come first, then the
    class's own. A bone of the class whose name (any case) an inherited bone already has is dropped, as in A3OB."""
    scope = _skeleton_classes(cfg)
    start = scope.get(name.lower())
    if start is None or start.is_reference:
        known = skeleton_names(cfg)
        raise CfgError("the skeleton %s is not in this model.cfg. It has: %s" % (name, ", ".join(known) or "(none)"))
    dropped: List[str] = []
    chain: List[str] = []

    def bones_of(item: CfgClass, path: Tuple[str, ...]) -> List[Tuple[str, str]]:
        if item.name.lower() in path:
            raise CfgError("skeletonInherit loops: %s" % " -> ".join(list(path) + [item.name]))
        own = _pairs(item.name, _inherited_prop(item, "skeletonBones", scope))
        parent_name = _inherited_prop(item, "skeletonInherit", scope)
        if not isinstance(parent_name, str) or not parent_name.strip():
            return own
        parent = scope.get(parent_name.strip().lower())
        if parent is None or parent.is_reference:
            raise CfgError("%s inherits from %s (skeletonInherit), which is not in CfgSkeletons"
                           % (item.name, parent_name))
        chain.append(parent.name)
        merged = bones_of(parent, path + (item.name.lower(),))
        taken = {bone.lower() for bone, _ in merged}
        for bone, owner in own:
            if bone.lower() in taken:
                dropped.append(bone)
            else:
                merged.append((bone, owner))
        return merged

    bones = bones_of(start, ())
    discrete = _inherited_prop(start, "isDiscrete", scope)
    pivots = _inherited_prop(start, "pivotsModel", scope)
    return Skeleton(
        name=start.name, bones=bones, is_discrete=int(discrete) if isinstance(discrete, (int, float)) else None,
        pivots_model=pivots if isinstance(pivots, str) else "", inherits=chain, dropped=dropped)

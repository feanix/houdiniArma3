# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Export options (spec 16, ux-tools.md 5.9) and the path and name rules they switch on.

Every option that changes data is off by default, so an untouched file exports to the same bytes (L0). The rules here
need no ``hou``.

The lowercase option follows A3OB's ``force_lowercase`` (``io/data_p3d.py`` line 752): texture paths, material paths,
selection names (so proxy paths too) and property names and values. The relative-path option follows A3OB's
``make_relative`` (``utilities/generic.py`` line 192) but does not change the case: lowercase is its own option.
"""

import fnmatch
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

__all__ = [
    "ExportOptions", "DEFAULT_SELECTION_MASK", "DUPLICATES", "UV_LOD_TYPES", "keeps_uvs", "matches_mask", "make_relative", "relative_proxy_name",
    "fold_collisions",
]

DEFAULT_SELECTION_MASK = "* ^__*"
#: What to do with a LOD whose signature was already written (spec 3.2): stop, drop the later one, or write both.
DUPLICATES = ("fail", "skip", "ignore")
#: Spec 18.4: Visual, View Gunner, View Pilot, View Cargo, View Commander, Unknown, Shadow and its three views, Ground
#: Layer (A3OB ``lod_allow_uvs``, ``utilities/data.py`` line 150). Every other LOD type drops its UVs.
UV_LOD_TYPES = frozenset({0, 1, 2, 3, 18, -1, 4, 26, 27, 28, 31})


def keeps_uvs(lod_type: int) -> bool:
    """True if a LOD of this type keeps its UVs when the Drop UVs option is on."""
    return lod_type in UV_LOD_TYPES


@dataclass
class ExportOptions:
    """The switches of the exporter. The defaults are the lossless ones."""

    #: Which point groups are selections (spec 7.2). ``^`` before a pattern excludes it.
    selection_mask: str = DEFAULT_SELECTION_MASK
    lowercase: bool = False
    relative_paths: bool = False
    project_root: str = ""
    sort_sections: bool = False
    sort_lods: bool = False
    auto_components: bool = False
    duplicate_lods: str = "fail"
    #: D17 (spec 18.2): ``component<digits>`` selections become ``component01``, ``component02``... in written order.
    renumber_components: bool = False
    #: D17 (spec 18.3): English selection names become the Czech names of the game.
    czech_names: bool = False
    #: D17 (spec 18.4): LODs of a type that does not use UVs are written without them.
    drop_uvs: bool = False
    #: Ids of the tier-2 per-LOD-type rules that are switched off (spec 17.1.1, for example ``"T2-TRI"``). All are on by
    #: default. They only change which warnings appear, never the bytes.
    rules_off: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duplicate_lods not in DUPLICATES:
            raise ValueError("duplicate_lods must be one of %s, got %r" % (", ".join(DUPLICATES), self.duplicate_lods))
        self.rules_off = tuple(self.rules_off)


def matches_mask(mask: str, encoded_name: str) -> bool:
    """True if a group name passes the selection mask (``"* ^__*"``: everything except names starting with ``__``).

    Patterns are separated by spaces and use ``*`` and ``?``. A pattern with a leading ``^`` excludes. With no
    positive pattern, every name that is not excluded passes."""
    positive: List[str] = []
    negative: List[str] = []
    for token in mask.split():
        if token.startswith("^"):
            negative.append(token[1:])
        else:
            positive.append(token)
    if any(fnmatch.fnmatchcase(encoded_name, pattern) for pattern in negative):
        return False
    return not positive or any(fnmatch.fnmatchcase(encoded_name, pattern) for pattern in positive)


_DRIVE = re.compile(r"^[A-Za-z]:")


def make_relative(path: str, root: str) -> str:
    """Spec 5 "Paths": strip the project root, or else the drive letter. The case is not changed.

    ``P:\\a3\\data_f\\x.paa`` with root ``P:\\`` gives ``a3\\data_f\\x.paa``. Without a match only the drive goes."""
    if not path:
        return path
    root = root.replace("/", "\\").strip()
    if root:
        prefix = root.rstrip("\\") + "\\"
        if path.casefold().startswith(prefix.casefold()):
            return path[len(prefix):]
    return _DRIVE.sub("", path).lstrip("\\")


_PROXY_PARTS = re.compile(r"proxy:(.*)\.(\d+)", re.IGNORECASE | re.DOTALL)


def relative_proxy_name(name: str, root: str) -> str:
    """Make the path inside a proxy selection name relative. Other names pass unchanged."""
    match = _PROXY_PARTS.fullmatch(name)
    if match is None:
        return name
    return "proxy:%s.%s" % (make_relative(match.group(1), root), match.group(2))


def fold_collisions(values: Sequence[str]) -> List[Tuple[str, str]]:
    """Pairs of values that are different but become equal when lowercased (the lowercase collision warning).

    Each later value is paired with the first one that it collides with."""
    first: Dict[str, str] = {}
    pairs: List[Tuple[str, str]] = []
    for value in values:
        folded = value.lower()
        if folded in first:
            if first[folded] != value:
                pairs.append((first[folded], value))
        else:
            first[folded] = value
    return pairs

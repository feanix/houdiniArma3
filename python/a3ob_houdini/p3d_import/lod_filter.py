# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The "LODs" parameter of P3D Import: which LODs of the file to import.

``*`` means all. Otherwise the text holds numbers (file positions), ranges (``2-4``) and, when the LOD names are known,
LOD display names or ``*`` patterns, in any mix, for example ``0 2-4 Geometry*``. Names are matched without regard to
case and may hold spaces ("Geometry Memory", "Resolution 1"); commas also separate entries. Pure Python.
"""

import fnmatch
import re
from typing import List, Optional, Sequence

from ..errors import UserError

__all__ = ["parse_lod_filter"]

_RANGE = re.compile(r"(\d+)(?:-(\d+))?")


def parse_lod_filter(text: str, count: int, names: Optional[Sequence[str]] = None,
                     problems: Optional[List[str]] = None) -> List[int]:
    """The LOD positions selected by ``text`` for a file with ``count`` LODs, ascending and without repeats.

    ``names`` are the display names of the LODs (``count`` of them). With names, an entry that is not a number, a range
    or a name is added to ``problems`` (one text each) and skipped; with no names, or no ``problems`` list, it raises
    ``UserError``. A pattern that matches no LOD counts as an unknown name. Positions that do not exist raise
    ``UserError``."""
    if names is not None and len(names) != count:
        raise ValueError("%d names for %d LODs" % (len(names), count))
    segments = [part.split() for part in text.split(",")]
    segments = [words for words in segments if words]
    if not segments or segments == [["*"]]:
        return list(range(count))
    folded = [name.casefold() for name in names] if names is not None else None
    chosen = set()
    for words in segments:
        position = 0
        while position < len(words):
            position += _take(words, position, count, folded, chosen, problems)
    return sorted(chosen)


def _take(words: List[str], start: int, count: int, folded, chosen: set, problems) -> int:
    """Read one entry that starts at ``words[start]``; return how many words it used."""
    word = words[start]
    match = _RANGE.fullmatch(word)
    if match is not None:
        _add_range(word, match, count, chosen)
        return 1
    if folded is not None:
        for end in range(len(words), start, -1):  # the longest name first: "Geometry Memory" before "Geometry"
            pattern = " ".join(words[start:end]).casefold()
            hits = [i for i, name in enumerate(folded) if fnmatch.fnmatchcase(name, pattern)]
            if hits:
                chosen.update(hits)
                return end - start
    if folded is not None and problems is not None:
        problems.append("LODs: no LOD is called %r. Pick LODs from the menu, or type numbers or names." % word)
        return 1
    raise UserError("LODs: %r is not a LOD number or range. Use * for all LODs, or numbers like 0 2-4." % word)


def _add_range(token: str, match, count: int, chosen: set) -> None:
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) is not None else first
    if last < first:
        raise UserError("LODs: the range %r runs backwards. Write it as %d-%d." % (token, last, first))
    if last >= count:
        raise UserError("LODs: LOD %d does not exist. The file has %d LODs (0-%d)." % (last, count, count - 1))
    chosen.update(range(first, last + 1))

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The "LODs" parameter of P3D Import: which LODs of the file to import, by file position.

``*`` means all. Otherwise a list of numbers and ranges, for example ``0 2-4``. Pure Python.
"""

import re
from typing import List

from ..errors import UserError

__all__ = ["parse_lod_filter"]

_RANGE = re.compile(r"(\d+)(?:-(\d+))?")


def parse_lod_filter(text: str, count: int) -> List[int]:
    """The LOD positions selected by ``text`` for a file with ``count`` LODs, ascending and without repeats.

    Raises ``UserError`` for text that is not ``*`` or a list of numbers and ranges, and for positions that do not
    exist in the file."""
    tokens = text.replace(",", " ").split()
    if not tokens or tokens == ["*"]:
        return list(range(count))
    chosen = set()
    for token in tokens:
        match = _RANGE.fullmatch(token)
        if match is None:
            raise UserError("LODs: %r is not a LOD number or range. Use * for all LODs, or numbers like 0 2-4." % token)
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) is not None else first
        if last < first:
            raise UserError("LODs: the range %r runs backwards. Write it as %d-%d." % (token, last, first))
        if last >= count:
            raise UserError("LODs: LOD %d does not exist. The file has %d LODs (0-%d)." % (last, count, count - 1))
        chosen.update(range(first, last + 1))
    return sorted(chosen)

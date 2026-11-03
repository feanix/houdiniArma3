# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The Assemble LODs match rule (spec section 3.2, "Assemble LODs match rule", D14 Q-4), without `hou`.

The rule compares the stored ``a3ob_lod_signature`` (raw int32 bits). It never looks at the decoded type or
resolution, and it reads no prims, so it also works for LODs without polygons.

    result = match_lods(base_signatures, edited_signatures, replace=True)
    result.replaced   {base position: edited position}
    result.appended   edited positions that go to the end, in input order

A signature of ``None`` means "the LOD has no ``a3ob_lod_signature``". Such an edited LOD is always appended, and a
base LOD without one is never replaced.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

__all__ = ["MatchResult", "match_lods"]


@dataclass
class MatchResult:
    #: ``{position in the base: position in the edited list}``.
    replaced: Dict[int, int] = field(default_factory=dict)
    #: Positions in the edited list that match nothing, in input order.
    appended: List[int] = field(default_factory=list)
    #: Signatures where an edited LOD matched but the base holds the signature more than once.
    ambiguous_base: List[int] = field(default_factory=list)
    #: Signatures that more than one edited LOD carries (only with ``replace`` on).
    repeated_edited: List[int] = field(default_factory=list)


def match_lods(base: Sequence[Optional[int]], edited: Sequence[Optional[int]], replace: bool = True) -> MatchResult:
    """Decide which edited LOD replaces which base LOD, and which are appended."""
    result = MatchResult()
    if not replace:
        result.appended = list(range(len(edited)))
        return result
    free: Dict[int, List[int]] = {}
    base_count: Dict[int, int] = {}
    for position, signature in enumerate(base):
        if signature is not None:
            free.setdefault(signature, []).append(position)
            base_count[signature] = base_count.get(signature, 0) + 1
    seen: Dict[int, int] = {}
    repeated: Set[int] = set()
    ambiguous: Set[int] = set()
    for position, signature in enumerate(edited):
        if signature is None:
            result.appended.append(position)
            continue
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] == 2:
            repeated.add(signature)
        candidates = free.get(signature)
        if candidates:
            result.replaced[candidates.pop(0)] = position
            if base_count[signature] > 1:
                ambiguous.add(signature)
        else:
            result.appended.append(position)
    result.ambiguous_base = sorted(ambiguous)
    result.repeated_edited = sorted(repeated)
    return result

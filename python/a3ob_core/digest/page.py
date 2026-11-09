# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The page rules follow the digest tools of the VoxelTerrarium project (docs/design/digests.md); the code is our own.
"""The page every digest prints, and the figures all digests format the same way (docs/design/digests.md), with no Houdini.

A page is a summary line first, then lines that name what was read (scene, node, part), the figures, and a last line
``next:`` that names the digests that go deeper. It keeps to ``MAX_LINES`` lines of ``MAX_COLUMNS`` columns. A page that is
longer is still printed whole; ``Page.size_notice`` says so (the command line prints it on stderr).

A figure that was not recorded is ``NR`` in a table cell and a sentence in the ``not recorded:`` line, never a zero.
"""

import dataclasses
import textwrap
from typing import List, Optional, Sequence

import numpy as np

__all__ = ["MAX_LINES", "MAX_COLUMNS", "NR", "EXIT_ANSWERED", "EXIT_COULD_NOT", "EXIT_WRONG_QUESTION", "DigestError",
           "CouldNotAnswer", "WrongQuestion", "Page", "cm", "mm", "deg", "count", "num", "percentiles", "row", "pairs", "clip", "short_label"]

MAX_LINES = 25
MAX_COLUMNS = 80
#: The cell of a figure that was not recorded. The page explains each one in its ``not recorded:`` line.
NR = "n/r"
EXIT_ANSWERED, EXIT_COULD_NOT, EXIT_WRONG_QUESTION = 0, 1, 2


class DigestError(Exception):
    """A digest that ends without a page. ``code`` is the exit code of the command line."""
    code = EXIT_COULD_NOT


class CouldNotAnswer(DigestError):
    """The question is fine but the scene cannot answer it (a node without a skeleton, meshes of another topology)."""
    code = EXIT_COULD_NOT


class WrongQuestion(DigestError):
    """The question was written wrongly (an unknown digest, node, part or group, a bad option)."""
    code = EXIT_WRONG_QUESTION


def _ok(value) -> bool:
    return value is not None and np.isfinite(value)


def num(value, digits: int = 1) -> str:
    """A figure with ``digits`` decimals, or ``NR`` when there is none. Negative zero prints as zero."""
    if not _ok(value):
        return NR
    text = "%.*f" % (digits, float(value))
    return text[1:] if text.startswith("-") and float(text) == 0.0 else text


def cm(metres, digits: int = 1) -> str:
    """Metres (the scene's unit) printed as centimetres."""
    return num(None if metres is None else metres * 100.0, digits)


def mm(metres, digits: int = 1) -> str:
    return num(None if metres is None else metres * 1000.0, digits)


def deg(degrees, digits: int = 1) -> str:
    return num(degrees, digits)


def count(value) -> str:
    return NR if value is None else "%d" % value


def percentiles(values: np.ndarray, which: Sequence[float] = (1, 50, 99)) -> List[Optional[float]]:
    """The percentiles of the finite values, or ``None`` for each when there is none."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not len(values):
        return [None] * len(which)
    return [float(v) for v in np.percentile(values, list(which))]


def clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters from the left, with ``...`` where it was cut (the end of a path is the part that
    tells nodes apart)."""
    return text if len(text) <= width else "..." + text[len(text) - width + 3:]


def short_label(label: str, width: int = 7) -> str:
    """A body label for a summary line: its first word, cut to ``width`` characters."""
    return (label.split() or [label])[0][:width]


def row(cells: Sequence[str], widths: Sequence[int], left: int = 1) -> str:
    """A table row: the first ``left`` cells flush left, the others flush right, apart by two spaces."""
    parts = [cell.ljust(width) if k < left else cell.rjust(width) for k, (cell, width) in enumerate(zip(cells, widths))]
    return "  ".join(parts).rstrip()


def pairs(entries: Sequence[str], per_line: int, gap: str = " | ") -> List[str]:
    """Entries packed ``per_line`` to a line, apart by ``gap`` (each entry padded to the width of the widest)."""
    width = max((len(e) for e in entries), default=0)
    per_line = max(1, min(per_line, (MAX_COLUMNS + len(gap)) // (width + len(gap))))  # as many as fit the page
    lines = []
    for start in range(0, len(entries), per_line):
        chunk = entries[start:start + per_line]
        lines.append(gap.join(e.ljust(width) for e in chunk).rstrip())
    return lines


@dataclasses.dataclass
class Page:
    """One digest page. ``summary`` is the first line; ``context`` names what was read; ``body`` holds the figures;
    ``missing`` the reasons for the ``NR`` cells; ``next`` the digests that go deeper."""
    summary: str
    context: List[str] = dataclasses.field(default_factory=list)
    body: List[str] = dataclasses.field(default_factory=list)
    missing: List[str] = dataclasses.field(default_factory=list)
    next: str = ""

    def lines(self) -> List[str]:
        out = [self.summary] + list(self.context) + list(self.body)
        for label, text in (("not recorded: ", "; ".join(self.missing)), ("next: ", self.next)):
            if text:
                out.extend(textwrap.wrap(label + text, MAX_COLUMNS, subsequent_indent="  ", break_long_words=False))
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def size_notice(self) -> str:
        """Empty when the page keeps the limits; else a sentence for stderr."""
        lines = self.lines()
        wide = max((len(line) for line in lines), default=0)
        if len(lines) <= MAX_LINES and wide <= MAX_COLUMNS:
            return ""
        return "digest: this page has %d lines and is %d columns wide (the limits are %d and %d); printed whole" % (
            len(lines), wide, MAX_LINES, MAX_COLUMNS)

    def __str__(self) -> str:
        return self.text()

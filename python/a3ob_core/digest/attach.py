# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the rule is new (docs/design/digests.md, task #91).
"""The attach digest: where a one-bone piece (a helmet) sits against the joint of its bone, and where the neck meets it,
in the posed mesh and in the unposed one (task #91). No Houdini.

The piece is the points of the mesh whose heaviest bone is the bone asked for and that are not in the body group (a helmet
is its own points; the body that follows the same bone is not the piece). Posed means mesh A against the fitted skeleton,
unposed means mesh B against the Arma skeleton; the reference is the posed value, so a change of 0 means the piece kept its
place on its bone. The #83 head-turn digest measured the turn of the head only (pitch, yaw, roll about its own centre), never
where it sits, so it could not see a head that was turned correctly and put in the wrong place.

- **piece -> joint:** the centre of the piece minus the joint of its bone.
- **ring:** the body points at the height of the lowest point of the piece in A (within ``BAND``) and within ``REACH`` of the
  piece sideways: the body where the piece ends. **rim:** the points of the piece in that same band.
- **ring -> joint, rim -> ring, top -> rim:** the centre of the ring minus the joint; the centre of the rim minus the centre of
  the ring (the gap between neck and helmet; plus = rim above the ring); the centre of the highest ``BAND`` of the body points
  near the piece (the neck top) minus the centre of the rim (minus = the neck top is inside the helmet).

The same points are used in A and B (chosen in A, followed by point number), so the change is the move of those points.
"""

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .page import NR, Page, WrongQuestion, cm, row
from .skeleton import Source

__all__ = ["BAND", "REACH", "attach_figures", "attach_page"]

#: The thickness (metres) of the band of points at the rim, the ring and the neck top.
BAND = 0.01
#: The body points count for the ring and the neck top when their distance sideways (X and Z) to the rim is at most this.
REACH = 0.15


def _centre(points: np.ndarray, mask: np.ndarray) -> Optional[np.ndarray]:
    return points[mask].mean(axis=0) if mask.any() else None


def attach_figures(a: np.ndarray, b: np.ndarray, piece: np.ndarray, body: Optional[np.ndarray], joint_a: np.ndarray,
                   joint_b: np.ndarray, band: float = BAND, reach: float = REACH) -> Dict[str, Tuple[Optional[np.ndarray], Optional[np.ndarray]]]:
    """``name -> (posed vector, unposed vector)`` (metres, ``None`` where the mesh has no such points) of the four figures:
    ``piece``, ``ring_joint``, ``rim`` and ``top`` (see the module text). ``a`` and ``b`` are the points of the mesh before and after
    (same order), ``piece`` and ``body`` bool masks (``body`` None: no body, so no ring), ``joint_a`` and ``joint_b`` the
    joint of the piece's bone in the fitted and the Arma skeleton."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    out: Dict[str, Tuple[Optional[np.ndarray], Optional[np.ndarray]]] = {}
    if not piece.any():
        return {k: (None, None) for k in ("piece", "ring_joint", "rim", "top")}
    out["piece"] = (a[piece].mean(axis=0) - joint_a, b[piece].mean(axis=0) - joint_b)
    low = a[piece][:, 1].min()
    rim = piece & (a[:, 1] <= low + band)
    rim_a, rim_b = _centre(a, rim), _centre(b, rim)
    near = np.zeros(len(a), dtype=bool)
    if body is not None:
        side = np.linalg.norm(a[:, [0, 2]] - rim_a[[0, 2]], axis=1)
        near = body & ~piece & (side <= reach)
    ring = near & (np.abs(a[:, 1] - low) <= band)
    ring_a, ring_b = _centre(a, ring), _centre(b, ring)
    top = near & (a[:, 1] >= a[near][:, 1].max() - band) if near.any() else near
    top_a, top_b = _centre(a, top), _centre(b, top)
    out["ring_joint"] = (None, None) if ring_a is None else (ring_a - joint_a, ring_b - joint_b)
    out["rim"] = (None, None) if ring_a is None else (rim_a - ring_a, rim_b - ring_b)
    out["top"] = (None, None) if top_a is None else (top_a - rim_a, top_b - rim_b)
    return out


def _cells(pair: Tuple[Optional[np.ndarray], Optional[np.ndarray]]) -> List[str]:
    posed, unposed = pair
    if posed is None or unposed is None:
        return [NR] * 7
    return [cm(v) for v in posed] + [cm(v) for v in unposed] + [cm(float(np.linalg.norm(unposed - posed)))]


def attach_page(a: np.ndarray, b: np.ndarray, dominant: np.ndarray, body: Optional[np.ndarray], bones: Sequence[str],
                fitted: Dict[str, np.ndarray], arma: Dict[str, np.ndarray], source_a: Source, source_b: Source,
                body_name: str = "body") -> Page:
    """The page. ``dominant`` is the lower-case heaviest bone of every point (``""`` for none), ``bones`` the bones to look at,
    ``fitted`` and ``arma`` map a lower-case joint name to its position."""
    widths = (15, 6, 6, 6, 6, 6, 6, 7)
    body_lines: List[str] = [row(("cm", "posed", "", "", "unposed", "", "", "change"), widths, 1),
                             row(("", "x", "y", "z", "x", "y", "z", "|b - a|"), widths, 1)]
    missing: List[str] = []
    first = None
    for bone in bones:
        bone = bone.lower()
        if bone not in fitted or bone not in arma:
            raise WrongQuestion("no joint %s in the fitted and the Arma skeleton" % bone)
        piece = (dominant == bone) & ~(body if body is not None else np.zeros(len(a), dtype=bool))
        if not piece.any():
            raise WrongQuestion("no point outside the group %s follows the bone %s" % (body_name, bone))
        figures = attach_figures(a, b, piece, body, fitted[bone], arma[bone])
        body_lines.append("%s: %d points outside group %s" % (bone, int(piece.sum()), body_name))
        for name, label in (("piece", "piece->joint"), ("ring_joint", "ring->joint"), ("rim", "rim->ring"),
                            ("top", "neck top->rim")):
            cells = _cells(figures[name])
            body_lines.append(row([label] + cells, widths, 1))
            if cells[0] == NR:
                missing.append("%s of %s (no body point near the rim of the piece)" % (label, bone))
        first = first or (bone, int(piece.sum()), figures)
    body_lines.append("world axes, +y up, +z forward. reference: the posed values, change 0 = kept")
    body_lines.append("ring: body points at the piece's lowest height (+-1 cm), within 15 cm sideways")
    if first:
        bone, n, figures = first
        change = lambda k: (None if figures[k][0] is None else cm(float(np.linalg.norm(figures[k][1] - figures[k][0]))))
        summary = "attach: %s piece (%d pts) moved %s cm against its joint, neck gap %s cm" % (
            bone, n, change("piece") or NR, change("rim") or NR)
    else:
        summary = "attach: " + NR
    context = ["scene: %s; A posed (fitted joints), B unposed (Arma joints)" % source_a.scene_name(),
               source_a.line("A: "), source_b.line("B: ")]
    return Page(summary, context, body_lines, missing, "head-turn A B; deform-compare A B --group G; skeleton")

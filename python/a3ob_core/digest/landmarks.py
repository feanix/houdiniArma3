# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no landmarks; the measures are those of a3ob_core.rig.landmarks (docs/design/digests.md).
"""The landmarks digest, with no Houdini (task #81): where the landmarks are, beside the matching measures on the mesh and on
the Arma sample.

Nothing is guessed here. The crotch on the mesh and the shoulder top come from ``landmarks.measure`` (the function the guess
uses), run on the points of the chosen group; the Arma sample's side is the ``Template`` that ``make_template`` made
(``truth`` are its landmarks, ``measure`` its own measurements). Heights are above the sole (the lowest point) of the body
concerned, so a mesh and the sample of another size are comparable. No verdicts: the page prints the figures.
"""

import dataclasses
from typing import Dict, List, Optional

import numpy as np

from a3ob_core.rig import LANDMARK_NAMES
from a3ob_core.rig.landmarks import PAIRS, Template, measure

from .page import Page, clip, cm, row, NR
from .skeleton import Skel, Source, measure_values

__all__ = ["SampleSide", "landmarks_page", "short"]

MIN_MESH_POINTS = 50


@dataclasses.dataclass
class SampleSide:
    """The Arma sample's side: its template and its rest skeleton (both optional; a page says what is not recorded)."""
    template: Optional[Template] = None
    skeleton: Optional[Skel] = None
    #: Why there is no template, when there is none (the reader's message).
    note: str = ""


def short(name: str) -> str:
    """``lm_shoulder_l`` -> ``shoulder``."""
    name = name[3:] if name.startswith("lm_") else name
    return name[:-2] if name.endswith(("_l", "_r")) else name


def _pos(positions: Dict[str, np.ndarray], name: str) -> Optional[np.ndarray]:
    return positions.get(name)


def _mean_y(positions: Dict[str, np.ndarray], a: str, b: str) -> Optional[float]:
    return None if a not in positions or b not in positions else float((positions[a][1] + positions[b][1]) / 2.0)


def _width(positions: Dict[str, np.ndarray], a: str, b: str) -> Optional[float]:
    return None if a not in positions or b not in positions else float(np.linalg.norm(positions[a] - positions[b]))


def _hip_to_crotch(positions: Dict[str, np.ndarray]) -> Optional[float]:
    if not all(n in positions for n in ("lm_hip_l", "lm_hip_r", "lm_groin")):
        return None
    return float(np.linalg.norm((positions["lm_hip_l"] + positions["lm_hip_r"]) / 2.0 - positions["lm_groin"]))


def _above(value: Optional[float], sole: Optional[float]) -> Optional[float]:
    return None if value is None or sole is None else value - sole


def landmarks_page(positions: Dict[str, np.ndarray], mesh: Optional[np.ndarray], sample: SampleSide, source: Source,
                   mesh_label: str, fitted: Optional[Skel] = None) -> Page:
    """The page. ``positions`` are the landmarks of the node (metres, the ones it holds; others are not recorded), ``mesh``
    the points of the group the mesh measures run on (None: no mesh), ``mesh_label`` says which mesh and group,
    ``fitted`` the fitted skeleton when the node has one (for pelvis-to-hips)."""
    missing: List[str] = []
    m = None
    if mesh is None or len(mesh) < MIN_MESH_POINTS:
        missing.append("mesh column and heights above the sole (%s)" % (
            "no mesh given" if mesh is None else "fewer than %d points in the group" % MIN_MESH_POINTS))
    else:
        m = measure(mesh)
    t = sample.template
    if t is None:
        missing.append("Arma sample columns (%s)" % (sample.note or "no skeleton and sample body for the template"))
    absent = [n for n in LANDMARK_NAMES if n not in positions]
    if absent:
        missing.append("%d landmarks not set (%s%s)" % (len(absent), ", ".join(n[3:] for n in absent[:4]),
                                                        " ..." if len(absent) > 4 else ""))
    found_crotch = None
    if m is not None:
        if m.groin_found:
            found_crotch = m.groin
        else:
            missing.append("crotch on the mesh (the legs touch, no gap was found)")
    gap = None
    if found_crotch is not None and "lm_groin" in positions:
        gap = float(np.linalg.norm(positions["lm_groin"] - found_crotch))
    summary = "landmarks: %d of %d set; landmark crotch %s cm from the mesh crotch" % (
        len(positions), len(LANDMARK_NAMES), cm(gap))
    context = ["scene: " + source.scene_name(), source.line("node: "), "mesh: " + clip(mesh_label, 70)]
    body: List[str] = []
    widths = (9, 1, 6, 6, 6, 1, 6, 6, 6)
    body.append(row(("position cm", "", "x", "y", "z", "", "x", "y", "z"), widths, 1))

    def xyz(name: str):
        p = _pos(positions, name)
        return ("", NR, NR, NR) if p is None else ("", cm(p[0]), cm(p[1]), cm(p[2]))

    for name in ("lm_chin", "lm_groin"):
        body.append(row((short(name), "") + xyz(name)[1:] + ("", "", "", ""), widths, 1))
    for left, right in PAIRS:
        body.append(row((short(left), "L") + xyz(left)[1:] + ("R",) + xyz(right)[1:], widths, 1))
    # the measures: the landmarks, the same measure on the mesh, then on the Arma sample (its landmarks, its mesh)
    tt = t.truth if t is not None else {}
    tm = t.measure if t is not None else None
    sole, tsole = (m.y0 if m is not None else None), (tm.y0 if tm is not None else None)
    mw = (21, 9, 9, 9, 9)
    body.append(row(("measure cm", "landmark", "mesh", "Arma lm", "Arma mesh"), mw, 1))
    body.append(row(("crotch height", cm(_above(_pos_y(positions, "lm_groin"), sole)), cm(_above(_f(found_crotch), sole)),
                     cm(_above(_pos_y(tt, "lm_groin"), tsole)), cm(_above(tm.groin_y if tm is not None else None, tsole))), mw, 1))
    body.append(row(("shoulder height", cm(_above(_mean_y(positions, "lm_shoulder_l", "lm_shoulder_r"), sole)),
                     cm(_above(m.shoulder_y if m is not None else None, sole)),
                     cm(_above(_mean_y(tt, "lm_shoulder_l", "lm_shoulder_r"), tsole)),
                     cm(_above(tm.shoulder_y if tm is not None else None, tsole))), mw, 1))
    body.append(row(("hip width", cm(_width(positions, "lm_hip_l", "lm_hip_r")), NR, cm(_width(tt, "lm_hip_l", "lm_hip_r")), NR),
                    mw, 1))
    body.append(row(("hip-to-crotch", cm(_hip_to_crotch(positions)), NR, cm(_hip_to_crotch(tt)), NR), mw, 1))
    fitted_value = measure_values(fitted)["pelvis_hips"] if fitted is not None else None
    arma_value = measure_values(sample.skeleton)["pelvis_hips"] if sample.skeleton is not None else None
    body.append(row(("pelvis-to-hips", cm(fitted_value), NR, cm(arma_value), NR), mw, 1))
    body.append("mesh, Arma mesh: measured on the body; Arma lm: the sample's landmark")
    if fitted is None:
        missing.append("pelvis-to-hips of the landmarks (the node has no fitted skeleton)")
    return Page(summary, context, body, missing, "skeleton; skeleton-compare A B; weights")


def _pos_y(positions: Dict[str, np.ndarray], name: str) -> Optional[float]:
    return float(positions[name][1]) if name in positions else None


def _f(value) -> Optional[float]:
    return None if value is None else float(value[1])

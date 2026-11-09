# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB has no digests; the rule it measures is a3ob_core.rig.orient (docs/design/character-rigging.md, task #86).
"""The joint-axes digest, with no Houdini (task #86): how well the stored joint frames follow the one orientation rule.

Per joint: how far local +X is off the direction to the joint it is aimed at (the main child). Per hinge chain (elbow, knee,
the five fingers): how far local +Z is off the normal of the plane of the three joints. Also the roll joints' twist against
their parent and whether every frame is a pure rotation. Nothing is judged; the rule is printed beside the figures.
"""

from typing import List, Optional

import numpy as np

from a3ob_core.rig import orient as rule

from .page import NR, Page, deg, num, row
from .skeleton import Skel, Source

__all__ = ["joint_axes_page", "x_offsets", "hinge_offsets", "roll_twists"]

#: A chain bent less than this (degrees) at its middle joint decides no plane.
MIN_BEND = rule.FINGER_MIN_BEND


def x_offsets(skel: Skel) -> np.ndarray:
    """Per joint, the angle in degrees between local +X and the direction to the joint the rule aims it at (NaN for a chain end)."""
    targets = rule.rule_targets(skel.names, skel.parents, skel.positions)
    out = np.full(len(skel.names), np.nan)
    for joint, target in enumerate(targets):
        if target >= 0:
            direction = skel.positions[target] - skel.positions[joint]
            direction = direction / np.linalg.norm(direction)
            out[joint] = np.degrees(np.arccos(np.clip(skel.frames[joint][:, 0] @ direction, -1.0, 1.0)))
    return out


def hinge_offsets(skel: Skel):
    """``[(label, side, bend, [off of the first joint, off of the middle joint])]`` for each hinge chain. ``off`` is the angle in
    degrees between local +Z and the normal of the plane of the three joints (either way), or None when the chain is bent less
    than ``MIN_BEND`` degrees."""
    found = []
    for label, side, (a, b, c) in rule.hinge_chains(skel.names):
        normal, bend = rule.bend_normal(skel.positions[a], skel.positions[b], skel.positions[c])
        offs: List[Optional[float]] = [None, None]
        if normal is not None and bend >= MIN_BEND:
            offs = [float(np.degrees(np.arccos(min(1.0, abs(float(skel.frames[j][:, 2] @ normal)))))) for j in (a, b)]
        found.append((label, side, bend, offs))
    return found


def roll_twists(skel: Skel):
    """``[(joint name, degrees)]``: the twist of each roll joint's frame against its parent's about the roll joint's x axis."""
    out = []
    for joint, name in enumerate(skel.names):
        side, rest = rule.split_side(name)
        parent = skel.parents[joint]
        if rest.lower() in rule.ROLLS and parent >= 0:
            x = skel.frames[joint][:, 0]
            yp = skel.frames[parent][:, 1]
            yp = yp - (yp @ x) * x
            yp /= max(np.linalg.norm(yp), 1e-12)
            yc = skel.frames[joint][:, 1]
            out.append((name, float(np.degrees(np.arctan2(np.cross(yp, yc) @ x, yp @ yc)))))
    return out


def _stats(names: List[str], values: np.ndarray):
    good = np.isfinite(values)
    if not good.any():
        return NR, NR, NR
    return deg(np.median(values[good])), deg(values[good].max()), names[int(np.nanargmax(values))]


def joint_axes_page(skel: Skel, source: Source) -> Page:
    """The ``joint-axes`` digest page of a skeleton with frames."""
    missing: List[str] = []
    context = ["scene: " + source.scene_name(), source.line("node: ")]
    if skel.frames is None:
        return Page("joint-axes: %d joints, no transform on the points (no frames to measure)" % len(skel.names), context, [],
                    ["the joint frames (the point attribute transform)"], "skeleton A; skeleton-compare A B")
    offsets = x_offsets(skel)
    hinges = hinge_offsets(skel)
    twists = roll_twists(skel)
    ortho = max(float(np.abs(f @ f.T - np.eye(3)).max()) for f in skel.frames)
    det = min(float(np.linalg.det(f)) for f in skel.frames)
    worst_x = _stats(skel.names, offsets)
    measured = [o for _, _, _, offs in hinges for o in offs if o is not None]
    summary = "joint-axes: %d joints; x off child max %s deg; hinge off plane max %s deg" % (
        len(skel.names), worst_x[1], deg(max(measured)) if measured else NR)
    body = ["rule: x at the main child (Pelvis>Spine, Spine3>neck, Hand>HandMiddle1,",
            "  HandRing>HandRing1); z is the hinge of elbows, knees, fingers, thumbs on",
            "  both sides; y = z x x; rolls share the parent's twist; pure rotations",
            "frames: orthonormal to %.0e, determinant at least %.6f" % (ortho, det)]

    def pick(test):
        return [k for k, name in enumerate(skel.names) if test(rule.split_side(name)[1].lower())]

    sets = [("spine, head", pick(lambda n: n in ("pelvis", "spine", "spine1", "spine2", "spine3", "neck", "neck1", "head"))),
            ("shoulders, arms", pick(lambda n: n in ("shoulder", "arm", "armroll", "forearm", "forearmroll"))),
            ("legs, feet", pick(lambda n: n in rule.LEG_JOINTS)),
            ("hands, fingers", pick(lambda n: n.startswith("hand")))]
    widths = (16, 6, 8, 8, 16)
    body.append(row(("x off, deg", "aimed", "median", "max", "at (max)"), widths, 1))
    for label, joints in sets:
        values = offsets[joints]
        median, high, at = _stats([skel.names[k] for k in joints], values)
        body.append(row((label, "%d" % int(np.isfinite(values).sum()), median, high, at), widths, 1))
    body.append(row(("hinge z off", "L bend", "L off", "R bend", "R off"), (16, 8, 12, 8, 12), 1))
    labels = []
    for label, _, _, _ in hinges:
        if label not in labels:
            labels.append(label)
    for label in labels:
        cells = [label]
        for side in ("Left", "Right"):
            found = [h for h in hinges if h[0] == label and h[1] == side]
            if not found:
                cells += [NR, NR]
                continue
            _, _, bend, offs = found[0]
            cells.append(deg(bend))
            cells.append("%s/%s" % (deg(offs[0]), deg(offs[1])) if offs[0] is not None else NR)
        body.append(row(cells, (16, 8, 12, 8, 12), 1))
    if twists:
        body.append("roll twist against the parent, deg: max %s (%s)" % (
            num(max(abs(t) for _, t in twists)), max(twists, key=lambda item: abs(item[1]))[0]))
    if any(offs[0] is None for _, _, _, offs in hinges):
        missing.append("hinge off for a chain bent under %g deg (the three joints decide no plane)" % MIN_BEND)
    if np.isfinite(offsets).sum() < len(offsets):
        missing.append("x off for the %d chain ends (nothing to aim at)" % int((~np.isfinite(offsets)).sum()))
    return Page(summary, context, body, missing, "skeleton A; skeleton-compare A B; landmarks")

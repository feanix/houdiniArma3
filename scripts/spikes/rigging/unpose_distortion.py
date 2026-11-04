# Spike for task #60 (character rigging design): how well does "fit -> transfer weights -> unpose" put a mesh back on
# Arma's rest pose, and where does it distort? Measuring spike, not product code. Only numpy is required.
#
# Idea. The user mesh is made here from the sample body: posed by linear blend skinning with a known "fitted"
# skeleton (limbs longer, arms lowered, whole body bigger), subdivided once (different topology).
# The true Arma rest positions are therefore known. The spike then does what the design says:
#   1. transfer the sample weights onto the posed mesh (closest point + inpainting, weight_transfer_numpy.transfer),
#   2. run the export clean-up (4 bones, 1/254 grid),
#   3. unpose: move each point by the inverse of its blended bone matrix, from the fitted skeleton to the Arma pivots,
#   4. report the distance to the true rest position, by body region and in joint zones.
# A second test shows a rigid accessory (a cuff round the elbow) unposed with per-point weights, one dominant bone, and
# one fixed blend.
#
# Run (hython, numpy 2.3.2; the Samples are read at run time, nothing is stored):
#   "E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/spikes/rigging/unpose_distortion.py
# Environment: A3OB_SAMPLES = Samples root.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, HERE)

import sample_facts  # noqa: E402
import weight_transfer_numpy as wt  # noqa: E402
from a3ob_core.p3d.reader import read_p3d_file  # noqa: E402


JITTER = 0.0  # metres; 0 so that the unpose with the true weights returns the true rest exactly (the baseline is 0)


def load_pivots(bones):
    """{lowercase bone name: pivot (3,)} from the pivots P3D (first point of the selection, A3OB convention)."""
    path = os.path.join(sample_facts.ADDONS, "TemplateRTM", "ManSkeleton_Pivots.p3d")
    lod = read_p3d_file(path, strict=False).lods[0]
    flat = np.array(lod.points, dtype=np.float64).reshape(-1, 3)
    out = {}
    for name in lod.selection_names():
        members = lod.selection(name).members()
        if members:
            out[name.lower()] = flat[members[0]]
    return {b: out[b.lower()] for b in bones if b.lower() in out}


def rotation(axis, degrees):
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / np.linalg.norm(axis)
    a = np.radians(degrees)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(a) * k + (1 - np.cos(a)) * (k @ k)


class Skeleton:
    """Bones with pivots, parents first. ``skin_matrices`` gives the 4x4 matrix that moves a rest point to the pose."""

    def __init__(self, names, parents, pivots):
        self.names = names
        self.parents = parents
        self.pivots = np.array([pivots[n] for n in names])

    def skin_matrices(self, local_rot=None, length=None, size=1.0):
        local_rot = local_rot or {}
        length = length or {}
        count = len(self.names)
        world_rot = [None] * count
        world_pos = np.zeros((count, 3))
        mats = np.zeros((count, 4, 4))
        for i, name in enumerate(self.names):
            parent = self.parents[i]
            rot_local = local_rot.get(name, np.eye(3))
            if parent < 0:
                world_rot[i] = size * rot_local
                world_pos[i] = self.pivots[i] * size
            else:
                offset = (self.pivots[i] - self.pivots[parent]) * length.get(name, 1.0)
                world_rot[i] = world_rot[parent] @ rot_local
                world_pos[i] = world_pos[parent] + world_rot[parent] @ offset
            mats[i, :3, :3] = world_rot[i]
            mats[i, :3, 3] = world_pos[i] - world_rot[i] @ self.pivots[i]
            mats[i, 3, 3] = 1.0
        return mats


def blend(mats, W):
    """Per-point blended 4x4 matrices (linear blend skinning)."""
    return np.einsum("nb,bij->nij", W, mats)


def skin(P, W, mats):
    M = blend(mats, W)
    h = np.concatenate([P, np.ones((len(P), 1))], axis=1)
    return np.einsum("nij,nj->ni", M, h)[:, :3]


def unskin(P, W, mats):
    M = blend(mats, W)
    h = np.concatenate([P, np.ones((len(P), 1))], axis=1)
    return np.einsum("nij,nj->ni", np.linalg.inv(M), h)[:, :3]


def region_of(name):
    n = name.lower()
    if "hand" in n:
        return "hand"
    if any(k in n for k in ("shoulder", "arm")):
        return "arm"
    if any(k in n for k in ("upleg", "leg")):
        return "leg"
    if any(k in n for k in ("foot", "toe")):
        return "foot"
    if n in ("neck", "neck1", "head"):
        return "head/neck"
    return "torso"


def summarise(label, err_m):
    cm = err_m * 100
    return "%-34s n %5d  mean %.2f cm  median %.2f  p95 %.2f  max %.2f" % (
        label, len(cm), cm.mean(), np.median(cm), np.percentile(cm, 95), cm.max())


def build_scenarios(skel):
    """(label, true pose kwargs, fit-error pose kwargs or None). Left is +X in file space, down is -Y."""
    z = (0, 0, 1)
    arms = {"LeftArm": rotation(z, -20), "RightArm": rotation(z, 20)}
    legs = {"LeftUpLeg": rotation(z, 8), "RightUpLeg": rotation(z, -8)}
    arm_len = {n: 1.10 for n in ("LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand")}
    leg_len = {n: 1.08 for n in ("LeftLeg", "LeftFoot", "RightLeg", "RightFoot")}
    pose = dict(local_rot={**arms, **legs})
    lengths = dict(length={**arm_len, **leg_len})
    both = dict(local_rot={**arms, **legs}, length={**arm_len, **leg_len}, size=1.05)
    def noisy(degrees, fraction):
        rng = np.random.default_rng(7)
        noisy_rot = {n: rotation(rng.normal(size=3), rng.normal(0, degrees)) for n in skel.names}
        for n, m in {**arms, **legs}.items():
            noisy_rot[n] = m @ noisy_rot[n]
        noisy_len = {n: 1.0 + rng.normal(0, fraction) for n in skel.names}
        for n, s in {**arm_len, **leg_len}.items():
            noisy_len[n] = s * noisy_len[n]
        return dict(local_rot=noisy_rot, length=noisy_len, size=1.05)

    return [
        ("A pose only: arms 20 deg down, legs 8 deg apart", pose, None),
        ("B lengths only: arms +10 %, legs +8 %", lengths, None),
        ("C pose + lengths + whole body +5 %", both, None),
        ("D = C, but the fit is off by about 1 deg and 1 % per bone (random)", both, noisy(1.0, 0.01)),
        ("E = C, but the fit is off by about 2.5 deg and 2 % per bone (random)", both, noisy(2.5, 0.02)),
    ]


def main():
    print("numpy %s, python %s" % (np.__version__, sys.version.split()[0]))
    src_P, src_tris, src_W, wnames = wt.load_sample_lod(0)
    cfg = os.path.join(sample_facts.ADDONS, "Test_Character_01", "model.cfg")
    cfg_bones = sample_facts.parse_skeleton(cfg, "OFP2_ManSkeleton")
    pivots = load_pivots(cfg_bones)
    # Bones that have a pivot, parents first; a parent without a pivot is skipped up the chain.
    names = [b for b in cfg_bones if b in pivots]
    index = {b: i for i, b in enumerate(names)}

    def parent_of(b):
        p = cfg_bones[b]
        while p and p not in index:
            p = cfg_bones.get(p, "")
        return index[p] if p else -1

    skel = Skeleton(names, [parent_of(b) for b in names], pivots)
    # Weights columns follow ``wnames``; put them on the skeleton's bone order (bones without weights get zeros).
    col = {n.lower(): k for k, n in enumerate(wnames)}
    missing = [n for n in wnames if n not in index]
    print("skeleton: %d bones with a pivot; %d weighted bones have no pivot: %s" % (len(names), len(missing), missing))
    W_src = np.zeros((len(src_P), len(names)))
    for i, b in enumerate(names):
        if b.lower() in col:
            W_src[:, i] = src_W[:, col[b.lower()]]
    keep = W_src.sum(1) > 0.5
    print("source: sample Resolution 1 body, %d points, %d with weights" % (len(src_P), int(keep.sum())))
    diag = float(np.linalg.norm(src_P.max(0) - src_P.min(0)))

    sub_P, sub_tris, sub_W = wt.subdivide_midpoint(src_P, src_tris, W_src)
    region = np.array([region_of(names[i]) for i in sub_W.argmax(1)])
    top2 = np.sort(sub_W, axis=1)[:, -2]
    joint_zone = top2 > 0.25
    print("user mesh stand-in: sample body subdivided once, %d points, %d triangles; joint zone = second-largest "
          "weight > 0.25: %d points" % (len(sub_P), len(sub_tris), int(joint_zone.sum())))

    results = {}
    for label, true_pose, fit_pose in build_scenarios(skel):
        fit_pose = fit_pose or true_pose
        true_mats = skel.skin_matrices(**true_pose)
        fit_mats = skel.skin_matrices(**fit_pose)
        rng = np.random.default_rng(3)
        target = skin(sub_P, sub_W, true_mats) + rng.normal(0, JITTER, sub_P.shape)  # the user mesh, posed
        source_posed = skin(src_P, W_src, fit_mats)  # the fitted sample body
        moved = np.linalg.norm(target - sub_P, axis=1)
        print("\n== %s ==" % label)
        print("   the pose moves points by mean %.1f cm, max %.1f cm" % (100 * moved.mean(), 100 * moved.max()))
        W_t, info = wt.transfer(source_posed, src_tris, W_src, target, sub_tris, max_distance=0.05 * diag)
        W_c, stats = wt.export_cleanup(W_t)
        print("   weights: matched %d of %d, %d points had > 4 influences before the clean-up" % (
            info["matched"], info["n"], stats["points_over_limit"]))
        for name, W in (("true weights", sub_W), ("transferred, cleaned", W_c)):
            rest = unskin(target, W, fit_mats)
            err = np.linalg.norm(rest - sub_P, axis=1)
            results[(label, name)] = err
            print("   unpose with %-21s all    " % name + summarise("", err)[35:])
            if name == "transferred, cleaned":
                for r in ("torso", "arm", "hand", "leg", "foot", "head/neck"):
                    sel = region == r
                    if sel.any():
                        print("      " + summarise("region %s" % r, err[sel]))
                print("      " + summarise("joint zones", err[joint_zone]))
                print("      " + summarise("not joint zones", err[~joint_zone]))
                # skin-to-skin shape change: edge length ratio against the true rest mesh
                e = np.concatenate([sub_tris[:, [0, 1]], sub_tris[:, [1, 2]], sub_tris[:, [2, 0]]])
                ratio = np.linalg.norm(rest[e[:, 0]] - rest[e[:, 1]], axis=1) / np.maximum(
                    np.linalg.norm(sub_P[e[:, 0]] - sub_P[e[:, 1]], axis=1), 1e-9)
                print("      edge length after unpose / true: median %.3f  p1 %.3f  p99 %.3f" % (
                    np.median(ratio), np.percentile(ratio, 1), np.percentile(ratio, 99)))

    rigid_accessory_test(skel, names, src_P, src_tris, W_src, sub_P, sub_tris, sub_W, diag)


def rigid_accessory_test(skel, names, src_P, src_tris, W_src, sub_P, sub_tris, sub_W, diag):
    """A rigid cuff round the left elbow, glued to LeftForeArm. Three ways to weight it, then unpose."""
    print("\n== rigid accessory: a cuff of the body points within 7 cm of the left elbow, pushed 1.5 cm out, elbow bent 45 deg ==")
    pose = dict(build_scenarios(skel)[2][1])  # scenario C plus the left elbow bent 45 degrees
    pose["local_rot"] = {**pose["local_rot"], "LeftForeArm": rotation((0, 1, 0), 45)}
    mats = skel.skin_matrices(**pose)
    elbow = skel.pivots[names.index("LeftForeArm")]
    near = np.where(np.linalg.norm(sub_P - elbow, axis=1) < 0.07)[0]
    normals = wt.vertex_normals(sub_P, sub_tris)
    remap = -np.ones(len(sub_P), dtype=np.int64)
    remap[near] = np.arange(len(near))
    tris = remap[sub_tris]
    tris = tris[(tris >= 0).all(1)]
    cuff_rest = sub_P[near] + 0.015 * normals[near]
    dom = names.index("LeftForeArm")
    cuff_posed = (mats[dom][:3, :3] @ cuff_rest.T).T + mats[dom][:3, 3]  # truth: rigid on the forearm bone
    source_posed = skin(src_P, W_src, mats)
    # First-pass weights for the cuff points: closest point on the posed body, no thresholds, no inpainting needed.
    tri, bary, dist = wt.closest_points(cuff_posed, source_posed, src_tris)
    per_point = (bary[:, :, None] * W_src[src_tris[tri]]).sum(1)
    mean_blend = per_point.mean(0)
    dominant = np.zeros_like(per_point)
    dominant[:, per_point.mean(0).argmax()] = 1.0
    fixed = np.tile(mean_blend / mean_blend.sum(), (len(cuff_posed), 1))
    cleaned_pp, _ = wt.export_cleanup(per_point)
    top = np.argsort(-mean_blend)[:3]
    print("   cuff: %d points, %d triangles; piece-average weights: %s" % (
        len(near), len(tris), ", ".join("%s %.2f" % (names[i], mean_blend[i]) for i in top)))
    print("   dominant bone of the piece: %s (truth: LeftForeArm)" % names[int(per_point.mean(0).argmax())])
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    base = np.linalg.norm(cuff_rest[e[:, 0]] - cuff_rest[e[:, 1]], axis=1)
    for label, W in (("per-point first-pass weights", cleaned_pp), ("one dominant bone", dominant),
                     ("one fixed blend (piece average)", fixed)):
        rest = unskin(cuff_posed, W, mats)
        # The true rest is cuff_rest only if the unpose is taken from the forearm bone; the Arma rest is cuff_rest too.
        err = np.linalg.norm(rest - cuff_rest, axis=1)
        ratio = np.linalg.norm(rest[e[:, 0]] - rest[e[:, 1]], axis=1) / base
        print("   %-34s offset from true rest: mean %.2f cm max %.2f cm | edge length ratio p1 %.3f median %.3f p99 %.3f" % (
            label, 100 * err.mean(), 100 * err.max(), np.percentile(ratio, 1), np.median(ratio),
            np.percentile(ratio, 99)))


if __name__ == "__main__":
    main()

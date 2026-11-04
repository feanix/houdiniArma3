# Character rigging for Arma 3 in Houdini: research and design

<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

Status: second revision (2026-11-04): task #60, owner decision D36; the owner's answers D37 and the verified wiki facts
were added in task #61. This is a design document. The product code it describes is built phase by phase (the Rig
Skeleton node is the first). The spike scripts are in `scripts/spikes/rigging/` and their printed output is in
`docs/design/evidence/rigging/`. Nothing from the Arma 3 Samples is stored in the repository: the spikes read the
Samples from the owner's disk at run time and print numbers and names only.

## 0. Summary

**Goal.** Rigging a character for Arma 3 in Houdini should be as easy as possible. The artist points at a `model.cfg`,
takes Bohemia's rigged sample body, and gets weights on their own mesh with little or no hand fixing.

**What we found.**

1. Houdini 22.0.466 has **no automatic joint placement** for an arbitrary mesh (section 1.1). It can build capture
   weights from a skeleton, move weights between meshes by nearest point, and fit one mesh onto another with hand-placed
   landmarks. It cannot find the joints.
2. No learned auto-rigger outputs the Arma skeleton (section 1.3). All of them need PyTorch, mostly a CUDA GPU, and none
   of that exists in hython. They can only be an optional external step, later.
3. The best first fit is plain geometry: a few landmarks, then a rigid fit down the Arma bone hierarchy. It needs only
   numpy (section 1.5).
4. Weight transfer by closest point plus Laplacian inpainting (Abdrashitov et al. 2023) runs in hython with numpy only.
   It takes about 2.5 s for 6k points and 5 to 9 s for 12k points (section 2.3). scipy is **not** in hython.
5. **A fit must come before a transfer.** With the target scaled by only 2 %, the heaviest bone changes on 9 % of the
   points (section 2.3). Transfer quality is set by the fit, not by the transfer.
6. Unposing works well when the fit is good and badly when it is not. With a good fit the mesh lands within 1.1 cm of
   the true rest position (mean 0.2 cm). With a fit that is off by about 1 degree and 1 % per bone the error is 1.4 cm
   on average and up to 2.9 cm, and the weights are not the cause (section 2.6).
7. A rigid accessory that straddles a joint needs **one** bone. Per-point weights stretch it (edges 0.68 to 1.27 of
   their length at a 45 degree elbow). Picking the wrong single bone moves it as a whole (section 2.6).
8. The Arma limits are: at most 4 bones per point, weights in steps of 1/254 (not 1/255), only selections named like
   bones are weights (section 3).
9. The plan has four phases (section 6.3). Phase 1 needs no fit and already gives a usable export path.

**What the owner must decide** is in section 6.5 (numbered open questions).

## 0.1 Owner decisions so far (D36 and D37, 2026-11-04)

The owner answered the 14 open questions of section 6.5 in D37 (`decision show D37`). Each answer is recorded under its
question there, and the sections that depend on it now say the decided behaviour.


These come from `python .aesir-pkg/aesir_run.py decision show D36` and task #60. They are fixed inputs to this design.

1. The skeleton comes from a **`model.cfg` the user points at**, not a hard-coded list. There is a **skeleton picker**
   because a `model.cfg` can hold several `CfgSkeletons` classes, and a class can inherit with `skeletonInherit`.
2. `model.cfg` has `pivotsModel`, but that names a binarized game file. So the node also takes the path of an
   **editable pivots `.p3d`**. Default: Arma 3 Samples `TemplateRTM/ManSkeleton_Pivots.p3d` (65 body pivots).
3. **Face bones (the 36 `Face_*` bones and the eyes) are out of scope for now.**
4. **Layered weighting** with three group fields:
   - *first pass*: base clothing, full transfer from the sample body;
   - *rigid accessories*: each connected piece gets its weights from the first-pass points it sits on, as one dominant
     bone or one fixed blend per piece;
   - *non-rigid accessories* (bandoliers, cables): per-point transfer from the first pass, smoothed along the piece.
5. A **visualiser** that highlights problems.
6. **Fit, rig, unpose.** Adjust the imported Arma skeleton (and/or the sample body) to the user's mesh. Transfer the
   weights there. Then return the mesh to the Arma rest pose for export, so that it sits on Arma's fixed pivots. Going
   back to the standard skeleton should fix bone-length differences. Find out how well that works and where it
   distorts (joints, rigid accessories). Section 2.6 answers this with numbers.
7. Research and design come first (#60). Build phases follow after the owner reviews this document.

Constraints from the project: the add-on is GPL-3.0-or-later; it runs in Houdini Indie 22.0.466 (Python 3.13, numpy
2.3.2 in hython); the Arma 3 Samples must not be shipped (their readme allows use "for non-commercial game content for
PC game Arma 3" and forbids converting them for another game or engine, `Test_Character_01/readme.txt:7-22` in the
Samples). So the user points at their own Samples folder.

## 1. Auto-rigging and alignment survey

**The core idea.** The Arma skeleton is fixed: same bones, same names, same 65 body pivots. We do not need a rigger that
invents a skeleton. We need to find where **these** joints sit inside the user's mesh. There are two routes:

- *Route A, skeleton to mesh.* Place the pivots inside the user mesh (landmarks, then a rigid fit down the hierarchy).
- *Route B, mesh to mesh.* Deform Bohemia's sample body so it matches the user mesh (non-rigid registration). The
  sample's skeleton and weights come along. Weight transfer is then easy, because the two meshes lie on top of each
  other.

Route B also gives the fit for free: the fitted sample body tells us where every pivot belongs.

Facts below are checked in hython 22.0.466 or cited. A claim I could not check is marked **(unverified)**. Several web
pages were read through a fetch tool that returns a summary written by a small model, so exact licence text should be
re-read before we depend on it. The Bohemia wiki returned HTTP 403 to that tool. The wiki facts in section 3.1 were
afterwards checked in a browser by the advisor (2026-11-04) and are marked "wiki, verified".

### 1.1 Houdini 22.0.466 native tools (criterion 1a)

Sources: the help in `Houdini Indie/houdini/help/nodes.zip` (`nodes/sop/<name>.txt`) and `character.zip`
(`kinefx/*.txt`, `autorigs.txt`), read with `zipfile`; node type names from
`hou.sopNodeTypeCategory().nodeTypes()`; cook tests in `scripts/spikes/rigging/houdini_node_survey.py` and
`houdini_node_cooks.py` (output: `docs/design/evidence/rigging/houdini_node_survey.out`, `houdini_node_cooks.out`).
KineFX and APEX types carry a namespace: the type name is `kinefx::skeleton`, not `skeleton`.

**Headline: there is no automatic joint placement for an arbitrary mesh in Houdini 22.0.** I searched all node names and
the help text for "landmark", "joint placement", "place joints", "skeleton from geometry". Every joint-placement tool
is interactive (a viewer state). All the tools below cook in Indie, headless.

| Type name | What it does | Places joints by itself? |
|---|---|---|
| `kinefx::skeleton` | Draw or edit a SOP skeleton in a viewer state. Joint Placement: View Based (middle of the geometry), Normal Based, Freehand, Surface (`nodes/sop/kinefx--skeleton.txt`). | No, only by clicks |
| `apex::autorigbuilder` (since 21.0) | Viewer state: drag rig components (Root, Spine, Arm, Leg, Hand, Neck/Head) onto a guide skeleton. Templates "adapt to skeletons with different proportions". Builds a control rig, not capture weights. | No, guides are moved by hand |
| `kinefx::biped_setup` (since 22.0) | Tags a biped skeleton and builds the T-pose that `kinefx::biped_retarget` needs. Maps by joint name; templates in `$HFS/packages/kinefx/biped_templates/templates.py` (SideFX, Mixamo, HumanIK). Errors on a 3-joint chain ("Characterization is missing required key <C_pelvis> ..."). | No, maps existing joints |
| `kinefx::rigpose` | Pose a SOP skeleton. Post-Multiply "is useful for reproportioning an input skeleton". | No |
| `kinefx::rigmatchpose` | Poses two skeletons so their rest poses conform (retarget prep). Viewer state. | No |
| `kinefx::mappoints` | Stores a reference from points of input 1 to points of input 2; clicked in a viewer state. | No |
| `kinefx::skeletonblend::2.0`, `kinefx::fullbodyik`, `kinefx::rigdoctor`, `kinefx::skeletonmirror`, `kinefx::rigmirrorpose`, `kinefx::orientjoints` | Blend poses, IK retarget, repair names and hierarchy, mirror, re-orient. | No |
| `kinefx::jointcapturebiharmonic` | Capture weights from a skeleton: tetrahedralises the closed skin, solves a biharmonic field. "Works well for softer, non-mechanical, organic deformation." Needs Intel MKL (help note). 3.1k points: 1.9 s. | No, joints must exist |
| `kinefx::jointcaptureproximity` | Capture weights by distance to joint lines. 0.02 s. | No |
| `kinefx::jointcapturepaint` | Interactive painting of capture weights. Not cooked headless. | No |
| `kinefx::capturepackedgeo` | Rigid weighting: whole packed pieces get 100 % to one joint. This is the stock tool for rigid accessories. | No |
| `kinefx::jointdeform`, `bonedeform` | Skin deformation from `boneCapture` and a skeleton. `jointdeform` is 32-bit only. | - |
| `captureattribunpack`, `captureattribpack` | Convert `boneCapture` between the packed and the array form (section 4.1). | - |
| `topolandmark` + `topotransfer` (since 18.5) | Non-rigid deformation of a template mesh onto a target, with optional hand-placed landmark pairs. The template must be one manifold component. Needs MKL. Landmarks can also come as point data on input 3 (help; not measured). | No, landmarks are manual |
| `attribtransfer`, `attribblur` | Point-to-point attribute copy; iterative attribute diffusion (usable as a fill). | - |
| `kinefx::dembones_skinningconverter::1.0` | Dem Bones: fit skinning to a mesh sequence. | No |
| `ml_*` (ML Deform, ML Pose Generate, ...) | Machine-learning deformers (22.0). None predicts joints or weights from a static mesh. | No |
| "Autorigs (Deprecated)" | Python panel with guide objects you position inside the mesh. | No |

`weightarrayinterpolate` and `weightarraybiharmonic` exist but need an "index array attribute"; `weightarraybiharmonic`
failed to cook on made-up data ("Index array attribute missing"). Not pursued.

Not in 22.0.466: no joint predictor, no landmark detector, no body-model fit, no skin-weight transfer SOP. `attribsmooth`
does not exist. Houdini's own weight tools therefore cover two of our steps (capture from a skeleton, nearest-point
copy) and none of the fitting.

**Python modules inside hython 22.0.466** (checked): numpy 2.3.2 and PySide6 6.8.3 import. scipy (also `.sparse`,
`.linalg`, `.spatial`), torch, sklearn, igl, trimesh, open3d, pymeshlab, numba, cv2, networkx and onnxruntime do **not**.
So any heavy method is numpy-only, a Houdini node, or an external process.

### 1.2 Template and registration methods (criterion 1b)

| Method | Fits the fixed Arma skeleton? | Licence | Dependencies in hython 22 | Standard body | Other proportions |
|---|---|---|---|---|---|
| **b1. Per-bone rigid fit down the hierarchy** from landmarks | Yes, it uses our own hierarchy | ours | numpy | Good | Good for lengths and limb direction; cannot fit soft shape (belly, thick thighs) |
| **b2. Non-rigid ICP** (Amberg et al. 2007) of the sample body onto the user mesh | Yes, through the sample's binding | paper; reference code (MATLAB, Nash) licence not checked | one sparse least-squares solve per step; no scipy, so our own conjugate gradient in numpy (the spike CG works, section 2.3) | Good | Good if started from a rough pose and scale match |
| **b3. Deformation transfer** (Sumner and Popovic 2004) | Helper only | paper | sparse solve | n/a | n/a; its marker-correspondence step is the same as b2 |
| **b4. Pinocchio** (Baran and Popovic 2007) | Yes: embeds a given skeleton (`-skel`, `-nofit`) | core library LGPL (version not read), demo UI MIT | separate C++ binary | Risky on gear and non-watertight meshes (unverified) | Designed for it |
| **Houdini Topo Transfer** | Via the sample body | Houdini | built in (MKL) | Halves the template-to-target distance, no landmarks (spike) | Same |

Sources:

- b1: standard practice, no single source **(unverified)**.
- b2: Amberg, Romdhani, Vetter, "Optimal Step Nonrigid ICP Algorithms for Surface Registration", CVPR 2007,
  doi:10.1109/CVPR.2007.383165, https://shapemodelling.cs.unibas.ch/gravis-literature/publications/2007/CVPR07_Amberg.pdf .
  Abstract (via search summary): the framework "allows the use of different regularisations, as long as they have an
  adjustable stiffness parameter"; the stiffness is lowered so "the template moves from global to local deformations".
  Reference code: https://github.com/charlienash/nricp . Known weakness (from memory, **unverified**): with very
  different poses it can fold limbs together, so it needs a pose pre-alignment.
- b3: Sumner and Popovic, "Deformation Transfer for Triangle Meshes", ACM TOG 23(3), 2004,
  https://people.csail.mit.edu/sumner/research/deftransfer . The user gives "a small set of vertex markers".
- b4: Baran and Popovic, "Automatic Rigging and Animation of 3D Characters", ACM TOG 26(3), 2007,
  https://people.csail.mit.edu/ibaran/papers/2007-SIGGRAPH-Pinocchio.pdf . Given "a static character mesh and a generic
  skeleton, the method adapts the skeleton to the character" (search summary). Code mirror:
  https://github.com/wjk17/Pinocchio (README: library "distributed under the GNU LGPL", DemoUI MIT, `-skel file`,
  `-nofit`). A full port to numpy would be a big job; not recommended.
- Spike for Houdini's Topo Transfer: `scripts/spikes/rigging/weight_transfer_houdini.py`, block N5 in
  `docs/design/evidence/rigging/weight_transfer_houdini.out`: distance from each sample point to its true counterpart
  went from mean 0.047 m to 0.020 m (max 0.086 to 0.071 m); the weight error after transfer fell from L1 0.424 to
  0.336. It needs one connected component, so it ran on the 3103-point main body.

### 1.3 Learned auto-riggers (criterion 1c)

| Method | Own skeleton or fits a given one | Code licence | Runtime |
|---|---|---|---|
| UniRig (SIGGRAPH 2025) | Own skeleton (autoregressive); skinning takes a skeleton as input | MIT (repo LICENSE and Hugging Face card) | CUDA GPU over 8 GB, PyTorch 2.3.1 or newer, Python 3.11, spconv, torch_scatter, torch_cluster, flash_attn, numpy 1.26.4 |
| RigNet (2020) | Own skeleton, joint count varies | GPL-3.0; weights licence not stated | Python 3.7, PyTorch 1.12, CUDA 11.3, torch-geometric, open3d 0.9.0 |
| Make-It-Animatable (CVPR 2025) | Fixed Mixamo-style structure (joint count not confirmed) | MIT | PyTorch, Python 3.11; inference GPU need not stated |
| TARig (2023) | Adapts a humanoid template | GPL-3.0 (built on RigNet) | same old stack as RigNet |
| HumanRig (2024) | Mixamo skeleton, T-posed humanoids | not found | not stated |
| Neural Blend Shapes (2021) | Takes a prescribed skeleton | BSD/MIT mix | PyTorch |

Sources: UniRig https://github.com/VAST-AI-Research/UniRig and https://huggingface.co/VAST-AI/UniRig ; RigNet
https://github.com/zhan-xu/RigNet ; Make-It-Animatable https://github.com/jasongzy/Make-It-Animatable , paper
https://arxiv.org/abs/2411.18197 , page https://jasongzy.github.io/Make-It-Animatable/ ; TARig
https://github.com/jingma-git/TARig-1 ; HumanRig https://arxiv.org/abs/2412.02317 ; Neural Blend Shapes
https://github.com/PeizhuoLi/neural-blend-shapes .

What matters for us:

- **None predicts the Arma skeleton.** The nearest are Make-It-Animatable and HumanRig (Mixamo skeleton) and Neural
  Blend Shapes (prescribed skeleton, but it predicts weights, not joint positions, as far as I read).
- **Landmark use.** Make-It-Animatable is the only candidate whose output joints map by name (head, neck, shoulders,
  elbows, wrists, hips, knees, ankles, fingers) to Arma pivots. Its joints could be landmarks for our rigid fit. Whether
  its skeleton is always the Mixamo one, and its joint count, are **not stated** in what I read.
- **Runtime.** All need PyTorch, most a CUDA GPU. numpy 2.3.2 in hython conflicts with UniRig's `numpy==1.26.4`. None of
  it can run inside hython. It could only be an **optional external step**: export the mesh, run a separate Python,
  import a skeleton file. Heavy, Linux-oriented, not verified end to end.
- **Licences.** The code licences (MIT, BSD, GPL-3.0) are compatible with GPL-3.0-or-later for an external tool we do
  not bundle. The licences of the model weights and training data (Mixamo animations, Objaverse) are mostly **not
  stated**. We would not redistribute them.
- **Quality on Arma uniforms** (vests, helmets, packs) is untested by their authors (inference, **unverified**).
- UniRig's skinning checkpoint status is unclear (the README and the Hugging Face card disagree).

### 1.4 Commercial tools, as workflow reference only (criterion 1d)

- **Mixamo auto-rigger.** The user places markers on chin, wrists, elbows, knees and groin, roughly 11 clicks with left
  and right. Sources: https://forum.reallusion.com/PrintTopic284483.aspx (chin, elbows, wrists, knees and groin),
  https://www.cgchannel.com/2012/06/mixamo-makes-auto-rigger-free-for-low-poly-meshes (older version),
  https://www.tripo3d.ai/blog/rig-ai-generated-character-for-mixamo ("groin at the hip joint center"; "even 1cm off can
  cause animation issues"). No official Adobe page was found for the exact step order. Lesson: **a handful of landmarks
  is enough to fit a fixed skeleton**, and chin and groin are the error-prone ones, so show them clearly and snap them
  to the mesh.
- **Reallusion AccuRIG** (free). It guesses joints and lets the user adjust them: "the manual joint definition to
  customize models with different characteristics" (https://80.lv/articles/reallusion-released-free-automatic-rigging-tool-accurig/).
  I could not verify its landmark count. Lesson: **guess, show, let the user correct**, with symmetry.

### 1.5 Recommendation

| | Approach | Why |
|---|---|---|
| **First (build phase 2)** | Landmarks (auto-guessed, user-correctable, about 11 to 13) then a **rigid fit of the sample body and the 65 pivots down the Arma hierarchy**; optional **Houdini Topo Transfer** as a light non-rigid refinement; then transfer and unpose. | Only numpy and built-in nodes. The same workflow users know from Mixamo. It uses our fixed hierarchy. The user can correct each step. Quality is as good as the landmarks. |
| **Later** | An own **non-rigid ICP** (Amberg 2007) of the sample body onto the user mesh, started from the first approach's pose. Optionally an **external Make-It-Animatable import** to auto-guess the landmarks. The owner has an NVIDIA RTX 50-series
GPU (about 20 GB VRAM, 64 GB RAM), so an optional external learned-landmark step is acceptable later (D37 Q10). | NICP removes most manual landmarks and fits different proportions. Make-It-Animatable is the only learned tool found whose joints map by name to our bones. |
| Not recommended | UniRig, RigNet, TARig (own skeleton, old or heavy stacks). A full Pinocchio port (big job, assumes a watertight mesh). | |

Decided inputs (D37): the user mesh is in an **A-pose** (Q5), it is an **adult humanoid** (Q7), landmarks are **point
groups** on the mesh (Q11), and the Arma skeleton is **never changed for body shape**, female shapes included (Q7): the
fit moves the sample body and the joints inside the user's mesh, and the unpose brings the mesh back onto the fixed
Arma pivots. The incoming mesh carries no joint data (Q6).

The first approach does not need a fit for the first build phase (section 6.3, phase 1). A user who already has a mesh
in the Arma rest pose, made from the sample, can go straight to a transfer (section 2.3 shows how fast it degrades with
misalignment).

## 2. Weight transfer method and unpose (criterion 2)

### 2.1 The method

Abdrashitov, Raichstat, Monsen, Hill, "Robust Skin Weights Transfer via Weight Inpainting", SIGGRAPH Asia 2023
Technical Communications, DOI 10.1145/3610543.3626180, preprint
https://www.dgp.toronto.edu/~rinat/projects/RobustSkinWeightsTransfer/preprint.pdf , page
https://www.dgp.toronto.edu/~rinat/projects/RobustSkinWeightsTransfer/index.html . Official code (MIT, uses libigl
bindings): https://github.com/rin-23/RobustSkinWeightsTransferCode . We write our own numpy version and credit the paper.

1. *Input* (paper section 3): a skinned source mesh and an unskinned target. "We assume that two meshes are aligned."
2. *Closest point match* (3.1): for each target point find the closest point on the source surface. It is a match if the
   distance is at most `D` and the normal angle at most `Theta`. Defaults: "D = 0.05 * d_bbox and Theta = 35 degrees",
   with `d_bbox` the target's bounding-box diagonal. Weights at a match are barycentric.
3. *Inpainting* (3.2, 3.3): for the rest, solve for weights with the matched rows fixed, minimising a Laplacian-based
   energy, with rows summing to 1. Non-negativity is not enforced: "the resulting weights are only slightly negative (>=
   -0.03) so in practice, we simply clamp the negative weights to 0 and re-normalize." One sparse solve per bone, using
   the cotangent Laplacian.
4. *Artist control* (3.4): the user may force points into the "inpaint" set. Our layered groups use this idea.
5. *Limits* (section 5): cannot guarantee no interpenetration; assumes the clothing is connected (separate pieces are
   future work). The paper does not cap influences per point; real-time rigs have "approximately 4-12". We cap to 4
   afterwards (section 3).

### 2.2 Houdini-native alternatives (spike results)

All in `scripts/spikes/rigging/weight_transfer_houdini.py`; output `docs/design/evidence/rigging/weight_transfer_houdini.out`.

| Route | Result (target A, scaled 5/10/15 %, 3125 pts) | Time |
|---|---|---|
| Attribute Transfer SOP on 65 float attributes (one per bone), 1 source point | L1 mean 0.383, heaviest bone changed on 21.6 %, no unweighted point, no point over 4 | 0.04 s |
| Same, blend of 4 source points | L1 mean 0.390; 60 points over 4 | under 0.01 s |
| Attribute Transfer on the `boneCapture_index` / `boneCapture_data` arrays | The nearest point's arrays are copied whole. Two source points cannot be blended. `captureattribpack` accepts the result. | 0.00 s |
| VEX wrangle: `xyzdist` and `primuv` per bone, plus a normal check (same thresholds) | accepted 1585, rejected 1540; accepted-only L1 0.324 | 0.12 s (12.5k points: 0.02 s) |
| Attribute Blur SOP on the rejected points as the fill, 200 iterations | whole result L1 0.454 (numpy CG fill on the same points: 0.434) | 1.6 s (numpy 0.5 s) |
| Joint Capture Proximity from the pivots skeleton alone (no sample weights) | L1 0.759, 35.5 % wrong dominant bone | 0.02 s |
| Joint Capture Biharmonic from the pivots skeleton alone | L1 0.482, 25.6 % wrong dominant bone, 253 points over 4 | 2.4 s |

Findings:

- The closest-point step is cheap natively (VEX, 0.02 s for 12k points times 65 bones). Attribute names must be assigned
  to a typed float first (`float w = primuv(...)`), or VEX reports an ambiguous `setpointattrib`.
- The inpainting step has a native substitute (Attribute Blur on a group) and a numpy one (exact, 0.2 to 5 s). Both work.
  The numpy conjugate gradient matches the paper and gives a cotangent Laplacian.
- Attribute Transfer needs no thresholds and never leaves a hole, but never rejects a wrong match either.
- Weights from the skeleton alone (Joint Capture Biharmonic) are 0.48 L1 away from Bohemia's hand weights. A fit-based
  transfer on an aligned mesh is 0.06 (section 2.3). So transfer beats skeleton-only weighting once the sample body is
  near. Biharmonic stays useful as a **fallback for parts with no sample counterpart** and as an independent check.
- Topo Transfer with no landmarks halved the template error and cut the transfer error by about 20 % (block N5).

### 2.3 Spike: closest point plus numpy inpainting

`scripts/spikes/rigging/weight_transfer_numpy.py`, hython, output `docs/design/evidence/rigging/weight_transfer_numpy.out`.
The method is re-implemented from the paper's description (no code of the authors). Closest point: Ericson, Real-Time
Collision Detection 5.1.5, 24 candidate triangles per query chosen by centroid distance (checked against brute force:
none of 200 sampled queries missed). Inpaint: cotangent Laplacian with weights clamped positive, Jacobi-preconditioned
conjugate gradients on all 65 columns at once, no scipy.

Source: sample Resolution 1, without the 11 proxy triangles (the 22-point insignia piece stays): 3125 points, 6229
triangles, 65 bones, bounding-box diagonal 2.163 m. Targets are made from the sample, so the truth is known:
A = same topology, scaled `(1+m/2, 1+m, 1+1.5m)` about the centre, plus 2 mm noise; B = Resolution 2, subdivided once;
C = Resolution 1, subdivided once. Error L1 = sum over bones of |w - truth| (0 = same, 2 = fully different).

| Target (distance limit 5 % of the diagonal, normal limit 30 degrees) | Points | Matched / inpainted | Total time | L1 mean / median / p95 | Heaviest bone changed | Over 4 bones |
|---|---|---|---|---|---|---|
| A, m = 0 (noise only) | 3125 | 2886 / 239 | 0.75 s | 0.064 / 0.018 / 0.32 | 5.1 % | 12 |
| A, m = 0.02 | 3125 | 2377 / 748 | 0.82 s | 0.139 / 0.066 / 0.59 | 9.0 % | 93 |
| A, m = 0.05 | 3125 | 1955 / 1170 | 1.00 s | 0.289 / 0.166 / 1.06 | 15.3 % | 495 |
| A, m = 0.10 | 3125 | 1690 / 1435 | 1.02 s | 0.423 / 0.288 / 1.36 | 23.2 % | 721 |
| B, m = 0.10 | 6260 | 3411 / 2849 | 2.44 s | 0.414 / 0.297 / 1.27 | 22.3 % | 1556 |
| C, m = 0.10 | 12476 | 6156 / 6320 | 7.15 s | 0.406 / 0.276 / 1.28 | 21.8 % | 2556 |

Reading the numbers:

1. **The error comes from misalignment, not from the algorithm.** m = 0.10 moves points up to 8.6 cm. Even m = 0.02 (about
   1.7 cm at the extremes) changes the heaviest bone on 9 % of the points. The plain nearest-vertex copy is just as
   wrong (L1 0.38 to 0.40). **A fit must come first.**
2. **Thresholds help only on an aligned mesh.** At m = 0.10 the thresholds rejected 46 % of the points, and the copied
   rest was not more accurate than the plain copy (0.32 against 0.38).
3. **Speed.** About 10k points take 2.4 s (6k) to 7 s (12k) in hython with numpy only. The closest-point search is
   1 to 2.3 s of that (a numpy stand-in for a bounding-volume tree), the solve 1.3 to 6.7 s. An earlier run of the same
   script took 5.5 s for 12k; the machine load differs. With `scipy.sparse.linalg.cg` (system Python only) the solve was
   about 10 times faster, but scipy is not in hython. It is fine for a one-shot node. The native VEX closest-point step
   is roughly 50 times faster and can replace the numpy search in a later optimisation.
4. **The inpainted fill produces many points over 4 influences** (1125 of 3125 at m = 0.10). Pruning to 4 changes a
   point by L1 0.053 on average and up to 1.2. So the clean-up must report what it changed (section 4.4) and the
   visualiser should show the points before the clean-up (section 5).
5. Snapping to the file's weight grid keeps the sum within 1/254 of 1 (max 0.0039). Section 3.2 shows how to make it
   exact.

Limits of this spike: the targets are the sample itself, scaled. They show the reaction to misalignment, not the result
on a real third-party character (other topology, clothing layers, spread fingers). L1 overstates the visible error where
bones move alike (`LeftArm` and `LeftArmRoll`). A pose test (section 2.6, phase 4) is the right measure.

### 2.4 Recommended method

1. **Fit first** (section 1.5).
2. **Closest point and normal check** in VEX or numpy, with the paper's defaults `D = 0.05 * d_bbox` and
   `Theta = 35 degrees` as parameters.
3. **Inpaint the rest** in numpy (the spike CG), clamp negatives, renormalise.
4. **Clean up for export** (section 4.4): keep the 4 heaviest, renormalise, snap to the file grid, report.
5. **Fallback** for parts with no counterpart in the sample (a shoulder bag, a tail): Joint Capture Biharmonic from the
   fitted skeleton, or `kinefx::capturepackedgeo` (rigid).

Where the code lives: pure numpy in a new hou-free package `a3ob_core/rig/` (decided, D37 Q14: it may need numpy; it
exists since task #61), node glue in
`python/a3ob_houdini/nodes/`, as the existing nodes do (`docs/design/ux-tools.md` section 1).

### 2.5 Layered weighting, how it maps to the method

| Layer (group field) | Source of the weights | Mechanism |
|---|---|---|
| First pass (base clothing) | The fitted sample body | Sections 2.1 to 2.4. Everything not in the other two groups. |
| Rigid accessories | The first-pass points the piece sits on | Per connected piece: take the first-pass weights of the nearest first-pass points (distance limit), average them (area weighted), then either **one dominant bone** (weight 1.0) or **one fixed blend** (the average, same for every point of the piece). Paper idea 3.4, at piece level. A user override picks the bone. Twist (`...Roll`) weight goes to the next non-twist bone when the dominant bone is chosen (D37 Q12). |
| Non-rigid accessories | The first-pass points they sit on | Per point: transfer from the first pass, then smooth along the piece only (Laplacian smoothing on the piece's own edges, a few iterations). The paper assumes connected pieces (section 5 of the paper), which fits. |

Order: first pass, then the other two (they read the first pass). A piece is a connected set of primitives (by
connectivity) inside its group. Pieces of fewer than 10 points are not skin and are ignored by the spikes (the sample's
proxy triangles).

### 2.6 Fit, rig, unpose: how well does it work, and where does it distort

Spike `scripts/spikes/rigging/unpose_distortion.py` (hython; output `docs/design/evidence/rigging/unpose_distortion.out`).

*What it does.* The "user mesh" is the sample body subdivided once (12476 points, so a different topology), posed with
linear blend skinning by a known fitted skeleton. The true Arma rest positions are therefore known. Then it does what
this design does: transfer the weights from the fitted sample body (section 2.3 method), run the export clean-up (4
bones, file grid), unpose with the inverse of each point's blended matrix (fitted skeleton to Arma pivots), and compare
with the true rest position. The pivots come from `ManSkeleton_Pivots.p3d`, the 64 bones with a pivot from `model.cfg`.
`weapon` and `launcher` have weights in the sample body but no pivot; they are left out of the skeleton.

*Scenarios.* A: arms 20 degrees down, legs 8 degrees apart. B: arms 10 % longer, legs 8 % longer. C: both, plus the whole
body 5 % bigger. D: C, but the skeleton used to unpose is off by about 1 degree and 1 % per bone (random). E: C, but off
by about 2.5 degrees and 2 %.

| Scenario | Error with the true weights (mean) | Error with transferred, cleaned weights: mean / p95 / max | Joint zones mean | Worst region (mean) |
|---|---|---|---|---|
| A pose only | 0.03 cm | 0.18 / 0.54 / 0.87 cm | 0.24 cm | leg 0.30 cm |
| B lengths only | 0.01 cm | 0.20 / 0.61 / 1.08 cm | 0.28 cm | leg 0.30 cm |
| C pose, lengths, size | 0.03 cm | 0.18 / 0.55 / 0.88 cm | 0.25 cm | leg 0.31 cm |
| D fit off by about 1 deg, 1 % | 1.43 cm | 1.43 / 2.52 / 2.92 cm | 1.30 cm | foot 2.12 cm |
| E fit off by about 2.5 deg, 2 % | 3.58 cm | 3.56 / 6.39 / 7.39 cm | 3.22 cm | foot 5.30 cm |

("Joint zones" = points whose second-largest weight exceeds 0.25.)

What this says:

1. **With a correct fit, unpose works.** Bone-length and size differences (10 %, 8 %, 5 %) are removed to a mean of 0.2 cm
   and a worst point of 1.1 cm. The pose moved points by 13 cm on average and up to 26 cm.
2. **The weights add 0.15 to 0.2 cm; joint zones are slightly worse** (0.24 to 0.28 cm against 0.14 to 0.16 cm elsewhere),
   and legs and arms are worse than the torso.
3. **The fit decides everything.** In D and E the error with the true weights equals the error with the transferred
   weights. The unpose can only remove what the fitted skeleton describes. Errors add up down the chain, so hands and
   feet suffer most (D: hands 1.9 cm, feet 2.1 cm; E: hands 4.8 cm, feet 5.3 cm). A fit worse than about 1 degree and
   1 % per bone costs more than the weights ever do. This is the argument for showing the fit clearly and letting the
   user correct it.
4. **Local shear remains.** Edge lengths after unpose against the true edges: p1 0.75 to 0.80, p99 1.4 to 1.55 for good fits (median
   1.000). A wrong weight shears a patch even when the average error is small. The pose stress test (section 5) is
   there to find these patches.
5. **A bad fit also hurts the transfer**: in D and E the source and target no longer match, so only 8202 and 6885 of
   12476 points pass the match thresholds (12144 in A), and 3381 and 4459 points need more than 4 bones before the
   clean-up (72 in A).

*Rigid accessory test.* A "cuff" of the body points within 7 cm of the left elbow, pushed 1.5 cm out, glued to
`LeftForeArm`, with the elbow bent 45 degrees. First-pass weights of the cuff points: `LeftArmRoll` 0.73, `LeftForeArm`
0.24, `LeftArm` 0.03. Offset from the true rest position (the point cloud is a rigid piece, so the truth is its rest
shape) and edge-length ratio after unpose:

| Way to weight the piece | Mean offset | Max offset | Edge length ratio p1 / median / p99 |
|---|---|---|---|
| Per-point first-pass weights | 2.60 cm | 5.25 cm | 0.676 / 0.964 / 1.271 |
| One dominant bone (the average picks `LeftArmRoll`) | 3.26 cm | 5.39 cm | 1.000 / 1.000 / 1.000 |
| One fixed blend (the piece average) | 2.59 cm | 4.30 cm | 1.001 / 1.034 / 1.059 |

1. **Per-point weights stretch a rigid piece** (edge lengths after the unpose run from 0.68 to 1.27 of the true length, p1 to p99, at this bend). That is why rigid pieces get their own layer.
2. **A single bone keeps the shape exactly**, but only the right bone returns it to the right place. The average picked
   `LeftArmRoll`, which does not bend with the forearm, so the piece moved as a whole by 3.3 cm. The correct pick here
   is a judgement the algorithm cannot make (the cuff sits on the elbow). So: allow an override bone per piece, and
   show the pieces that straddle two bones (section 5).
3. **A fixed blend** keeps the shape within about 3 to 6 % and the offset is as small as the per-point result. It is
   the safer default when two bones are close to equal. It is not exact.

Caveats of this spike: the "user mesh" is the sample itself, so its skin is exactly consistent with the weights (best
case). The fit error is random noise, not the error pattern of a real landmark fit. Linear blend skinning is assumed for
both the posing and the unpose; the engine's skinning method is not documented in any source I could read **(unverified)**.
The cuff truth is an assumption (rigid on `LeftForeArm`).

## 3. Arma rules and the A3OB rigging inventory (criterion 3)

### 3.1 Rules

The Bohemia wiki pages returned 403 to the fetch tool. The advisor then read them in a browser (2026-11-04); the
facts marked "wiki, verified" below are theirs. The forum post is still only a search summary.

| Rule | Source | Our rule |
|---|---|---|
| **At most 4 bones per point.** | BI wiki "Arma 3: Modding Characters" (wiki, verified; https://community.bistudio.com/wiki/Arma_3_Modding_Characters): "use max 4 bones per vertex". The page recommends transferring the skinning from the example character (3ds Max Skin Wrap), which is the same idea as ours. It also says helmets and glasses need an empty View Pilot LOD, and a chin strap can be skinned to `Face_Jawbone`. A3OB docs: it can select points "assigned to more than 4 bones" and "prune the weakest" (https://mrcmodding.gitbook.io/arma-3-object-builder/tools/rigging); code `utilities/rigging.py:72` (select `bones > 4`) and `:75-105` (keep top 4 by weight). The sample never exceeds 4 (table below). One forum user says Arma ignores the extra bones and writes to the RPT log (a forum post seen only in a search summary, unverified, https://forums.bohemia.net/forums/topic/207475-advice-on-texturing/?page=2). | Hard limit 4. Prune the weakest, renormalise. What the engine does with a 5th bone is not stated by any source I could read. |
| **Weight steps.** | The wiki page "P3D Named Selections" is unofficial: its formulas are "found by experimenting" and give conflicting decodes (wiki, verified; https://community.bistudio.com/wiki/P3D_Named_Selections). The real files settle it. A3OB `data_p3d.py` `decode_weight` is `(255 - b) / 254`, and the sample's Resolution 1 has 201 distinct weight bytes with `b = 2` never used, which rules out the page's 1 % step variant. Our reader agrees: byte 1 is 1.0, byte 255 is a member with weight 0.0 (`python/a3ob_core/p3d/model.py:104-124`, `decode_weight`, `encode_weight`; data model section 7.1). | The grid is **k/254** with k from 0 to 254, **not 1/255**. The task text says 1/255; the file says 1/254. Because 254 divides evenly, a sum of exactly 1.0 is always possible. |
| **Sums.** | A3OB treats `abs(sum - 1) > 0.01` as unnormalised (`utilities/rigging.py:30`). The sample's sums run 0.984 to 1.0 (table below), and 29 points of Resolution 4 are 100 % `Spine3` and 100 % `Head` at once (sum 2.0). No source says whether the engine normalises. | Normalise to exactly 1 on the grid (section 3.2). This is our rule, not Bohemia's. |
| **Which LODs carry weights.** | Not stated by any source I could read. From the sample (table below). | Follow the sample (section 3.3). |
| **Only bone-named selections are weights.** | A3OB docs: the skeleton is needed "so that only the actual bone selections are affected, and selections meant for hiddenSelections are left alone". `sections[]` in the `model.cfg` lists texture and hidden-selection names, not bones (Samples `Test_Character_01/model.cfg:154-159`). | Never weight a name that is not a bone. Match bone names **case-insensitively** (the sample's selections are `Head`, `Neck`, `Weapon`; the bones are `head`, `neck`, `weapon`; evidence below). |
| **Bone names.** | A3OB `utilities/validator.py:615-683` and `io/data_rtm.py:21-53`: unique (case-insensitive), parent listed before child, ASCII, RTM names under 32 characters. RTM wiki: names are fixed 32-byte fields (https://community.bistudio.com/wiki/Rtm_(Animation)_File_Format). | Same checks in Rig Skeleton. |
| **Mesh joints off the pivots.** | Not stated by any source. | See below. |

**What happens in game when the mesh joints are off the pivots** is not in any source I could read. The mechanism
(inference): the engine rotates each bone about its pivot from the pivots P3D and moves weighted points by the bone
matrix. RTM transforms are absolute matrices in the rest frame (armaio docs, https://armaio.readthedocs.io/latest/rtm/plain/).
If the mesh joints sit elsewhere than the pivots, the points swing about the wrong centre: elbows and knees pinch or
shear and limbs change length. That is the reason for fit, rig, unpose. Section 2.6 measures what the unpose recovers
in Houdini. The in-game look is a check the owner will do in game (D37 Q9, section 6.5). This design has no Arma runtime
to test it.

**Sample facts** (`scripts/spikes/rigging/sample_facts.py`, output `docs/design/evidence/rigging/sample_facts.out`):

- `OFP2_ManSkeleton` in the sample `model.cfg` (`Test_Character_01/model.cfg:9-12`, `isDiscrete = 0`,
  `skeletonInherit = ""`) has 103 bones, 67 without `Face_*` and `Eye*`. Bones are listed as name and parent pairs, root
  parent `""` (`:15 "Pelvis",""`). `pivotsModel` is a game path (`:132`, `A3\anims_f\data\skeleton\SkeletonPivots.p3d`).
  `CfgModels` links `class A3_character_example: ArmaMan {}` and `skeletonName = "OFP2_ManSkeleton"` (`:154-165`).
  `TemplateRTM/model.cfg` has `ManSkeleton` with `pivotsModel="Samples_F\TemplateRTM\ManSkeleton_Pivots.p3d"` (`:126`).
  The BI wiki page "Model Config" (wiki, verified; https://community.bistudio.com/wiki/Model_Config) says:
  `skeletonBones` are `"<bone>", "<parent>"` pairs, with an empty parent for a root; `skeletonInherit` inherits all the
  bones of another skeleton; `isDiscrete = 1` means 0 or 100 % per vertex and `0` means partial weights (smooth
  skinning). It marks `pivotsModel` as "(A2 only)", although the A3 Samples use it (as the sample files above show). The
  Rig Skeleton node does not need it (it reads the editable pivots file). Whether joints off the pivots matter in
  game is for the owner's in-game test (D37 Q9). File naming (same page): a `<foldername>.cfg` takes precedence over
  `model.cfg`, and `<modelname>.cfg` is read alongside. The node reads the one file the user picks, and its tooltip says so.
- `ManSkeleton_Pivots.p3d`: one LOD ("Memory"), 85 points, 65 selections. 64 are `model.cfg` bones (case-insensitive).
  The 65th is `Hips` (two points at the origin), which is not a bone. 56 selections have one point; the others 2 to 7
  points that coincide or sit in a small cluster (`LeftHand`: 4 points at the same place). The pivot is the **first
  point** of the selection (A3OB convention, `io/import_armature.py:23-38`). The bones `Camera`, `launcher`, `weapon`
  have no pivot. Pivots and sample mesh share one space.
- `A3_character_example.p3d`, bone-selection statistics (weights by LOD):

| LOD | Points | Bone selections | Weighted points | Max bones per point | Sum min / max |
|---|---|---|---|---|---|
| Resolution 1 | 3158 | 65 | 3143 | 4 | 0.984 / 1.000 |
| Resolution 2 | 1604 | 65 | 1589 | 4 | 0.984 / 1.000 |
| Resolution 3 | 1045 | 65 | 1033 | 4 | 0.984 / 1.000 |
| Resolution 4 | 487 | 31 | 475 | 4 | 0.984 / 2.000 |
| View - Pilot | 3291 | 65 | 3279 | 4 | 0.984 / 1.000 |
| Shadow Volume 0 | 409 | 65 | 403 | 4 | 0.984 / 1.000 |
| Shadow Volume 10 | 139 | 31 | 139 | 4 | 0.992 / 1.000 |
| Geometry | 42 | 0 | 0 | - | - |
| Memory | 44 | 23 | 43 | 1 | 0.996 / 1.000 |
| Land Contact | 1 | 0 | 0 | - | - |
| Hit-points | 38 | 22 | 38 | 1 | 1.0 |
| Fire Geometry | 1000 | 34 | 1000 | 1 | 1.0 |

  In Resolution 1: 1 bone 809 points, 2 bones 1602, 3 bones 656, 4 bones 76; 15 points unweighted (the 5 proxy triangles
  with no weights). 201 distinct weight bytes. No `Face_*` selection exists in any LOD; the sample weights only 65 body
  bones. `Camera` and `neck1` have no selection. The Geometry LOD has no bone selections (its selections are
  `Component01`...). Memory, Hit-points and Fire Geometry are **rigid, one bone per point**. 11 loose 3-point triangles
  carry proxy positions (6 weighted: `weapon`, `launcher`, `RightHand`, `LeftHand`, `head`, `Spine3`). Other samples:
  `A3_character_template.p3d` has the same 12 LODs (59 to 60 bone selections in Resolution 1 to 3);
  `A3_vest_example.p3d` has 10 to 12 bone selections per visual LOD; `A3_headgear_example.p3d` has one (the head).

### 3.2 What the export must do

1. Only skeleton-bone selections are touched.
2. At most 4 bones per point.
3. Weights on the grid k/254, summing to **exactly 1**: take the weights after pruning and renormalising, scale to 254,
   floor each, then give the missing units to the weights with the largest remainders. (Independent rounding, as in the
   spike, leaves a sum within 1/254; this is exact.)
4. Drop weights that would be 0. (Byte 255, a member of weight 0, is legal but useless for a bone selection; the data
   model keeps it on import, section 7.1.)
5. A point with no weight at all is reported (and optionally filled from its nearest weighted neighbour).

### 3.3 Which LODs to weight

Follow the sample. Weight Resolution 1 to 4, View - Pilot and the Shadow Volume LODs. Memory, Hit-points and Fire
Geometry use one rigid bone per point. The Geometry LOD gets no bone selections. The sample's Resolution 4 and Shadow
Volume 10 use only 31 bones (the main body bones, no fingers). Which LODs the engine really needs is not stated; keep
the sample's pattern (decided, D37 Q8).

### 3.4 A3OB rigging inventory

Root: `.reference/Arma3ObjectBuilder/Arma3ObjectBuilder/` (GPL). All Blender-bound (`bpy`), so we port the logic, not the
code. A3OB has no weight transfer, no fit and no unpose. Files derived from it must name the upstream file and say they
were modified.

| A3OB file | What it does |
|---|---|
| `io/import_mcfg.py` | Reads a `model.cfg` into scene skeletons. `read_mcfg` (:66-93) tries its own parser, else `cfgconvert.exe -bin` and `config.derapify_file`. `get_bones` (:96-104) pairs the flat array. `get_bones_compiled` (:109-126) follows `skeletonInherit` recursively, parent bones first, drops duplicates by lower-case name. `read_file` (:129-174) imports every class under `cfgskeletons`, skipping references. |
| `io/config/` (`tokenizer.py`, `parser.py`, `data.py`, `derapifier.py`) | A pure-Python config parser (`CFGParser`, `parser.py:8`, `parse_array` :108, `parse_property` :133, `parse_class` :198) and a rapified-binary reader. We need a small subset: `CfgSkeletons` and `CfgModels`, string arrays, `//` and `/* */` comments, `class A: B` inheritance. |
| `io/export_mcfg.py` | Writes one `CfgSkeletons` class (`isDiscrete: 0`, `skeletonInherit: ""`, flattened `skeletonBones`, `pivotsModel: ""`, :21-38). No `CfgModels`. |
| `io/import_armature.py` | Builds an armature from a pivots P3D: `read_pivots` (:56-64) uses the Memory LOD; `extract_pivot_coords` (:23-38) maps the lower-cased selection name to its first point; `filter_bones` (:67-76); `fake_pivot_coords` (:41-53) places bones without a pivot in a row unless "Ignore Without Pivot" (default on); `build_bone_hierarchy` (:83-92); `build_bones` (:95-111): head at the pivot, tail at the average of the children, leaf along +Z; `link_bones` (:114-122). |
| `utilities/rigging.py` | `get_bone_group_indices` (:12-14); `select_vertices_unnormalized` (:17-30, tolerance 0.01); `normalize_weights` (:33-56, no zero-sum guard: divides by zero); `select_vertices_overdetermined` / `prune_overdetermined` (:59-105, top 4 by weight); `prune_weights` (:110-129); `cleanup` (:132-137: prune threshold, prune over 4, normalise); `bone_order_from_skeleton` (:144-158); `pivots_from_armature` (:161-189: Memory LOD point mesh, one point per bone head). |
| `ui/tool_rigging.py` | The "Rigging" and "Weight Painting" panels and their operators: skeleton add/remove/validate/from armature/`OFP2_ManSkeleton` preset, pivots from armature, select unnormalised, select overdetermined, normalise, prune, general cleanup. |
| `ui/import_export_armature.py`, `ui/import_export_mcfg.py`, `ui/import_export_rtm.py` | The dialogs. The pivots dialog has the skeleton picker and "Ignore Without Pivot". |
| `io/data_rtm.py`, `io/import_rtm.py`, `io/export_rtm.py` | RTM and BMTR read; plain RTM write. 32-byte bone name plus 12 floats, axis swizzle (:21-53); `RTM_0101` (:115-166); BMTR versions 3 to 5 with LZO (:321-451), relative to absolute with the hierarchy (:296-318). BMTR write is not supported (:453-454). Import compensates the rest matrix (:144). |
| `io/import_p3d.py:114-132`, `io/export_p3d.py:528-555` | Selections to vertex groups and back. Raw weights per point, **no** normalising or pruning on import or export; the user runs the cleanup tools first. |
| `utilities/validator.py:615-683` | Skeleton validation (unique names, parents first, ASCII, RTM name length). |
| `scripts/ofp2_manskeleton.py`, `scripts/vertex_groups_*` | A preset bone list; scripts to lower-case groups and match armature name case. |

What we reuse as ideas: parse `CfgSkeletons` with the `skeletonInherit` merge; the pivot is the first point of the
Memory-LOD selection; head at the pivot, tail at the child average; the name checks; cleanup order (prune, limit 4,
normalise). Gaps we fill: `CfgModels` reading (to map a model class to its `skeletonName`), the zero-sum guard, the
1/254 grid, and all of fit, transfer and unpose. Name case matters for the Object Builder animation preview
(`io/data_rtm.py:155-159`), and the engine matches case-insensitively.

## 4. Houdini data (criterion 4)

Design notes first: coordinates follow the data model (`docs/design/houdini-data-model.md` section 4.3): file `(x, y, z)`
is `P = (x, y, -z)`. The pivots are read through the same map, so the skeleton lies on the imported sample mesh.
Every new attribute below is a **proposal**, named `a3ob_*` (data model D-PREFIX).

### 4.1 KineFX skeleton and the capture format

Facts (`character.zip` `kinefx/skeletons.txt`, `kinefx/capture.txt`; cooks in `houdini_node_cooks.out`):

- A skeleton is points with a `name` string and a `transform` **matrix3** (position is `P`), joined by polyline
  primitives. "The parent-child relationship between the joints in a hierarchy is determined by vertex order."
- A Python SOP can build a skeleton from `name`, `transform` and 2-point polylines; Joint Capture Proximity,
  Joint Capture Biharmonic, Rig Pose and Joint Deform accepted it (spike).
- `boneCapture` is a point attribute of index-pair floats `(index, weight, index, weight, ...)`, padded with `-1, -1`.
  Biharmonic output on a tube: `(2.0, 0.9447, 1.0, 0.0553, -1.0, -1.0)`. The indices point into the detail array
  `boneCapture_pCaptPath`. "The weight values are normalized between the capture regions."
- Unpacked form (Capture Attribute Unpack SOP): point arrays `boneCapture_index` (int) and `boneCapture_data` (float);
  detail `boneCapture_pCaptPath` (joint names), `boneCapture_pCaptData` (20 floats per region: 16 for the inverse rest
  4x4 matrix, 2 taper, 2 min/max; "KineFX only uses the 4x4 matrix"), `pCaptFrame`, `pCaptSkelRoot`. Capture Attribute
  Pack goes back.
- `boneCapture_pCaptPath` held bare joint names (`Pelvis`) in the spike, because the made-up skeleton has no path
  hierarchy.
- Joint Deform matches the capture to the animated skeleton by the `name` point attribute.

**Rig Skeleton output (proposal).**

| Item | Value |
|---|---|
| Points | one per body bone that has a pivot, in `model.cfg` order (parents first) |
| `P` | the pivot (first point of the pivots selection), mapped to Houdini axes |
| `name` | the `model.cfg` spelling of the bone (for example `head`) |
| `transform` | **identity** (world axes) for every joint (proposal). Then a joint's local rotation equals the rotation in an Arma RTM (absolute 4x3 matrices in the model frame), which keeps phase 4 simple. |
| Primitives | a 2-point polyline from each parent to its child |
| Built in task #61 | Exactly the table above: points with `name` and `transform`, polylines parent to child. The `transform` is a 9-float point attribute, which is how a Python SOP can write it; `kinefx::rigpose`, `kinefx::jointcaptureproximity` and `kinefx::jointdeform` accept it (test in `tests/test_node_rig_skeleton.py`). |
| `a3ob_rig_discrete` (detail int) | Decided in task #62: the `isDiscrete` of the chosen `CfgSkeletons` class (1 or 0), written only when the model.cfg says. Weight Cleanup reads it from its second input, so the model.cfg need not be read twice. Absent means 0 (smooth skinning). |
| Not written | The earlier proposals `a3ob_rig_skeleton`, `a3ob_rig_cfg`, `a3ob_rig_pivots` (detail) and `a3ob_rig_pivot_source` (point) are dropped: nothing needs them yet, and bones with no pivot are left out (D37 Q4), so no joint is placed by a rule. |

The 64 bones with a pivot: the spike (`weight_transfer_houdini.py` block N4) built exactly this skeleton from `model.cfg`
and the pivots file; Joint Capture accepted it ("64 joints").

### 4.2 Canonical weight form and the capture form

**Canonical form: the P3D selection form** of the data model (section 7.2): a point group named
`hou.text.encodeAttrib(bone)` plus a point float `a3ob_w_<enc>` with default 1.0, created only if some weight is not 1.
Reason: the P3D export and the Selection browser already work on it; one truth, no second weight store. Native Houdini
nodes (Joint Deform, Joint Capture) need `boneCapture`, so a conversion makes it on demand (section 6.2, Weights Convert).

Mapping, selections to capture (`mode = Selections to Capture`):

1. For each skeleton bone `b`, find the point group whose decoded name equals `b` ignoring case. Two groups that differ
   only in case are an error (the data model warns for this, section 7.3).
2. The weight of point `p` for `b` = 0 if `p` is not in the group; else `a3ob_w_<enc>` of `p` (1.0 if the attribute does
   not exist). Byte 255 on import is weight 0.0: a member that adds no weight (ignored, counted).
3. Build `boneCapture_index` (bone number in the order of `boneCapture_pCaptPath`) and `boneCapture_data` per point,
   weights greater than 0 only.
4. Build the detail arrays: `boneCapture_pCaptPath` from the skeleton `name`s and `boneCapture_pCaptData` (layout below).
5. Run the Capture Attribute Pack verb (`hou.sopNodeTypeCategory().nodeVerb("captureattribpack")`) on the geometry.
   `boneCapture` cannot be written directly: the joint names and `pCaptData` are part of the packed attribute and only
   the verb stores them.

**The `boneCapture` layout (settled in task #62, by cooking Joint Capture Proximity, Capture Attribute Unpack and
Joint Deform).**

| Item | Unpacked form (what the pack verb reads) | Packed form (what Joint Deform reads) |
|---|---|---|
| Per point | point int array `boneCapture_index` (region numbers) and point float array `boneCapture_data` (weights), same length per point, heaviest first (our order; Houdini does not need one) | point float array `boneCapture`: `index, weight` pairs, padded to the longest point with `-1, -1`; a point with no weight is all `-1` |
| Regions | detail string array `boneCapture_pCaptPath`: one joint name per region, in joint (point) order of the skeleton | stored inside the attribute (no detail attribute is left) |
| `boneCapture_pCaptData` | detail float array **with tuple size 20**, one tuple per region, in the order of `pCaptPath` (a flat array of tuple size 1 is not read by the verb and leaves the capture unusable: Joint Deform fails with "Missing Rest Point Transforms") | stored inside the attribute |
| Also | detail float `pCaptFrame` = 0.0, detail string `pCaptSkelRoot` = "" | kept as detail attributes |

The 20 floats of one region: floats 0 to 15 are the **inverse of the joint's rest matrix** as a 4x4, row-major, in
Houdini's row-vector form (`x' = x * M`): the three rotation rows first, then the translation row. For a joint at `P` with
identity `transform` this is the identity with `-P` in the last row (for example `0, -1, 0.5, 1` for a joint at
`(0, 1, -0.5)`). A joint with a rotated `transform` uses the inverse of its rest matrix `[R; P]`
(`a3ob_core.rig.weights.inverse_rest_floats`). Floats 16 to 19 are `1, 1, 1, 1` (taper and range; Joint Capture writes
these neutral values and Joint Deform does not use them). Joint Deform matches regions to the skeleton by the joint
`name`, and **normalises** the weights of a point, so a sum other than 1 does not change a pose; Weight Cleanup is
for Arma, not for Houdini.

Test 1.5 proves it: with these numbers Joint Deform returns every point of the sample Resolution 1 within 1e-5 at the
rest pose, and with one joint turned it equals a numpy linear blend skinning (row-vector rotation, normalised weights)
within 1e-5.

Mapping, capture to selections (`mode = Capture to Selections`): for every region in `boneCapture_pCaptPath` that matches
a skeleton bone ignoring case, create or update the point group and `a3ob_w_<enc>` from the weights. The group name uses
the `model.cfg` spelling (decided, D37 Q3; the node keeps an option to keep an existing spelling). Names that are not bones are left
untouched. Regions of the capture that are not in the skeleton are an error.

The numpy working form inside the nodes is a dense `(points, bones)` matrix `W` built from the canonical form. Memory is
small (12k points times 65 bones is 6 MB of float64).

### 4.3 P3D selections and capture, both ways: round trip

Round trip (acceptance test 1.4): P3D Import (sample, Resolution 1) -> Weights Convert to Capture -> Weights Convert to
Selections -> the point groups and `a3ob_w_*` values equal the import to within 1/254 (they are identical to the byte
once snapped). Non-bone selections (components, proxies, hidden selections) are untouched.

### 4.4 Export clean-up and how it reports what it changed

The Weight Cleanup node (section 6.2) does, in this order, on the skeleton-bone selections only:

1. Drop weights below `threshold` (default 0.01, as A3OB `prune_weights` uses a threshold).
2. Keep the 4 heaviest per point (`max_bones`, default 4).
3. Normalise (guard: a point whose sum is 0 is **not** divided; it is reported).
4. Snap to the grid k/254 with the largest-remainder rule so the sum is exactly 1 (section 3.2).
5. Fill unweighted points from the nearest weighted neighbour, if the option is on (default off, reported).

What it reports (info message, as every A3OB node has a summary line, `docs/design/ux-tools.md` section 2). The numbers below are an illustration, not a measurement:

```
Weight Cleanup: 3125 points. 1125 had more than 4 bones (mean change 0.053, worst 1.2). 14 weights below 0.01 dropped.
Sums were within 0.016 of 1 before, are exactly 1 now. 0 points had no weight.
```

and as data (proposal): point float `a3ob_rig_changed` (L1 change of this point, so the Rig Check can colour it; built in
task #62), point int
`a3ob_rig_infl_before` (bones before the clean-up), detail floats `a3ob_rig_cleanup_*` (counts and statistics). Warnings
for: points that had no weight, a sum of 0 before the clean-up, a pruned point whose change is above `warn_change`
(default 0.3, a parameter). The spike (`export_cleanup` in `weight_transfer_numpy.py`) measured the typical values
(section 2.3).

**As built (task #62).** Weight Cleanup has the parameters `maxbones` (4) and `threshold` (0.01) only. Not built yet, because
no acceptance test needs them: `fill`, `warn_change`, the `group` parameter, `a3ob_rig_infl_before` and the
`a3ob_rig_cleanup_*` details. Details of the rules as built: a point never loses its heaviest weight to the threshold
(a point whose weights are all below it keeps the heaviest); equal weights keep the lower bone number; a negative weight
counts as no weight; byte 255 members (weight 0.0) are counted, reported and taken out of their selection; a point with
no weight stays empty and is a warning; when the skeleton has `a3ob_rig_discrete = 1` every point keeps its heaviest bone
with weight 1 and the message says so. Weights Convert has `mode` and `case` (and no `group`). It reads and writes the
point groups and `a3ob_w_*` attributes, renames an existing selection with a different spelling together with its prim
group, `a3ob_fw_*` and its entry in `a3ob_tagg_order`, and never touches a group that is not a bone. Capture to Selections
only rewrites the bones that have a region in the capture.

Export itself does not change weights (data model section 7). So the Validate node gets three **proposed** rows next to
the `T2-*` rules of `houdini-data-model.md` section 17.1: `T2-WGT4` (a point with more than 4 bone weights), `T2-WGTSUM`
(a bone-weighted point whose sum is not 1 on the grid), `T2-WGTNONE` (a point in a LOD that should be weighted, with no
bone weight). All three are warnings by default, switchable like the other tier-2 rules.

## 5. Visualiser design (criterion 5)

### 5.1 How it is shown

The Mass node's "Visualize" toggle is the model (`python/a3ob_houdini/nodes/mass.py:79-82`, `:256-279`;
`python/a3ob_houdini/node_visualizer.py`). It makes a **node visualizer**, not colour on the geometry:
`hou.viewportVisualizers.createVisualizer(type("vis_generic"), Node, node)`, then `setType(vis_color)` and
`setParm("colortype", "attribramped")`, `setParm("attrib", ...)`, `setParm("colorramp", hou.Ramp(...))`. The geometry
and any `Cd` it has are not changed, the export ignores it, and hython has no `hou.viewportVisualizers`, so every call
does nothing there. The Rig Check node uses the same module: `node_visualizer.ensure_color_ramp(node, name, label,
attrib, stops, low, high)`.

One small addition is needed (proposal): a `set_attrib(viz, attrib)` helper so the node can re-point the same visualizer
at a different attribute when the artist changes the **Show** menu. Whether a visualizer survives save and reload could
not be tried without the window (`node_visualizer.py` header); the Mass node re-makes it in `loaded()`. Rig Check does
the same.

Because this needs the GUI, it is a **check for the owner** (section 6.4, test 2.9): all visual properties below are
unverified until seen in a Houdini window.

### 5.2 The problems, and the attribute each one is shown from

Rig Check (section 6.2) computes these on a copy of the input (point attributes, proposals):

| # | Problem | Attribute | How it is computed | Colour |
|---|---|---|---|---|
| 1 | Filled-in versus copied points | `a3ob_rig_origin` int: 0 no weight, 1 copied, 2 filled in, 3 rigid piece, 4 non-rigid piece | written by Weight Transfer; missing means "unknown" | 0 black, 1 grey, 2 orange, 3 blue, 4 green (stepped ramp) |
| 2 | Weight jumps between neighbours | `a3ob_rig_jump` float | for each point, the largest L1 difference of its weights to those of any edge neighbour, divided by 2 (0 = same, 1 = completely different bones) | 0 white to 0.5 and above red |
| 3 | More than 4 influences before the clean-up | `a3ob_rig_infl` int | number of bones with weight above 1/254 | 1 to 4 grey to white; 5 magenta; 6 and more red |
| 4 | Weight sum not 1, or no weight | `a3ob_rig_sumerr` float | `abs(sum - 1)` over the skeleton-bone weights | 0 white, up to 0.1 yellow, above red; sum 0 black |
| 5 | Rigid piece straddling a joint | `a3ob_rig_straddle` float, per point of a rigid piece | share of the piece's points whose heaviest bone differs from the piece's dominant bone; the first-pass weights under the piece are used | 0 white, 0.5 and above red |
| 6 | Joints outside the mesh | `a3ob_rig_joint_depth` float on the **skeleton output** | for each joint: signed distance to the mesh surface, positive inside (winding number test); negative is outside | green inside by more than 1 cm, yellow within 1 cm, red outside |
| 7 | Pose stress: stretch and compression | `a3ob_rig_stress` float | see 5.3 | blue compressed, white fine, red stretched |
| 8 | Heaviest bone | `a3ob_rig_dominant` int | index of the heaviest bone | a repeating ramp of 12 hues, to read the regions at a glance |

Problem 6 is on the second output (the skeleton), so it cannot share a node visualizer with the mesh. The skeleton is
our own geometry, so Rig Check writes `Cd` there directly (the exporter never reads the skeleton).

The node also writes **messages** (info, warning), as the other nodes do (`docs/design/ux-tools.md` section 2): for
example "1125 points have more than 4 bones", "3 rigid pieces straddle a joint: piece 4 (bones LeftArmRoll 0.73,
LeftForeArm 0.24)", "left wrist joint is outside the mesh by 2.1 cm". Counts and the first five element numbers, then
"and N more". The worst problems are warnings; they do not block the cook (the data model's rule: a warning keeps
geometry flowing, an error stops everything below).

### 5.3 Pose stress test

A set of test poses (a parameter menu; the node ships a list). For each pose the node poses the mesh with linear blend
skinning in numpy (the spike code), then for every point averages over its edges `log(posed length / rest length)` and
stores the largest absolute value over the poses with its sign. Presets (proposal; all poses are bone rotations about the
pivots): arms raised 90 degrees; arms forward 90; elbows bent 90; hips flexed 90 (sitting); knees bent 90; spine bent 30
forward; neck turned 45; wrists bent 45. Shown colour range: -30 % to +30 % (parameter). The `Pose` menu also offers one
pose at a time and outputs that posed mesh on a third output, so the artist can look at the deformation.

This is the measure that the spike (section 2.6) lacked: it shows shear and stretch where the weight error L1 only
shows a weight difference.

## 6. Nodes, data flow, phases, tests and open questions (criterion 6)

### 6.1 Data flow

```
 model.cfg --+
 pivots.p3d -+--> [Rig Skeleton] ---------- Arma skeleton A (rest) ------------------------------------+
                                                                                                         |
 Samples A3_character_example.p3d --> P3D Import --> Extract LOD (Resolution 1)                          |
        (existing nodes)                    |                                                            |
                                            +--> [Weights Convert] (capture form, only if needed)        |
                                            sample body S with bone weights                              |
                                                                                                         |
 user mesh U (+ optional landmarks) ----------+---------------------------+                             |
                                              v                           v                             |
                                   [Rig Fit] (A, S, U) -> fitted skeleton A', fitted sample body S'    |
                                              |                                                         |
                       group fields: first pass / rigid / non-rigid                                     |
                                              v                                                         |
                                   [Weight Transfer] (S' -> U, three layers)                            |
                                              |                                                         |
                                   [Weight Cleanup] (4 bones, grid, report)                              |
                                              |                                                         |
                                   [Rig Check]  <-- visualiser, in the fitted pose                      |
                                              |                                                         |
                                   [Unpose] (A' -> A) <------------------------------------------------+
                                              |
                                   [Rig Check] (again, in the Arma rest pose, with the pose test)
                                              |
                                   existing Assemble LODs --> Validate --> P3D Export
```

Notes: the existing P3D nodes carry the sample into Houdini unchanged; there is no "sample loader" node. The Samples
path is the artist's own (section 0.1), stored once in the project settings (D37 Q1) and defaulted to the Steam folder
when it exists. A user mesh already in the Arma rest pose, made from the sample, skips Rig Fit
and Unpose.

### 6.2 Nodes (all proposals)

Naming follows the existing nodes (`docs/design/ux-tools.md` section 5): parameter names lower case and short; one
parameter table in `python/a3ob_houdini` builds the HDA (D13); each node shows a summary line; the logic is in
`python/a3ob_houdini/nodes/` and a hou-free `a3ob_core/rig/`. Sections of parameters below omit Houdini's own
"group" convention details, which follow the Mass node's group parameter.

**Rig Skeleton** (no input; output: skeleton)

| Parameter | Type, default | Meaning |
|---|---|---|
| `cfg` | file | The `model.cfg` to read. Parsed by an own small parser (A3OB shows a pure parser works; `cfgconvert` is only a fallback for binarised files). |
| `skeleton` | menu | The `CfgSkeletons` classes of the file (class name, bone count), or the class named by a `CfgModels` entry (`skeletonName`). `skeletonInherit` is merged, parent first, duplicates dropped by lower-case name (`io/import_mcfg.py:109-126`). |
| `pivots` | file, default Samples `TemplateRTM/ManSkeleton_Pivots.p3d` | An editable pivots P3D. The pivot is the first point of the bone's selection in the Memory LOD (A3OB convention). |
There are no parameters for the next three rules: the owner decided them (D36, D37 Q4), so the node does them always.

- `Face_*` and `Eye*` bones are left out (the face is out of scope).
- A bone with no pivot (`Camera`, `weapon`, `launcher` in the sample) is left out and reported as missing.
- A pivot selection that is not a bone (the sample's `Hips`) is ignored and reported.

Messages (as built in task #61): an info line with the counts ("64 joints from the 103 bones of OFP2_ManSkeleton. Left
out: 36 face and eye bones, 3 with no pivot. 1 pivot ignored."), a line with the names with no pivot, a line with the
ignored pivots; a warning for `#include` lines that were not followed and for a joint whose parent was left out; an error for
duplicate names, a parent after its child, a name over 31 characters or not ASCII (A3OB validator rules), an empty
or missing file (the text names the setting to change), and a skeleton or pivots file that cannot be read. The node
reads the one `model.cfg` the user picks (section 3.1, "Model Config"). A new node starts from the Samples files when the
Samples folder is set once in the project settings (D37 Q1). Output: section 4.1.

**Weights Convert** (input: geometry; with an optional second input, a skeleton)

| Parameter | Type, default | Meaning |
|---|---|---|
| `mode` | menu: Selections to Capture, Capture to Selections | Section 4.2. |
| `case` | menu: model.cfg spelling, keep existing | Group name spelling when writing selections. |
| `group` | point group | Points to convert (empty = all). |

**Weight Cleanup** (input: mesh with weights; second input: skeleton)

| Parameter | Type, default | Meaning |
|---|---|---|
| `max_bones` | int, 4 | The Arma limit. |
| `threshold` | float, 0.01 | Weights below this are dropped. |
| `grid` | toggle, on | Snap to k/254 with an exact sum. |
| `fill` | toggle, off | Give unweighted points the weights of the nearest weighted point. |
| `warn_change` | float, 0.3 | Warn for a point whose weights change by more than this. |
| `group` | point group | Points to clean. |

Reports: section 4.4. Output: the mesh with cleaned groups and `a3ob_w_*`, plus `a3ob_rig_changed`, `a3ob_rig_infl_before`.

**Rig Fit** (inputs: 1 user mesh, A-pose, with optional landmark point groups; 2 Arma skeleton; 3 sample body with weights)

| Parameter | Type, default | Meaning |
|---|---|---|
| `landmarks` | menu: Auto-guess, From groups, Both | Auto-guess places the standard 11 to 13 points (chin, groin, wrists, elbows, knees, ankles, shoulders, hips) from the mesh. The input mesh is in an A-pose (D37 Q5). From groups reads the landmarks as **point groups (selections) on the user mesh**, one group per landmark (D37 Q11); the average of a group's points is the landmark. They win over the guess. |
| `snap` | toggle, on | Snap landmarks to the mesh surface. |
| `symmetry` | toggle, on | Mirror left and right landmarks. |
| `refine` | toggle, off | Light non-rigid refinement of the sample body with Topo Transfer (one connected component needed). |
| `scale` | menu: Per bone length, Whole body | How bone lengths are fitted. Default Per bone length. |

Outputs: 1 fitted skeleton A' (joints in the user mesh, `transform` carrying the fitted rotation); 2 fitted sample body S'
(posed and scaled, weights kept, `a3ob_rig_origin` not set yet); 3 the landmarks as points. The artist corrects a
landmark by changing the points of its group on the user mesh (D37 Q11: landmarks are selections, like every other
group in this add-on). The fit is a rigid fit down the hierarchy: from
the pelvis, rotate each bone to the fitted child direction and set its length to the fitted distance. This is the step the
spike measured only indirectly; its accuracy is the main risk of the whole design (section 2.6, point 3).

**Weight Transfer** (inputs: 1 user mesh; 2 fitted sample body S')

| Parameter | Type, default | Meaning |
|---|---|---|
| `firstpass` | point group | Base clothing. Empty = every point not in the two groups below. |
| `rigid` | point group | Rigid accessories. |
| `nonrigid` | point group | Non-rigid accessories. |
| `distance` | float, 0.05 | `D` as a share of the bounding-box diagonal (paper default). |
| `angle` | float, 35 | `Theta` in degrees (paper default). |
| `rigid_mode` | menu: Dominant bone, Fixed blend | Per piece. |
| `merge_twist` | toggle, on | Decided (D37 Q12): for a rigid piece, a `...Roll` (twist) weight goes to the next non-twist bone down the chain when choosing the dominant bone (in the spike's cuff, `LeftArmRoll` counts for `LeftForeArm`). The per-piece override `a3ob_rig_bone` below still wins. |
| `smooth` | int, 8 | Smoothing passes along a non-rigid piece. |
| `minpiece` | int, 10 | Pieces under this many points are ignored (as the spike). |

An optional point string attribute `a3ob_rig_bone` (proposal) on a rigid piece names its bone and wins over the choice.
Output: weights as point groups and `a3ob_w_*`, plus `a3ob_rig_origin` and `a3ob_rig_piece`. The numpy transfer of section
2.3, with the native VEX closest-point step available as a later speed-up.

**Unpose** (inputs: 1 mesh with cleaned weights; 2 fitted skeleton A'; 3 Arma skeleton A)

| Parameter | Type, default | Meaning |
|---|---|---|
| `rigid_pieces` | toggle, on | Points of pieces in the rigid group are moved by the inverse of **one** matrix per piece (its bone, or the fixed blend), so they keep their shape (section 2.6). |
| `group` | point group | Points to unpose. |

Each point moves by the inverse of its blended bone matrix, from the fitted skeleton to the Arma rest skeleton. It
reports the mean and the largest displacement, and the worst bones. Output: the mesh in the Arma rest pose. The weights
do not change.

**Rig Check** (inputs: 1 mesh with weights; 2 skeleton). Outputs: 1 mesh with the problem attributes; 2 the skeleton with
`a3ob_rig_joint_depth` and `Cd`; 3 the posed mesh of the chosen test pose.

| Parameter | Type, default | Meaning |
|---|---|---|
| `show` | menu: Origin, Jump, Influences, Sum, Straddle, Stress, Heaviest bone | Which attribute the visualizer colours (section 5.2). |
| `pose` | menu: Worst of all, then each test pose | Section 5.3. |
| `stress_range` | float, 0.3 | The stretch at full red. |
| `jump_limit` | float, 0.5 | Jumps above this are warned about. |
| `visualize` | toggle, off | As on Mass. |

Later phases add **LOD Weights** (phase 3: propagate to the other LODs, section 6.3) and **Mirror Weights** (phase 3),
and **RTM Import** (phase 4).

### 6.3 Phases and acceptance tests

Every test that needs the Samples uses `tests/samples_support.py` and skips when the Samples are absent. Tests use the
same dual runner as the rest of the suite (system Python and hython). Plain Python tests have numpy 1.26; hython has numpy
2.3.2; the pure rig code must pass on both.

**Phase 1: skeleton, weight conversion, export clean-up.** No fit. Gives a complete path "weights in Houdini to a valid
Arma P3D", and the checks that every later phase uses.

| # | Acceptance test |
|---|---|
| 1.1 | Rig Skeleton on the Samples `Test_Character_01/model.cfg` (class `OFP2_ManSkeleton`) and `TemplateRTM/ManSkeleton_Pivots.p3d` gives 64 joints with a pivot, parents before children, every joint at its pivot (compared to a direct read of the P3D), no `Face_*` or `Eye*`. `Hips` is reported and ignored. `Camera`, `weapon`, `launcher` are reported as missing. |
| 1.2 | The skeleton picker lists the classes of both Samples `model.cfg` files; a hand-written test `model.cfg` with `skeletonInherit` gives the parent's bones first, without duplicates; `//` and `/* */` comments and `class A: B` parse. |
| 1.3 | Bad skeletons give the errors of section 6.2 (duplicate name differing only in case; child before parent; a name of 32 characters). |
| 1.4 | Round trip of section 4.3: sample Resolution 1 -> Selections to Capture -> Capture to Selections equals the import to within 1/254, non-bone selections untouched. |
| 1.5 | Joint Deform with our capture (Selections to Capture) at the rest pose returns every point to its position within 1e-5; with one joint rotated it equals a numpy linear blend skinning of the same weights within 1e-5. This settles the `boneCapture_pCaptData` layout (section 4.2, step 4). |
| 1.6 | Weight Cleanup on a weights set with 5 and 6 bones per point, a sum of 0.7, a sum of 0, and a weight of 0.004: the result has at most 4 bones, every sum is exactly 1 on the k/254 grid (or the point is reported), the message gives the counts, and `a3ob_rig_changed` equals the L1 change. On the sample Resolution 1 it changes nothing except the points whose sum is not 1 (the sample's own sums run 0.984 to 1.0). |
| 1.7 | Export: the sample imported, cleaned and exported again has the same set of selections, and a re-import reads the same weights; a file with a point over 4 bones triggers `T2-WGT4`, with `T2-WGTSUM` and `T2-WGTNONE` for the other two rows. |
| 1.8 | The Validate rows are warnings and switchable like the other tier-2 rules. |

**Phase 2: fit, transfer, layered groups, visualiser.**

| # | Acceptance test |
|---|---|
| 2.1 | Weight Transfer, same topology, no scale (the spike's target A, m = 0): L1 mean at most 0.07 and the heaviest bone changed on at most 6 % of the points (spike: 0.064 and 5.1 %); 12k points in under 10 s in hython. |
| 2.2 | Layered groups: with a rigid group containing a cuff, every point of a piece gets identical weights (dominant bone: one bone, weight 1.0; fixed blend: the same row). With a non-rigid group, the weights vary along the piece but the difference between neighbours is smaller than in the unsmoothed transfer. The first pass is unchanged by the other two groups. |
| 2.3 | Rig Fit on a scaled-and-posed copy of the sample (arms 20 degrees down, lengths +10 %, body +5 %, as spike scenario C) places the joints within 1.5 cm of the true joints, and the unpose result is within 1 cm of the true rest (max) with a mean under 0.3 cm. If the landmark auto-guess misses this, the test uses landmark point groups. |
| 2.4 | The fit-quality measure of section 2.6, point 3 is a message of Rig Fit (joint distance after the fit to the landmarks) and a warning above 1 cm. |
| 2.5 | Unpose reproduces the spike's table: with the true weights and a perfect skeleton, the error is below 0.01 cm; the rigid pieces keep their edge lengths to within 0.1 %. |
| 2.6 | Rig Check computes the attributes of section 5.2 on made-up cases with known answers: one point with 5 bones is `a3ob_rig_infl` 5; a joint placed outside a sphere has a negative `a3ob_rig_joint_depth`; a piece whose points favour two bones has a `a3ob_rig_straddle` of the right share. |
| 2.7 | The pose stress test: a cylinder skinned to two bones gives stretch on the outside and compression on the inside of a bent elbow with the sign expected. |
| 2.8 | The Export of the output of Rig Check is byte identical to the export of its input (the new attributes are ignored). |
| 2.9 | **Owner check in the Houdini window:** the visualizer colours appear for each Show menu entry, survive saving and loading the scene, and switch off cleanly. |
| 2.10 | On a real third-party character provided by the owner (question Q-6), the fit, transfer, clean-up and unpose run to the end, and the owner judges the visualiser's flags against what they see. |

**Phase 3: LOD propagation and mirroring.**

| # | Acceptance test |
|---|---|
| 3.1 | LOD Weights copies Resolution 1 weights to Resolution 2 to 4, View - Pilot and the two Shadow Volume LODs by the Weight Transfer method (no fit; the LODs are aligned). On the sample's own LODs the L1 against Bohemia's weights is below 0.1 mean. |
| 3.2 | Memory, Hit-points and Fire Geometry points get one rigid bone each (the bone of the nearest Resolution 1 point). The Geometry LOD gets no bone selections. |
| 3.3 | Mirror Weights swaps `Left*` and `Right*` bone groups across the X plane: mirroring a mirrored mesh returns the input; a symmetric body's weights are unchanged except for the swap. |

**Phase 4: RTM import for pose tests.**

| # | Acceptance test |
|---|---|
| 4.1 | A pure Python RTM reader (port of `io/data_rtm.py`, plain RTM only; BMTR optional) reads the Samples' template RTM, with the axis swizzle, and gives a pose per frame for the Rig Skeleton joints. Bone names over 31 characters or not in the skeleton are reported. |
| 4.2 | RTM Import poses the skeleton; the mesh posed by Joint Deform equals the numpy linear blend skinning to within 1e-4. |
| 4.3 | The pose stress test of Rig Check can use an RTM frame as its test pose. |
| 4.4 | An A3OB-exported RTM of a simple bent arm round-trips through the reader. |

### 6.4 What stays unverified until built or seen

- The in-game result (joints off the pivots, 5th bone, sums, which LODs): needs Arma 3. The owner will test these in
  game (D37 Q8, Q9).
- The visualiser in a Houdini window (test 2.9).
- (Settled in task #62, section 4.2:) the `boneCapture_pCaptData` layout for Joint Deform (test 1.5).
- Fit accuracy on real, third-party meshes (tests 2.3, 2.10). The spikes use the sample itself.
- That the engine uses linear blend skinning.

### 6.5 Questions for the owner, with the answers (D37, 2026-11-04)

1. **Samples path.** The Rig Skeleton and the sample body need the Samples folder. Should the add-on remember one path in
   the project settings (the settings dialog already stores a project root), or ask on each node?
   **Answer (D37 Q1):** one path, stored once in the project settings and the file `a3ob_settings.json` (field "Arma 3 Samples folder" in the project settings dialog). It defaults to the Steam folder when that exists. A new Rig Skeleton node starts from it.
2. **Weight grid.** The task text says 1/255. The real files and our M1 code say 1/254 (`(255 - b) / 254`). This design
   uses 1/254 with an exact sum of 1. OK?
   **Answer (D37 Q2):** yes. The grid is 1/254 and every sum is exactly 1.
3. **Selection name spelling.** The sample's selections are `Head`, `Neck`, `Weapon`; the bones are `head`, `neck`,
   `weapon`. The engine matches ignoring case; Object Builder's animation preview needs matching case (`io/data_rtm.py:155-159`).
   Default to the `model.cfg` spelling when writing selections?
   **Answer (D37 Q3):** write the bone selections with the `model.cfg` spelling.
4. **Bones with no pivot** (`Camera`, `weapon`, `launcher`) and the pivots file's `Hips`. Default to leaving them out of
   the skeleton and ignoring `Hips`. Is there any case where they must be rigged?
   **Answer (D37 Q4):** `Camera`, `weapon` and `launcher` are left out and `Hips` of the pivots file is ignored. The Rig Skeleton node reports them.
5. **Pose of the user meshes.** T-pose, A-pose, the Arma sample pose, or anything? The landmark auto-guess and the rigid
   fit assume a roughly standing, limbs-apart pose.
   **Answer (D37 Q5):** the input meshes are in an **A-pose**.
6. **Test characters.** One or two real third-party characters (uniform plus vest, helmet, pack; one with other
   proportions) for tests 2.3 and 2.10. Single watertight mesh, or several separate pieces?
   **Answer (D37 Q6):** the owner's `exportTests/testChar/testChar.fbx`: a single mesh with a helmet, pauldrons, knee pads, a bandolier, pouches and a holster (a CC-BY-4.0 Sketchfab asset, never committed). Its existing JOINTS/WEIGHTS and the glTF are ignored: incoming meshes are assumed to carry no joint data.
7. **How different can the proportions be** (child, very tall, stylised)? The spike covers bone lengths of +10 %, size +5 %.
   **Answer (D37 Q7):** adult humanoids only. The skeleton is never changed for body shape, female shapes included.
8. **Which LODs need weights.** We follow the sample (section 3.3). Is a missing weight on Shadow Volume or Memory a
   problem you have seen in game? Can you check one case: a character with only Resolution 1 weighted?
   **Answer (D37 Q8):** follow the sample's LOD set (section 3.3).
9. **In-game behaviour with joints off the pivots, with more than 4 bones, and with sums not 1.** These are not in any
   source I could read. Can you test one model in game for each?
   **Answer (D37 Q9):** the owner will test in game: joints off the pivots, a fifth bone, and sums not 1.
10. **Learned landmarks.** Is an optional external step (Make-It-Animatable, PyTorch and a CUDA GPU) acceptable later? Do
    you have a CUDA GPU?
    **Answer (D37 Q10):** the owner has an NVIDIA RTX 50-series GPU (about 20 GB VRAM, 64 GB RAM). An optional external learned-landmark step is acceptable later.
11. **Landmark clicks.** Is it acceptable to confirm or correct about 11 to 13 landmarks in phase 2, or must the fit be
    fully automatic?
    **Answer (D37 Q11):** landmarks are fine, given as point groups (selections).
12. **Twist (`...Roll`) bones.** In the Arma hierarchy `LeftArmRoll` sits between `LeftArm` and `LeftForeArm`. A rigid piece
    at the elbow can have `LeftArmRoll` as its heaviest bone yet needs to follow `LeftForeArm` (section 2.6). Keep the
    default (heaviest wins, with an override) or always merge twist weights into the forearm?
    **Answer (D37 Q12):** for rigid pieces the twist (Roll) weight goes to the next non-twist bone, with an override.
13. **Weights in the fitted pose or the rest pose?** Weight Transfer and Cleanup run in the fitted pose. The Unpose then
    applies those weights. After Unpose, the weights are exported unchanged. OK, or should they be recomputed after the
    unpose by a second transfer from the sample body at the Arma rest pose (a re-transfer would remove the shear of
    section 2.6, point 4, but needs the sample to match the unposed mesh)?
    **Answer (D37 Q13):** weights from the fitted pose, exported unchanged after the unpose.
14. **Package of the pure code.** A new `a3ob_core/rig/` that needs numpy, where the rest of `a3ob_core` does not (only
    `paa.py` uses it, as an optional import). Acceptable, since the rig nodes only run inside Houdini?
    **Answer (D37 Q14):** `a3ob_core/rig` may need numpy.

## 7. Interpretations and gaps in this document

- The task gave six numbered criteria; sections 1 to 6 match them. The unpose question of D36 is section 2.6.
- The 1/255 in the task is answered by the file format: 1/254 (section 3.1).
- "Recommend one approach for phase 1 and one for later" is answered in section 1.5 as "first fit build" and "later fit
  build", because the task's phase 1 (skeleton, weights, clean-up) needs no fit.
- Houdini 22 features were checked by name and cook in hython; behaviours that need the viewer (landmark placement, the
  Autorig Builder, Topo Landmark) were read from the help only.
- The Bohemia wiki pages returned HTTP 403 to the fetch tool; the advisor read the three pages of section 3.1 in a browser
  (2026-11-04) and their facts are recorded there.
- Not read: the sample's engine behaviour; HumanRig's repo; Pinocchio's LGPL version;
  AccuRIG's landmark count.

## 8. Reproduce

All from the repository root. The Samples path comes from `A3OB_SAMPLES`
(default `E:/SteamLibrary/steamapps/common/Arma 3 Samples`). Do not set `HOUDINI_PACKAGE_SKIP=1` for the Houdini spikes:
181 KineFX and APEX types go missing without the default packages.

| Spike | Command |
|---|---|
| Sample facts | `python scripts/spikes/rigging/sample_facts.py` |
| Node survey | `"<Houdini>/bin/hython.exe" scripts/spikes/rigging/houdini_node_survey.py` |
| Node cooks | `"<Houdini>/bin/hython.exe" scripts/spikes/rigging/houdini_node_cooks.py` |
| Weight transfer, numpy | `"<Houdini>/bin/hython.exe" scripts/spikes/rigging/weight_transfer_numpy.py` |
| Weight transfer, Houdini routes | `"<Houdini>/bin/hython.exe" scripts/spikes/rigging/weight_transfer_houdini.py` |
| Unpose | `"<Houdini>/bin/hython.exe" scripts/spikes/rigging/unpose_distortion.py` |

Printed output of each: `docs/design/evidence/rigging/<script>.out`.

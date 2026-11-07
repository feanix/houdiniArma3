# Digests

Status: v1 built (task #81): `skeleton`, `skeleton-compare`, `deform-compare`, `weights`, `landmarks`; `feet` and `wrap-distance` added in task #82, `fingers` and `head-turn` in task #83. The model is the digest
tools of the VoxelTerrarium project (its `docs/design/digest-tools.md`, sections 1 to 3, and its decision D232).

## 1. Why

A scene holds data: points, weights, joint positions. A digest turns that data into information: figures, counts and
percentiles, each beside the value it should be read against. An agent (or the owner) turns the information into knowledge:
"this skeleton is too short at the hips". The digests do the first step so that nobody writes a one-off script over raw
geometry to answer a question that has been asked before. A question no digest answers is a missing digest: add one, not a
measuring test.

## 2. Rules

- **A digest never judges.** It prints a figure and the reference value beside it: the Arma rest value, the other input's
  value, a limit (4 bones per point, a weight sum of 1). It does not say "good", "bad" or "too long".
- **One question per digest.** The page answers it and ends with `next:`, the digests that go deeper.
- **Real units.** Centimetres (the scene is in metres), millimetres where it helps, degrees. Ratios are plain numbers (1 is
  unchanged).
- **Not recorded, never zero.** A figure the data does not hold prints as `n/r` in a table, and the `not recorded:` line says
  what is missing. A missing joint is not a length of 0.
- **The page names what it read:** the scene file, the node path and the part (`fitted_skeleton`, `landmarks`, or `whole
  output`).
- **Size.** The summary line is first, the page is at most 25 lines of 80 columns. A longer page (`--all`) is printed whole and
  the command line says so on stderr.
- **No second copy of a formula.** The pages call the project's own readers and maths: `rig_weights.read_part`,
  `read_skeleton`, `read_selections`, `read_capture`, `read_mesh`, `group_mask`, `landmark_io.sample_side`, `fit.MIN_BONE` (the
  shortest bone that has a direction), `check.weight_problems`, `check.edges_of`, `landmarks.measure`, `BONE_OF`.
- **Exit codes.** 0 answered, 1 could not answer (the node does not cook, has no skeleton, the meshes differ in topology), 2 the
  question was written wrongly (unknown node, part, group or digest, wrong number of nodes).

## 3. Form

- `python/a3ob_core/digest/`: pure code (numpy, no Houdini). `page.py` (the page, units, `n/r`, exit codes), `skeleton.py`,
  `deform.py`, `weights.py`, `landmarks.py`, `feet.py` (sole pitch, wrap distance), `pose.py` (finger bend, the turn of a bone), `regions.py` (the body region of a bone).
- `python/a3ob_houdini/digest.py`: reads a node and calls the pure code. In a live session:
  `from a3ob_houdini import digest; print(digest.skeleton("/obj/rig_fit_66/rig_fit"))`.
- `scripts/a3ob_digest.py`: `hython scripts/a3ob_digest.py <digest> <scene.hip*> <node path> [options]`. It loads the scene and
  never saves it. Run it with hython and without `HOUDINI_PACKAGE_SKIP` (a rig scene needs KineFX).

## 4. Which command answers which question

| Question | Command |
|---|---|
| What are the joints, parents and bone lengths, and how do they compare with the Arma rest skeleton? | `skeleton <scene> <node>` (`--rest <node>`, `--all`) |
| How does skeleton B differ from skeleton A (joint offsets, bone length, direction, pelvis-to-hips, elbow and knee angles, widths)? | `skeleton-compare <scene> <A> <B>` |
| Did mesh B stretch or squash against mesh A (same topology)? Where, by region, and how did a group (helmet) change size? | `deform-compare <scene> <A> <B>` (`--by region\|bone`, `--group G`, `--body G`) |
| How many points does each bone hold, which points have no weight, which have more than 4, how far is the sum from 1? | `weights <scene> <node>` (`--all`) |
| Where are the landmarks, and what do the mesh and the Arma sample say at the same places? | `landmarks <scene> <node>` (`--mesh <node>`, `--group G`) |
| What is the sole pitch of each foot, and where do the heel and the toe end, for your body, the wrapped body, the Arma sample and an unposed mesh? | `feet <scene> <node> [<node> ...]` (`--group G` for your body, default `body`; a Rig Fit node gives your body, the wrapped body and the sample, any other node its own points) |
| How far is the wrapped sample body from your body, per region, and how far are its heel and toe from yours? | `wrap-distance <scene> <rig fit node>` (`--group G`) |
| How much is each finger bent at its base and middle knuckle (0 = straight), for the wrapped body, the Arma sample and your posed or unposed mesh? | `fingers <scene> <node> [<node> ...]` (a Rig Fit node gives the wrapped body and the sample; any other node its own mesh with its bone weights; the bend is the angle between the centroids of the three segments of a finger by dominant bone; the Arma sample's rows are the reference) |
| How much did the points of a bone (the head) turn between two meshes (posed and unposed)? How far do neck and head lean from vertical in the fitted skeleton and in the Arma? | `head-turn <scene> <A> <B>` (`--bone head`, repeatable; `--fit <rig fit node>` for the lean lines; pitch nose down +, yaw to the left +, roll left ear up +, from the rigid (Kabsch) fit of the bone's points; the reference is 0) |

`--part` and `--part-b` name the part of a Rig Fit output to read (`fitted_skeleton`, `fitted_body`, `landmarks`); the default
is the part the digest needs.

## 5. Definitions

- **Bone length:** the distance from a joint to its parent. A root has none.
- **Position offset (skeleton-compare):** the distance between a joint in A and in B, matched by name ignoring case. A second
  row gives the offset left after the mean move of all joints is taken off, so a shift of the whole skeleton can be told from a
  change of shape. **Bone direction:** the angle between the bone of a joint in A and in B (not recorded for a root or a bone
  shorter than 1 mm, such as Pelvis to Spine).
- **Body measures** (`measure_values`): pelvis-to-hips is the distance from the Pelvis joint to the middle of the two hip
  joints (`LeftUpLeg`, `RightUpLeg`); pelvis-to-head the distance from the Pelvis to `head`; an elbow (knee) angle is the angle
  at the `ForeArm` (`Leg`) joint between the shoulder (hip) and the wrist (ankle), 180 straight; hip width and shoulder width
  are the distances between the two hip joints and between `LeftArm` and `RightArm`.
- **Edge ratio (deform-compare):** the length of an edge in B over its length in A. An edge belongs to a region when the dominant
  bone of both its points is in that region (`regions.region_of`); an edge between two regions is `between regions`; an edge with
  an unweighted point is `no weight`. The group rows count edges with both points in the group.
- **Weights as the export reads them:** a bone selection is a point group named like a bone of the man skeleton (ignoring case,
  `rig_bones`). A point has a weight for a bone only where it is a member of the group; the value is `a3ob_w_<group>` (1 without
  the attribute). A value on a point that is not a member is not a weight (it is only the attribute's storage; reading it as a
  weight was a mistake once). A member with weight 0 or less is no weight. The digest reads through `rig_weights.read_selections`,
  and a test checks the influences and sums against `type_rules` `bone_weights` of P3D Export. A mesh with no bone selections and
  a `boneCapture` is read through `rig_weights.read_capture`. The points only a packed primitive uses (proxies) are not counted.
- **Landmarks:** heights are above the lowest point of the body concerned. The crotch on the mesh and the shoulder top come
  from `landmarks.measure`, the function the guess uses, run on the points of the chosen group; the Arma sample column is the
  template that `make_template` made from the sample body and skeleton.

- **Feet (task #82):** the rule of `a3ob_core/rig/foot.py`, the one Rig Fit's foot landmarks use (docs/design/character-rigging.md,
  Feet). Sole pitch: the angle of the edge, at the middle of the foot, of the lower convex hull of the foot seen from the side,
  degrees, toes up positive; the reference is 0 and the Arma sample's own rows. Heel z and toe z: the back and the front end of
  the foot (its farthest points along the forward axis); length: between them, seen from above. Wrap distance: from each wrapped
  point to the closest point of the surface of your body group, per region of the wrapped point's dominant bone; foot ends: the
  wrapped end minus yours along your foot's axis (plus = farther forward); the reference is 0.

## 6. Adding a digest

Add the pure page function to `a3ob_core/digest/` (inputs are numpy arrays and small records, so a test can use a three-joint
skeleton), the reader to `a3ob_houdini/digest.py`, the name to `DIGESTS` and the table in section 4, and a known-answer test.
Pin the page layout with the owner-scene-sized case in `tests/test_digest_core.py`.

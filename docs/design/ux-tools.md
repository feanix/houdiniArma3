# A3OB tools in Houdini: UX design (nodes, shelf tools, walkthroughs)

Status: revision 2 (2026-11-03), task #6. Updated for D13 (shipping) and D14 (the owner's answers to Q-1 to Q-13).
Nothing here is built yet.

This document says what an artist sees and does: which nodes exist, what is on each node, what the shelf buttons do,
and how errors show up. It follows the owner decision D10 (node and shelf split, one SOP network per model) and the
data-model spec `docs/design/houdini-data-model.md` revision 3 (called "the spec" below; `§n` points into it).

How nodes ship is settled by owner decision D13 (which supersedes D11): text-authored Python-type HDAs, stored in
folder form under `houdini/otls/<name>.hda/` and never saved from the GUI. This document describes every node by its
parameters and its behaviour. [Shipping: what the artist sees](#6-shipping-what-the-artist-sees) lists what D13 means
for the artist. The packaging spike (task #5, `docs/design/packaging-spike.md`) has the evidence.

## 0. How to read this

| Word | Meaning |
|---|---|
| **P3D geometry** | The spec's container (§3.1): one packed primitive per LOD, in file order. What P3D Import outputs. |
| **LOD geometry** | One LOD as ordinary unpacked geometry, with its LOD data in detail attributes (§3.1). What you edit. |
| **Spec attribute** | An attribute, group or detail name defined in the spec. This document never invents one. |
| **Parm name** | The short internal name of a parameter, given so that both shipping options use the same names. |
| **A3OB** | The Blender add-on. Paths like `ui/tool_mass.py` are relative to `.reference/Arma3ObjectBuilder/Arma3ObjectBuilder/` (pinned at 9f2b8bc). |
| **Q-n** | A question that the owner has answered (D14). The answers are in [section 10](#10-resolved-d14). |
| **M1, M3, M4** | M1 is the finished pure-Python layer `python/a3ob_core`. M3 is import, M4 is export. |

Rules for this document: where the UX needs something the spec does not define, it is a spec addition (task #7),
not a new attribute invented here.
Every Houdini claim marked "checked" was run in hython 22.0.466 on this machine.

## 1. The design in one page

**One network per model.** Each model is one SOP network (D10). The shape is the same every time:

```
                                   +-> Extract LOD (Geometry) -> ...edit... --+
 P3D Import ---+-------------------+-> Extract LOD (Res 1)    -> ...edit... --+--> Assemble LODs -> P3D Export
               |                                                                    ^
               +--------------------------- base input: untouched LODs pass through -+
```

- **P3D Import** reads the file and outputs the P3D geometry.
- **Extract LOD** takes one LOD out of the P3D geometry as a LOD geometry. One branch per LOD you want to edit.
- You edit the branch with ordinary Houdini nodes and the A3OB nodes (LOD Properties, Proxy, Material Assign, Mass).
- **Assemble LODs** puts edited LODs back. The P3D geometry from Import goes into its first input. Every LOD you did
  not touch passes through unchanged (no unpack, no repack).
- **Validate** (optional) and **P3D Export** finish the chain. Export has the same checks built in.

**Nodes versus shelf** (D10):

| Where | What | Why |
|---|---|---|
| Nodes | Import, Extract LOD, Assemble LODs, LOD Properties, Proxy, Material Assign, Mass, Validate, P3D Export | They change or check the geometry, so they must re-cook when anything upstream changes. |
| Shelf | Quick Export, Selection browser, Project root and presets, Batch folder convert, New P3D | They act once, on the file system, on settings, or they build a network. They are not part of the cook. |

**Where the code lives.** Every node's logic is a Python function in `python/a3ob_houdini`. The node itself is a thin
wrapper that calls that function and reports what it returns. A single parameter table per node in the same package
describes the parameters (name, label, default, menu), so one table builds the HDA's parameters (D13). The same table
could build the spare parameters of a Python SOP, which keeps the fallback open. Pure data work (reading, writing, signatures, strings) stays
in `a3ob_core`; the node code only translates between Houdini geometry and `a3ob_core` objects (spec §16).

**Defaults are lossless.** An unedited file imported and exported again must give the same bytes (the spec's L0 goal).
So every export option that changes data is off by default (owner decision, spec §16). A3OB's own defaults are the
opposite (see the table in [section 5.9](#59-p3d-export)); [Presets](#72-project-root-and-presets) bring them back
with one click.

## 2. How problems show up

Houdini nodes have three kinds of message. **Checked** in hython 22.0.466 with a Python SOP wrapped by a Null:

| Kind | Call on the node | What the artist sees | Does geometry keep flowing? |
|---|---|---|---|
| Message | `addMessage` | Blue info, in the node's message list | Yes |
| Warning | `addWarning` | Yellow node, text on the node | **Yes**: the downstream Null still had all 8 points |
| Error | `addError` or a raised `hou.NodeError` | Red node | **No**: the node has no geometry, and the node below it turns red too ("Invalid source ... (Error: ...)") |

This fact drives the rules below. A red node does not just look bad: everything below it stops. That is the right
behaviour for "the file cannot be written", and wrong for "you might want to look at this".

**Rules (used by every node):**

1. **Spec severity decides.** A spec §17.1 "error" is a node error. A spec "warning" is a node warning. A bad
   parameter or a bad input is an error.
2. **One text shape.** `<LOD name>: <what is wrong>. <how to fix it>.` Example below. The LOD name is the A3OB display
   name from `a3ob_core` (`format_lod_name`), for example `Resolution 1` or `Geometry`. Element numbers are listed
   (first 5, then "and 12 more").
3. **No wall of text.** A node shows at most 25 messages (parm `maxmessages` on Validate and Export). The summary line
   says how many were left out. The full list also goes to the Houdini console, like A3OB's console log
   (`utilities/logger.py`).
4. **A summary message** (info) on every A3OB node says what it did: "9 LODs, 11 packed proxies" or
   "2 replaced, 7 passed through".
5. **The viewport is never blanked by a warning.** To see geometry while an error is active, put the display flag on
   a node above the red one (walkthrough 3).

Example error as the artist reads it:

```
Geometry: 3 polygons have more than 4 points (prims 12, 15, 40). P3D faces have 3 or 4 points.
Fix: add a Divide node on the Geometry branch (Convex Polygons on, Maximum Edges 3).
```

## 3. Shared pieces

### 3.1 The two shapes of input

Most nodes work on one shape and check it first.

| Node | Accepts | Error text if wrong |
|---|---|---|
| Extract LOD | P3D geometry | "Input is not a P3D geometry (packed LODs). Connect P3D Import or Assemble LODs." |
| LOD Properties, Proxy, Material Assign, Mass | any geometry (normally a LOD geometry) | none; they never fail on shape |
| Assemble LODs | input 1: P3D geometry (optional). Other inputs: LOD geometry | "Input 3 has no LOD type. Add a LOD Properties node." |
| Validate, P3D Export | P3D geometry **or** one LOD geometry | the collection errors of spec §3.2 (mixed packed and loose, packed prim that is not a LOD or a proxy) |

Validate and P3D Export use the **same collection code** (spec §3.2), so a model that passes one passes the other.

### 3.2 LOD name

The name shown in menus and messages is `format_lod_name(type, resolution)` from `a3ob_core/p3d/lod_types.py`: for
example `Resolution 1`, `View - Pilot`, `Shadow Volume 0`, `Geometry`, `Memory`. It is also written to the outer prim
attribute `name` of each LOD in the P3D geometry (spec §3.1), so the Geometry Spreadsheet is readable.

### 3.3 Group fields

Group fields on the A3OB nodes use normal Houdini group syntax. A3OB selections are point groups with encoded names
(spec §7.3), for example `-proxies` is `xn__proxies_cka`. The [Selection browser](#73-selection-browser) shows the
decoded name next to the encoded one and copies the encoded one for you, so nobody has to type `xn__...`.

## 4. Where the A3OB Blender UI tells us what to keep

| A3OB UI | File | Carried over as |
|---|---|---|
| Import dialog: "Absolute Paths", "Enclose In Collection", "Group By", "Allow Additional Data" and its list, "Validate Meshes", "Proxy Action", "First LOD Only", "Translate Selections", "Cleanup Selections", "Sections" | `ui/import_export_p3d.py` lines 17-94 | Mostly not. See [5.1](#51-p3d-import). One is kept ("LODs" filter), one is spec-driven (explicit face groups). |
| Export dialog: relative paths, custom normals, validate meshes, selected only, apply transforms, apply modifiers, sort sections, LOD collisions (Ignore / Skip / Fail), validate LODs, warnings are errors, renumber components, force lowercase, translate selections, generate components | `ui/import_export_p3d.py` lines 241-315 | The export options of [5.9](#59-p3d-export). Blender-only ones (selected only, modifiers, apply transforms, validate meshes) are not needed: the network is already procedural. |
| LOD Properties panel: "Is P3D LOD", type, resolution, Named Properties list with add / remove / "paste common", Copies list | `ui/props_object_mesh.py` lines 569-704 | [LOD Properties](#54-lod-properties) |
| Proxy panel: path, index, align, realign, extract, copy, transfer | `ui/props_object_mesh.py` lines 819-858, `ui/tool_proxies.py` | [Proxy](#55-proxy) (path, index, placement). Copy and transfer become ordinary Houdini work (see 5.5). |
| Material panel: "File / Color / Custom" source, texture, procedural colour with type, rvmat | `ui/props_material.py`, `props/material.py` lines 11-45 | [Material Assign](#56-material-assign) |
| Mass tool: value type (Mass or Density), value, Set On Each, Distribute (Uniform or Weighted), From Density, Clear, Visualize, Center of Mass, stats | `ui/tool_mass.py`, `props/scene.py` lines 121-161 | [Mass](#57-mass) |
| Validation tool: "Detect" LOD type, warnings are errors, relative paths, three tiers (error / warning / info) | `ui/tool_validation.py`, `utilities/validator.py` lines 26-91 | [Validate](#58-validate) |
| Preferences: Project Root (default `P:\`), custom data JSON, default flags, create backups | `__init__.py` lines 232-295 | [Project root and presets](#72-project-root-and-presets) |
| "Paste common" lists for proxies, properties, procedural textures, materials; `common_data` | `ui/props_object_mesh.py` lines 78-115, 170-203; `utilities/data.py` line 782 onward | Preset menus on Proxy, LOD Properties, Material Assign, fed by the same JSON idea |
| Batch scripts | `scripts/import_p3d_batch.py` | [Batch folder convert](#74-batch-folder-convert) |

A3OB features that are **not** carried as nodes, and why:

| A3OB feature | Why not |
|---|---|
| Outliner and Proxy Access lists (`ui/tool_outliner.py`) | The brief marks them Blender-only. The Geometry Spreadsheet and the network editor do this job. |
| Vertex and face flag groups (`ui/props_object_mesh.py` lines 705-816) | Lossy in A3OB; the spec keeps raw `a3ob_vertex_flags` and `a3ob_face_flags`. No node in D10. See Q-10. |
| Proxy copy, copy-all, transfer between LODs (`ui/tool_proxies.py`) | In Houdini the proxies are packed prims inside the LOD geometry. Copy them with Blast (group by `a3ob_proxy_path`) and Merge (LOD geometry first, so its detail wins). The Proxy node can also place new ones. |
| Edit-mode assign / select operators | Native Houdini group editing. |
| Convex hull, find components, check convexity, cleanup vertex groups (`ui/tool_utilities.py`) | Native Houdini nodes. Convexity and components are checked by Validate as switchable warnings (Q-3, M4). |
| Import "Additional data" switches | Dropping data on import makes the export lossy (spec §15). Use Attribute Delete if you want less data. |

## 5. The nodes

All nodes: one output. Parm names are lowercase and short. "Default" is what a new node shows.

### 5.1 P3D Import

| | |
|---|---|
| Purpose | Read a `.p3d` file and output the P3D geometry. |
| Inputs | none |
| Output | P3D geometry: one packed prim per LOD, in file order, each with the outer attribute `name`. |
| Reads | the file, through `a3ob_core` (M1). |
| Writes | everything in spec §16 "Import, per LOD": points, polygons, `N`, `uv*`, `a3ob_vertex_flags`, `a3ob_face_flags`, `a3ob_texture`, `a3ob_material`, point groups with `a3ob_w_<enc>`, prim groups with `a3ob_fw_<enc>`, edge group `a3ob_sharp` (+ `a3ob_sharp_extra`), `a3ob_mass`, and the detail attributes of §3.3 (`a3ob_spec_version`, `a3ob_lod_type`, `a3ob_lod_resolution`, `a3ob_lod_signature`, `a3ob_prop_names` / `a3ob_prop_values` / `a3ob_prop_raw`, `a3ob_tagg_order`, `a3ob_source_counts`, the blob arrays, and others only when needed). Prim int `a3ob_lod_uid` on every polygon (§3.5), from the node's path. Packed proxies with `a3ob_proxy_path`, `a3ob_proxy_index` and the snap guard (§13.3), or inline triangles (§13.6). Detail `a3ob_file_trailing` on the P3D geometry when the file has trailing bytes. |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| File | `file` | empty | File chooser, `*.p3d`. Empty file is a node error with a hint, not a crash. |
| Reload | `reload` (button) | | Forces a re-read. Otherwise the node re-reads when the path or the file's modification time changes. |
| LODs | `lods` | `*` | Which LODs to import, by file position: `*`, or `0 2-4`. Carries A3OB's "First LOD Only" (`ui/import_export_p3d.py` line 75). A partial import gives a **warning**: "LODs not imported are missing from the P3D geometry; exporting it will not write them." |
| Explicit face groups | `facegroups` | off | Spec §7.2 (OQ-6): always make prim groups for face selections, not only where the face bytes differ. |

Not offered, on purpose (see section 4): absolute paths, translate selections, cleanup empty selections, proxy action,
data switches. Q-8 (proxy action: no forced-inline switch) and Q-11 (no extra import options) settle two of them.

**Messages.**

| Case | Surface |
|---|---|
| Info | "9 LODs, 11 packed proxies in Resolution 1, 0 inline proxies." |
| File missing or unreadable | Error. |
| File is not a valid MLOD (`a3ob_core` `P3DError`) | Error, with the reader's text (it names the LOD and the position). |
| Reader warnings from `a3ob_core` | Warning each. |
| Empty or duplicate selection name stored as a blob (§7.3), repeated `#UVSet#` id (§8), faces with 0 to 2 sides (§5), proxies interleaved so the round trip is L1 (§13.6) | Warning each, naming the LOD. |
| Characters that the cp1252 codec cannot show | none: M1 strings are always valid text (§5, D-STR). |

**Notes.**

- The LOD uid comes from the node's path (§3.5). Renaming or copy-pasting the node gives new uids. That is harmless: the
  uid is never written to the file.
- The node is the only importer. There is no File SOP converter (D10).

### 5.2 Extract LOD

| | |
|---|---|
| Purpose | Take one LOD out of the P3D geometry as a LOD geometry you can edit. This is the "unpack LOD k" helper of spec §17.2. |
| Input | P3D geometry. |
| Output | One LOD geometry: normal polygons and points, detail attributes of §3.3 intact, proxies still packed inside it. |
| Reads | the outer `name`, `a3ob_lod_type`, `a3ob_lod_resolution` (to build the menu), the packed transform. |
| Writes | nothing new. It unpacks one LOD with the Unpack verb (spec §3.1, §3.2), so the detail attributes stay detail. |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| LOD | `lod` | first LOD | Menu built from the input: `0: Resolution 1`, `1: Resolution 2`, ... The value is the position in the P3D geometry. Menu labels use the LOD name (3.2). |

**Messages.**

| Case | Surface |
|---|---|
| Info | "Extracted LOD 4: Geometry, 34 points, 64 polygons." |
| Input is not a P3D geometry | Error (3.1). |
| `lod` is past the end (the input changed) | Error: "The input has 6 LODs, LOD 8 does not exist." |
| The LOD prim has a transform that is not the identity | Warning: "Transform baked in." Unpack's "Apply Transform" does it (§3.2). |

**Notes.** Never Extract several LODs in one node and Merge them: a Merge of unpacked LODs keeps only the first
input's detail attributes (spec §3.1, T7c). That is why Assemble packs each LOD itself.

### 5.3 Assemble LODs

| | |
|---|---|
| Purpose | Pack LOD geometries into a P3D geometry. Put edited LODs back into the original and let the others pass through. This is the "Pack LODs" helper of §17.2. |
| Inputs | 1: base P3D geometry (**optional**). 2 and up: LOD geometries. |
| Output | P3D geometry. |
| Reads | `a3ob_lod_type`, `a3ob_lod_resolution`, `a3ob_lod_signature` (to match and to name). |
| Writes | the outer prim attribute `name` on each LOD it packs. Keeps the base's detail `a3ob_file_trailing` (the base is the first input, so Merge keeps it, T7c). |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Replace matching LODs | `replace` | on | A LOD input replaces the base LOD that has the **same stored signature** (`a3ob_lod_signature`, raw bits, §3.4). It keeps the base LOD's position. A LOD input with no match (a new LOD, or one without `a3ob_lod_signature`) is appended at the end. Off: always append. The matching rule is fixed by Q-4 (D14); the spec text for it is in spec section 17.1 / 3.2 (task #7). |

How it works: each LOD input is packed with the Pack verb (Pack by Name off, spec §3.1) and merged. The base LODs it
does not replace are copied as they are, packed, untouched. That is how "untouched LODs pass through" and why a
file with no edits is byte-identical after export.

**Messages.**

| Case | Surface |
|---|---|
| Info | "2 replaced, 7 passed through, 1 added." |
| An input has no `a3ob_lod_type` | Error: "Input 3 has no LOD type. Add a LOD Properties node." |
| An input is a P3D geometry (packed LODs) in the LOD slots | Error: "Input 2 is already a P3D geometry. Use it as the base input." |
| Two LOD inputs have the same signature | Warning, as spec 3.2 says: the first one replaces the first base LOD with that signature, a further one replaces the next base duplicate, if one is left, and is appended otherwise. "Resolution 1: 2 LOD inputs have the same signature (inputs 2, 3)." |
| Several base LODs have the same stored signature | Warning, the first one is replaced and the others are kept. |
| A base LOD to replace is not a PackedGeometry prim (for example PackedDisk) | Error naming the base LOD and the prim type. Replace Matching LODs off passes such a prim through. |

**Notes.**

- With no base input it builds a new P3D geometry from scratch. The New P3D template does this.
- It does not sort. Sorting is an export option (D7, OQ-3).
- Duplicate signatures after replacement are reported by Validate and Export, not here.
- There is no 4-input limit. The HDA sets its input count in its `INDEX__SECTION` ("Inputs: 1 to N"), see
  [section 6](#6-shipping-what-the-artist-sees). Input 1 is the base; the others are LODs. The first build uses one
  number N for the maximum, chosen when the HDA is written. Chaining two Assemble nodes (the second takes the first as
  its base) also works but is not needed.

### 5.4 LOD Properties

| | |
|---|---|
| Purpose | Give a geometry its LOD identity: type, resolution, named properties, LOD copies. Replaces A3OB's "LOD Properties" panel (`ui/props_object_mesh.py` lines 569-704). |
| Input | Geometry (normally a LOD geometry). |
| Output | The same geometry with the LOD detail attributes set. |
| Reads | `a3ob_lod_type`, `a3ob_lod_resolution`, `a3ob_prop_names`, `a3ob_prop_values`, `a3ob_prop_raw`, `a3ob_lod_copy_types`, `a3ob_lod_copy_resolutions` (only for the Read from Input button and the Add mode). |
| Writes | `a3ob_spec_version` (1), `a3ob_lod_type`, `a3ob_lod_resolution`, `a3ob_lod_copy_types`, `a3ob_lod_copy_resolutions`, `a3ob_prop_names`, `a3ob_prop_values`, and `a3ob_prop_raw` (kept parallel; dropped when no entry has a tail). It keeps a **stored** `a3ob_lod_signature` in step with the type and resolution (§3.4): bits that still decode to the new type and resolution stay as they are; otherwise they are re-encoded with `a3ob_core`. A LOD with no stored signature stays without one. It does not touch `a3ob_lod_flags`. |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Read from Input | `fromin` (button) | | Copies type, resolution, properties and copies from the input's detail attributes into the parameters below. Use it right after Extract LOD. |
| Set type and resolution | `settype` | on | Off: leave the input's type and resolution alone and only edit properties and copies. |
| LOD type | `lodtype` | `Resolution` | Menu of all types from `a3ob_core` `LOD_TYPES`, by display name (`Resolution`, `View - Gunner`, `Shadow Volume`, `Geometry`, `Memory`, ..., `Unknown`). Tooltip shows the description. Nobody types the enum number (§17.2). |
| Resolution | `resolution` | `1` | Float, 0 or more. Shown for types that carry a resolution (`has_resolution`). For `Unknown` the label is "Signature value". |
| Property mode | `propmode` | `Add to input` | `Add to input`: entries below are added; an entry with the same name (any case) replaces the input's. `Replace all`: only the entries below remain. |
| Properties | `props` (multiparm) | 0 | Rows of `prop_name#` and `prop_value#`. Menu "Add common" fills a row from the presets. |
| LOD copies | `copies` (multiparm) | 0 | Rows of `copy_type#` (the same menu) and `copy_res#`. A copy is the same mesh written under another signature (A3OB's "Copies"). |

There is no signature parameter. The signature, when the LOD has a stored one, is shown in the info message.

**Messages.**

| Case | Surface |
|---|---|
| Info | "Resolution 1, signature 1, 2 properties, 0 copies." |
| The stored signature did not fit the new type and resolution | Info: "The stored signature no longer fitted the new type and resolution and was updated." |
| `encode_signature` raises (resolution out of range for the type, or an Unknown value that collides with a known type) | Error naming the type and the allowed range. |
| Property name or value longer than 63 characters (`props/object.py` limit, M1 `PropertyTagg`) | Error naming the row. |
| A property or type text with a character that cp1252 cannot write | Error (D-STR, §5). |
| Same property name twice | Warning. |
| A copy has the same signature as the LOD itself | Warning (Validate will also fail it as a duplicate). |

**Notes.** When the artist changes the type of an imported LOD, the old `a3ob_lod_signature` is stale. The spec already
handles this: the export compares the decoded stored bits to the current type and resolution and re-encodes if they
differ (§3.4). The node does that step itself for a stored signature and says so in the info message above.

### 5.5 Proxy

| | |
|---|---|
| Purpose | Add proxies (a reference to another `.p3d`, such as a weapon slot or a seat) to a LOD geometry. Edit existing ones with normal Houdini nodes (Transform on the packed prims, group by `a3ob_proxy_path`). |
| Inputs | 1: geometry. 2 (optional): points, one proxy per point. |
| Output | The geometry plus new packed proxy prims. |
| Reads | the polygons and loose points (to place the new prims after them, as §13.3 requires). |
| Writes | for each new proxy a PackedGeometry prim (§13.3) with outer `a3ob_proxy_path` and `a3ob_proxy_index`; the embedded canonical triangle (§13.5 step 2) carries the same two attributes on its polygon, and flags 0 (`a3ob_vertex_flags`, `a3ob_face_flags`). No snap-guard arrays (`a3ob_proxy_src_P`, `a3ob_proxy_src_N`): a new proxy has no source values. |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Path | `path` | empty | The proxy model path, for example `\a3\data_f\proxies\weapon_slots\top`. Slashes are written as backslashes and the `.p3d` ending is removed, as A3OB does (`props/object.py` line 258, `utilities/generic.py` `format_path`). Material Assign does the same for its paths (Q-6, D14); see spec section 3.2 (task #7). |
| Common proxies | `common` (menu) | none | Fills Path from the presets (A3OB "Paste Common Proxy", `utilities/data.py` line 782 onward). |
| Index | `index` | `1` | 0 to 999. |
| Number from points | `autoindex` | on | With input 2: the first proxy gets `index`, the next `index + 1`, and so on. Off: all get `index`. |
| Translate | `t` | `0 0 0` | Placement when there is no input 2. |
| Rotate | `r` | `0 0 0` | Degrees. A proxy has no scale: A3OB also resets scale to 1 when aligning (`ui/tool_proxies.py` lines 58-59). |
| Orient from points | `useorient` | on | With input 2: rotate each proxy by the point's `orient` if the point has one; no other attribute is read (Q-8, D14). There is no switch to force inline triangles. |

**Messages.**

| Case | Surface |
|---|---|
| Info | "1 proxy added: \a3\data_f\proxies\weapon_slots\top 1." |
| Empty path | Error. |
| Path or index already used by another proxy in the same LOD | Warning. The game needs a unique path and index pair. |
| Path has characters cp1252 cannot write | Error (D-STR). |

**Notes.**

- The frame follows the spec: the placement matrix is a proper rotation (§13.4). The node never builds it from M1's
  `matrix_rows()`.
- Moving an imported proxy: the snap guard (§13.5) writes the stored source values back while the proxy has moved
  less than 1e-5; beyond that it takes the new transform. The artist does not have to think about it.

### 5.6 Material Assign

| | |
|---|---|
| Purpose | Set the texture and the material (rvmat) on a set of faces. |
| Input | Geometry. |
| Output | The same geometry with `a3ob_texture` and `a3ob_material` set. |
| Reads | `a3ob_texture`, `a3ob_material` (to keep the other one when only one is set). |
| Writes | prim strings `a3ob_texture` and `a3ob_material` (default `""` when created). No other attribute. Houdini's `shop_materialpath` and `material` are **not** touched (spec §12). |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Group | `group` | empty (all) | Primitives, or with `grouptype` points: the faces whose points are all in the group. |
| Group type | `grouptype` | `Primitives` | `Primitives` or `Points`. |
| Set texture | `settex` | on | Off: leave `a3ob_texture` as it is. |
| Texture source | `texsource` | `File` | `File`, `Color`, `Custom`. As A3OB (`props/material.py` line 11). |
| Texture file | `texture` | empty | For `File`. |
| Color | `color` | `1 1 1 1` | For `Color`: builds `#(argb,8,8,3)color(r,g,b,a,TYPE)` (A3OB `to_p3d`, `props/material.py` line 82). |
| Color type | `colortype` | `CO` | A3OB's list (`utilities/data.py` line 323 onward: CO, CA, LCO, ...). |
| Raw text | `raw` | empty | For `Custom`: copied as typed. Menu "Common procedural" fills it. |
| Set material | `setmat` | on | Off: leave `a3ob_material` as it is. |
| Material file | `material` | empty | The rvmat path. Empty with `setmat` on **clears** it. |

**Messages.**

| Case | Surface |
|---|---|
| Info | "Set on 1243 polygons." |
| Group matches nothing | Warning. |
| Characters cp1252 cannot write | Error naming the field (D-STR; Validate and Export would stop on it later). |
| Path ends in something other than `.paa`, `.tga`, `.png` or `.rvmat` | Warning only. Procedural text is never judged. |

**Notes.** Sections are the runs of equal texture and material in prim order (§12). Assigning a material to scattered
faces fragments them. Turn on **Sort sections** on the export (5.9) to group them. A3OB turns that on by default
because alpha faces need it (`ui/import_export_p3d.py` line 274).

### 5.7 Mass

| | |
|---|---|
| Purpose | Set the per-point mass of a Geometry LOD (`#Mass#`). Replaces A3OB's Vertex Mass Editing panel (`ui/tool_mass.py`). |
| Input | Geometry. |
| Output | The same geometry with the point attribute `a3ob_mass`. |
| Reads | `P` and the polygons (for density and centre of mass), `a3ob_mass`, Houdini `mass` (copy modes), `a3ob_lod_type` (for the warning). |
| Writes | point float `a3ob_mass`. With **Visualize** on, also `Cd` (spec §14 keeps `Cd` free for preview; the exporter ignores it). Never writes Houdini's `mass`, except in the "Copy to mass" mode that the user asks for (spec §10). |

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Mode | `mode` | `Uniform total` | `Set on each` (every point gets Value), `Uniform total` (Value in kg is shared equally among the points), `From density` (Value in kg/m3 times the absolute volume of each closed component, so a component with flipped faces still gets a positive mass, shared among its points), `Clear` (removes `a3ob_mass`), `Copy from mass`, `Copy to mass` (spec §10, D7 OQ-4 helper). The first four follow A3OB's buttons "Set Mass On Each", "Distribute Mass", "Mass From Density", "Clear All Masses". |
| Value | `value` | `1` | 0 to 1,000,000. Kilograms, or kg/m3 for density. |
| Group | `group` | empty (all points) | Points to set: a point group name or point numbers. A3OB works on the selected vertices. In density mode a closed component is used only when **all** its points are in the group; the others are ignored with the warning below. |
| Distribution | `distribution` | `Uniform` | `Uniform`. `Weighted` (by cell volume around each point) is A3OB's slow mode; the brief marks it secondary. Not in the first version. |
| Visualize | `visualize` | off | Writes `Cd`: blue (least) to red (most), A3OB's ramp (`props/scene.py` lines 162-215). |

Read-only results, shown as a node message (not as attributes): total mass, minimum, average, maximum, number of points
with mass, and the **centre of mass** in Houdini coordinates. A3OB moves the 3D cursor there
(`ui/tool_mass.py` line 171); in Houdini the artist reads it from the message and the Null / Add node is theirs to make.

**Messages.**

| Case | Surface |
|---|---|
| Info | "Total 90.0 kg on 34 points. Centre of mass (0.00, 0.95, 0.01)." |
| Density mode meets open or flat components | Warning: "Non-closed or flat components were ignored" (A3OB's text, `ui/tool_mass.py` lines 63-67). |
| `a3ob_lod_type` is not Geometry | Warning: the spec says `a3ob_mass` on another LOD type is a validator warning (§10). Geometry, Geometry Buoyancy and Geometry PhysX count as geometry types (Q-3, D14). |

### 5.8 Validate

| | |
|---|---|
| Purpose | Run the spec's checks and report them as node messages. It changes nothing. |
| Input | P3D geometry or one LOD geometry. |
| Output | The input, unchanged. The one exception: with `errorlevel` on `Error` and a spec error found, the node goes red and has no output (section 2). |
| Reads | everything the exporter reads. It writes no attribute and no file. |
| Writes | nothing. |

What it checks: **all of spec §17.1**, row for row, plus A3OB's per-LOD-type rules as a second tier of switchable warnings (Q-3, D14; spec section 17.1, task #7; built in M4). Some rows need only Houdini
data (polygon sides, merged LODs, packed prims); others use `a3ob_core` (`encode_signature`, `encode_string`,
`structure_errors()`, `check()`).
The node reuses the exporter's code to build the `a3ob_core` objects, so it tells the artist exactly what Export would.

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Errors show as | `errorlevel` | `Error (stop here)` | `Error (stop here)`: red node, nodes below it stop. `Warning (keep geometry)`: yellow, geometry keeps flowing, text still starts with "Error:". Default is Error (Q-1, D14). |
| Warnings count as errors | `strict` | off | A3OB has this on by default for its validator (`props/scene.py` line 262). Ours is off (Q-2, D14). |
| Duplicate LODs | `dupes` | `Fail` | `Fail`, `Skip`, `Ignore`. A3OB's "Collisions" (`ui/import_export_p3d.py` line 279). Spec §3.2. |
| Max messages | `maxmessages` | `25` | Section 2, rule 3. |

**Messages.** Spec severity maps to node severity (section 2). The wording follows the spec's table: for example the
"merged LOD" warning reads:

```
Resolution 1: looks like two LODs were merged (uids 482911 and 1730224, 5379 and 1371 polygons).
A Merge of unpacked LODs keeps only the first input's LOD data. Fix: pack the LODs with Assemble LODs instead.
```

A summary info line always appears: "9 LODs checked: 0 errors, 2 warnings."

### 5.9 P3D Export

| | |
|---|---|
| Purpose | The end of the chain. Check the model and, on **Save to Disk**, write the `.p3d`. |
| Input | One input: P3D geometry or one LOD geometry (spec §3.2). To export several SOPs together, make a P3D geometry with Assemble LODs. |
| Output | The input, passed through, so the display flag can stay on this node. |
| Reads | everything in the spec's attribute tables. |
| Writes | the file, only when Save to Disk is pressed. The geometry is never changed. Cooking never writes a file. |

The cook runs the same checks as Validate and reports them. **Save to Disk** runs them again on fresh data, builds the
`a3ob_core` objects in the spec's order (§16: `set_mesh` first, then TAGGs), runs `structure_errors()` (hard stop) and
`check()` (advisory), and writes the file through a temporary file that replaces the target when it is complete. A
failed save leaves the old file alone.

| Parameter | Parm name | Default | Notes |
|---|---|---|---|
| Output file | `file` | `$HIP/$HIPNAME.p3d` | Chooser, `*.p3d`. |
| **Save to Disk** | `save` (button) | | See 5.9.1. |
| Check | `check` | `Every cook` | `Every cook` or `When saving`. Large models can switch to `When saving`. |
| Errors show as | `errorlevel` | `Error (stop here)` | As in Validate. |
| Warnings count as errors | `strict` | off | As in Validate. |
| Duplicate LODs | `dupes` | `Fail` | As in Validate. |
| Max messages | `maxmessages` | `25` | |
| **Selection mask** | `selmask` | `* ^__*` | Which point groups become selections (spec §7.2, D7 OQ-9). A group whose decoded name is in `a3ob_tagg_order` is always a selection. |
| **Lowercase** | `lowercase` | **off** | Lowercases the same things A3OB does: texture and material paths, selection names (so proxy paths too) and property names and values (`io/data_p3d.py` line 752). The spec only names paths (§5); all four are lowercased (Q-12, D14), the tooltip says so, and a warning appears when lowering the case makes two selection names equal (spec section 17.1, task #7). |
| **Relative paths** | `relpaths` | **off** | Makes paths relative to `projectroot`; at least the drive letter is stripped (`utilities/generic.py` lines 174-216). |
| Project root | `projectroot` | empty = the shelf setting | Falls back to the root from [7.2](#72-project-root-and-presets) (A3OB default `P:\`). Used only when `relpaths` is on. |
| **Sort sections** | `sortsections` | **off** | Orders faces by texture and material pair (`io/export_p3d.py` line 255). Changes the face order, so a round trip becomes L1. |
| **Sort LODs** | `sortlods` | **off** | Sorts by signature, as A3OB always does (`io/export_p3d.py` line 716). |
| **Auto-components** | `autocomponents` | **off** | For Geometry-type LODs with no `component##` selection, make them (A3OB "Generate Components", `io/export_p3d.py` lines 294-303). Only if none exist. The check counts them as present while this is on (A3OB does the same with a temporary one, line 310). |
| Renumber components | `renumber` | off | Spec §16. |
| Translate names to Czech | `czech` | off | Spec §16 (`io/export_p3d.py` line 591). |
| Drop UVs on LODs that do not use them | `dropuvs` | off | Spec §8 option (`io/export_p3d.py` line 275). |
| Keep a .bak | `backup` | off | When overwriting, keep the old file as `.bak` (A3OB preference "Create Backups", `__init__.py` line 232). |
| Preset | `preset` (menu) + Apply | `Lossless` | Fills the option parameters from a saved set (7.2). |

A3OB's own defaults for the same options, for comparison. The owner chose off for all of them in D10.

| Option | A3OB default | Source line | Here |
|---|---|---|---|
| Relative paths | on | `ui/import_export_p3d.py` 241 | off |
| Sort sections | on | 274 | off |
| Force lowercase | on | 302 | off |
| Generate components | on | 311 | off |
| LOD collisions | Fail | 279 | Fail |
| Warnings are errors | on | 293 | off (Q-2, D14) |

#### 5.9.1 Save to Disk

- **In the GUI:** runs the save, then shows a short status line ("Saved 9 LODs, 412 KB to ...") and the node message.
  On a validation error it shows a dialog with the error list and writes nothing.
- **In hython or a script:** the same function without a dialog; errors raise a Python exception. This is what Batch
  folder convert and the tests call.
- **The button** is a button parameter of the HDA. Its callback calls one function in `python/a3ob_houdini`. Houdini
  swallows exceptions raised in a button callback (`pressButton` does not raise, checked in the spike), so that
  function catches every error itself, reports it (dialog in the GUI, node message everywhere), and never lets it
  escape. It writes the file to a temporary name next to the target and renames it when complete, so a failed save
  leaves the old file untouched (D13).
- **Later:** a `/out` ROP wrapper (D10) for rendering-style export, calling the same function.

**Messages (cook and save).**

| Case | Surface |
|---|---|
| Info | "9 LODs, 412 KB if saved now." |
| Any §17.1 row | As Validate. |
| `structure_errors()` or a non-empty `dropped_taggs` | Error: "Internal error: ... please report" (a coding or stale-blob bug, §16, not user data). |
| Folder of the output file does not exist | Error on save. |
| File cannot be written (locked, read-only) | Error on save; the old file is untouched. |
| Trailing bytes dropped because of several inputs or a Merge | Warning (§3.3). |

## 6. Shipping: what the artist sees

Owner decision D13 (which supersedes D11) fixes how nodes ship. The evidence is in `docs/design/packaging-spike.md`.

- Each node is a **text-authored, Python-type HDA**. It lives in folder form at `houdini/otls/<name>.hda/` and is
  never saved from the Houdini GUI (a GUI save would rewrite the files and hide the diffs).
- Each HDA has a **frozen entry function** and **one parameter table** in `python/a3ob_houdini`. The wrapper is thin:
  it calls the entry function and shows what it returns (section 1).
- Logic stays in `python/a3ob_houdini` and `a3ob_core`. A package update changes code; it does not need new assets
  unless the parameters change.

What this means for the artist:

| Topic | What the artist sees |
|---|---|
| Creating a node | Tab menu: a submenu "A3OB" with the nine names. The shelf tools (section 7) are separate. |
| Node type in the network | An A3OB type with the label from this document. |
| Parameter layout | Fixed by the asset, generated from the parameter table. |
| After a package update that changes parameters | The HDA update **adds the new parameters to old nodes and keeps the values** already set. Nothing is rebuilt by hand and there is no drift. |
| Inputs on Assemble LODs | Set in the HDA's `INDEX__SECTION` ("Inputs: 1 to N"). **There is no 4-input limit.** The spike changed the count from 1 to 4 by editing that text and connected three inputs (`docs/design/evidence/packaging/q4b_multi_input.out.txt` on main). N is one number chosen when the HDA is written. |
| Save to Disk button | A button parameter of the asset. Houdini swallows exceptions in button callbacks (`pressButton` does not raise), so the callback **catches and reports its own errors**, and writes to a temporary file and renames it (5.9.1). |
| Menus built from the input (Extract LOD) | Menu script on the parameter. |
| Licence tag on a saved asset | Indie-saved HDAs can carry the limited-commercial tag (D3). Not relevant while the HDAs are text-authored. |

**Fallback.** A Python SOP made by a shelf tool stays possible. The parameter table could build its spare parameters.
It would cost two things: a Python SOP has at most 4 inputs (checked: min 0, max 4), so Assemble would need chaining,
and old nodes would keep the old parameter layout (drift). D13 does not use it. The parameters, behaviour and messages
in this document would be the same.

## 7. Shelf tools

All shelf tools are one Python call into `python/a3ob_houdini`. None of them needs a node to be an HDA. Dialogs use
Houdini's built-in `hou.ui` calls first. `hou.ui` does not exist in hython (checked), so each tool has a function that
works without a dialog; the dialog is a thin layer on top.

### 7.1 Quick Export

| | |
|---|---|
| Does | Exports the selected node (or the node with the display flag in the current network) to a `.p3d` without adding a P3D Export node. |
| Settings used | If the selected node is a P3D Export node: its parameters. Otherwise the active preset (7.2). |
| Asks | The output path the first time; later it reuses the last one, stored as user data on the exported node (not a geometry attribute). |
| Creates | The `.p3d` file. No nodes. |
| Reports | The same error list as Export, in a dialog. Success: status line. |
| If the node is not a P3D geometry or a LOD geometry | Dialog with the Export collection error. |

### 7.2 Project root and presets

One dialog with:

| Setting | Default | Notes |
|---|---|---|
| Project root | `P:\` | A3OB default (`__init__.py` line 261). Also read from an environment variable if set (name chosen at build time). Used by relative paths. |
| Presets | `Lossless`, `Arma 3 release` | Named sets of the P3D Export option parameters (5.9). `Lossless` is the default (all data-changing options off). `Arma 3 release` is A3OB's defaults: relative paths, sort sections, lowercase and auto-components on. Both ship (Q-5, D14). |
| Common data file | none | A JSON file with extra common proxies, named properties, procedural textures and materials. Same idea as A3OB's "custom data" (`__init__.py` line 267, `utilities/data.py` `common_data`). Feeds the "Common" menus. |

Stored in a single JSON file in the user's Houdini preferences folder. The dialog can save the current Export node's
options as a new preset. The file is in the user folder, and an environment variable overrides its location (Q-7,
D14). The tool changes nothing in a scene.

### 7.3 Selection browser

| | |
|---|---|
| Does | Lists the point groups of the selected node's geometry by **decoded** name (`-proxies`, not `xn__proxies_cka`) with: member count, "weighted" if `a3ob_w_<enc>` exists, "faces" if a prim group of the same name exists, "proxy" if the name is a proxy name. A filter box narrows the list. |
| Actions | **Copy name**: puts the encoded group name on the clipboard, ready to paste into a group field. **Create selection**: asks for a P3D name and the point numbers (for example `0-9`; empty: all points), and adds a Group node that makes the encoded point group. **Delete selection**: adds a Group Delete node. **Rename**: adds a Group Rename node. |
| Creates | Nothing unless an action is chosen. Create, delete, and rename add one SOP node after the selected node (Group, Group Delete, Group Rename; all three exist, checked) and set its parameters. |
| Edits geometry? | Never directly: cooked geometry is read-only. |
| Version 1 form | A list dialog (`hou.ui.selectFromList`) and an input dialog. A real Python Panel comes after M4 (Q-9, D14). |

### 7.4 Batch folder convert

"Convert" means re-export with a report (Q-5, D14):

| | |
|---|---|
| Does | For every `.p3d` in a folder (option: subfolders): import it, export it again with the chosen preset to an output folder, and write a report. |
| Why | Fix up many old files at once: lowercase paths, change the project root, sort LODs. Also a quick check of the whole import and export pair on real files: the report says, for each file, **identical bytes** (L0), **different** (and the first difference), or **failed** (the error). |
| Dialog | Input folder, output folder (default: the input folder plus `_converted`), include subfolders (off), preset (default `Lossless`). |
| Creates | The converted files and `a3ob_batch_report.txt` in the output folder. No nodes, no scene. |
| Runs in | The GUI (progress bar, cancel) or hython (same function, no dialog). Models are processed one at a time and their geometry freed. |
| Reference | `scripts/import_p3d_batch.py` imports a folder into Blender with the import dialog's settings. |

### 7.5 New P3D

| | |
|---|---|
| Does | Builds the starter network of D10 under a new Geometry object, named by the artist. |
| Creates | One Geo object and these nodes (all inside it): |

```
 res1_mesh (Box) ---------> res1_props (LOD Properties: Resolution, 1) -----------+
 geometry_mesh (Box) -> geometry_triangles (Divide: 3 sides) -> geometry_components (Group: "component01") |
        -> geometry_mass (Mass: Uniform total, 100 kg) -> geometry_props (Geometry) --+--> assemble --> export
 memory_points (Add: 1 point, group "new_point") -> memory_props (Memory) --------+
```

| Node | Notes |
|---|---|
| `res1_mesh` | A Box, size 1, sitting on the ground. This is where the artist puts their model. |
| `geometry_mesh`, `geometry_triangles`, `geometry_components`, `geometry_mass` | A simple box, cut into triangles by a Divide node (Convex Polygons, 3 sides) so rule T2-TRI has nothing to warn about, with the point group `component01` that Geometry LODs need (A3OB `has_components`, `utilities/validator.py` line 252), and 100 kg on it. |
| `memory_points` | One point at the origin in a point group `new_point`, to be renamed and moved. |
| `*_props` | LOD Properties: `Resolution` 1, `Geometry`, `Memory`. |
| `assemble` | Assemble LODs with no base input. |
| `export` | P3D Export, file `$HIP/<object name>.p3d`, all data-changing options off. |

A sticky note in the network says which three nodes to replace. The network is built to pass every check in spec §17.1
as it stands (no errors and no warnings: the Geometry box is triangulated, a component group exists, mass is set), so Save to Disk works on the first
try. A test in M4 builds the template and checks that.

## 8. Walkthroughs

### 8.1 Edit an existing P3D and re-export

File: `tests/fixtures/p3d/sample_1_character.p3d` (read with `a3ob_core`):

| # | LOD | Points | Faces | Selections | Notes |
|---|---|---|---|---|---|
| 0 | Resolution 1 | 5379 | 10699 | 35 | 11 proxy selections, 1 UV set, `lodnoshadow=1` |
| 1 | Resolution 2 | 1371 | 2683 | 35 | |
| 2 | View - Pilot | 5376 | 10698 | 34 | |
| 3 | Shadow Volume 0 | 691 | 1343 | 29 | |
| 4 | Geometry | 34 | 64 | 1 | `component01`, mass 87.5 kg total, `class=man` |
| 5 | Memory | 36 | 4 | 42 | 4 proxy selections |
| 6 | Land Contact | 0 | 0 | 0 | |
| 7 | Hit-points | 31 | 0 | 22 | |
| 8 | Fire Geometry | 226 | 380 | 29 | |

Goal: set the Geometry LOD to 90 kg, give the character's head a new texture in Resolution 1, leave the seven other
LODs alone, and export to a new file. The fixture file itself is never written.

1. **Set the project root once** (shelf: Project root and presets). Leave `P:\` if that is your drive. This only
   matters for relative paths.
2. **New object** and a P3D Import node. Set `file` to the fixture. Cook it.
   The node message reads: "9 LODs, 11 packed proxies in Resolution 1, ...". In the Geometry Spreadsheet, Primitives
   tab, there are 9 packed prims named `Resolution 1`, `Resolution 2`, `View - Pilot`, `Shadow Volume 0`,
   `Geometry`, `Memory`, `Land Contact`, `Hit-points`, `Fire Geometry`.
3. **Check the round trip first (optional, one minute).** Wire Import straight into a P3D Export node, set `file` to
   a scratch path, press Save to Disk. No options are on, so the file should be **byte-identical** to the fixture (the
   spec's goal L0 on this fixture, §15). Compare the two files. Then continue.
4. **Geometry LOD, mass.** Add Extract LOD, `lod` = `4: Geometry`. Add a Mass node: `mode` Uniform total, `value`
   90. Its message says "Total 90.0 kg on 34 points."
5. **Resolution 1, texture.** Add a second Extract LOD, `lod` = `0: Resolution 1`. Add Material Assign: `grouptype`
   Points, `group` `head` (the selection browser shows `head` among the 35), `texsource` File, `texture`
   `\myaddon\data\head_co.paa`, leave Set material off. The message says how many polygons got the texture.
6. **Assemble.** Add Assemble LODs. Input 1 = the Import node (the base). Input 2 = the Mass branch. Input 3 = the
   Material Assign branch. Message: "2 replaced, 7 passed through, 0 added." (Check the spreadsheet: still 9 prims,
   same order.)
7. **Export.** Wire into the P3D Export node from step 3 (or a new one). Set `file` to a new name. Faces with the
   new texture now sit between faces with the old one in prim order, which breaks the sections apart (spec §12). If you
   want them grouped, turn on `sortsections` (this reorders faces: L1, expected).
8. **Save to Disk.** Status: "Saved 9 LODs, ... KB to ...". The seven untouched LODs come out as they went in; only
   LOD 0 and LOD 4 changed.
9. **Check by re-import.** A second P3D Import on the new file shows the same 9 LODs; the Geometry LOD mass sums to 90.

What this shows: the base-plus-branches shape, pass-through of untouched LODs, and that a no-edit export is lossless.

### 8.2 Build a simple model from New P3D

Goal: a crate with a visible mesh, a collision box and one memory point.

1. Shelf: **New P3D**. Name it `crate`. A Geo object appears with the starter network (7.5). The export node already
   points at `$HIP/crate.p3d`.
2. **Resolution 1.** Replace `res1_mesh` (the Box) with your crate model, or build on it. Insert a Material Assign
   after it: `texture` `\myaddon\crate\crate_co.paa`, `material` `\myaddon\crate\crate.rvmat`.
3. **Geometry.** Replace `geometry_mesh` with a convex, closed shape around the crate. Keep `component01`: if you add
   more parts, add more groups `component02`, ... (or turn on `autocomponents` on the export for a quick start).
   Change the mass on `geometry_mass`.
4. **Memory.** In `memory_points`, move the point and rename its group from `new_point` to a name the game uses, for
   example `pos_driver`. Add points and groups as needed. Selection browser: confirm the names.
5. **Properties.** On `res1_props` open Properties and add `lodnoshadow = 1` (use "Add common" for common ones).
6. **Look at the result.** Display flag on `assemble`: three packed prims named `Resolution 1`, `Geometry`, `Memory`.
7. **Export.** Open the P3D Export node. Messages: "3 LODs, ... if saved now" and any warnings. Press **Save to Disk**.
8. **Check.** Quick: add a P3D Import with the new file; you get the same three LODs back.
9. **For the game:** apply the preset `Arma 3 release` on the export node (relative paths, sort sections, lowercase,
   auto-components), then Save to Disk again.

### 8.3 Fix a validator error

Scenario: a Boolean on the Geometry LOD left some polygons with more than 4 points. The export will not write them.

1. **See it.** The P3D Export node is red. The Validate node (if there is one) is red too, and the nodes below it say
   "Invalid source". Click the first red node. The message reads (section 2's example):
   ```
   Geometry: 3 polygons have more than 4 points (prims 12, 15, 40). P3D faces have 3 or 4 points.
   Fix: add a Divide node on the Geometry branch (Convex Polygons on, Maximum Edges 3).
   ```
   Prim numbers are the polygon numbers in the Geometry LOD geometry, not in the P3D geometry.
2. **Find the branch.** Move the display flag to the last node of the Geometry branch (above Assemble). The viewport
   is no longer blank, because that node is above the red one.
3. **Look at the faces.** In that branch, add a Group node with `12 15 40` and look at them (the spreadsheet or the
   viewport). They are the Boolean's n-gons. (Highlighting them automatically comes after M4, Q-9.)
4. **Fix.** Insert a Divide node on the branch after the Boolean: `convex` on, `usemaxsides` on, `numsides` 3
   (triangles are best for a Geometry LOD, A3OB's `is_triangulated` check).
5. **Recook.** The red nodes clear. Export says: "9 LODs checked: 0 errors, 0 warnings." Save to Disk now works.
6. **Second case, a warning.** If the artist had used a Merge of two unpacked LODs, Validate would be yellow, with
   the "looks merged" text from 5.8 and a fix hint: use Assemble LODs. The geometry still flows and the model can still
   be written, because the spec calls this a warning (§17.1, D8). With `strict` on, it would be an error.

## 9. What belongs to M3 and what to M4

The rule: a piece belongs to the milestone whose code it needs. Import and everything that only edits spec attributes
needs the M1 reader and Houdini. Anything that writes a file, or checks what the file would contain, needs the
exporter's collection and `a3ob_core` build step (spec §16).

| Piece | Milestone | Why |
|---|---|---|
| Shared plumbing: parameter table, wrapper helper, message helper (section 2), string and name helpers (`encodeAttrib`, D-STR) | M3, step 1 | Every node uses it. |
| P3D Import | M3 | Reader plus the spec's import outline. |
| Extract LOD | M3 | Needs only the P3D geometry. |
| Assemble LODs | M3 | Needs only attributes. Testable without an exporter. |
| LOD Properties | M3 | Reads and writes detail attributes. |
| Material Assign | M3 | Prim strings. |
| Proxy | M3 | Packed prims, §13. |
| Mass (all modes except Weighted) | M3 | Point attribute. |
| Selection browser | M3 | Reads groups, creates Group nodes. |
| Collection (§3.2), the §16 build step, `Validate` checks | M4 | They are the exporter's front half. |
| Validate | M4 | Shares the collection and build code with Export. |
| P3D Export (+ Save to Disk) | M4 | Writer. |
| Quick Export | M4 | Calls the exporter. |
| Project root and presets | M4 | Only Export uses them (and Batch). |
| New P3D | M4 | Needs a working Export to be worth building. |
| Batch folder convert | M4, last | Needs both ends. |
| `/out` ROP wrapper, Weighted mass, Python Panel for selections, problem highlighting (Q-9) | after M4 | |

**Order of work.**

| Step | Content | Done when |
|---|---|---|
| 1 | M3 plumbing, then P3D Import | The fixtures import; every attribute in the spec tables checks out in hython. |
| 2 | Extract LOD, Assemble LODs | Import > Assemble with no edits gives the same packed geometry (pass-through test). |
| 3 | LOD Properties, Material Assign, Mass, Proxy (four small, independent nodes) | Each has a hython test for its attribute writes. |
| 4 | Selection browser | Names decode and Group nodes appear. |
| 5 | M4: collection and the build step, with Validate | Validate's rows of §17.1 each have a failing example. |
| 6 | P3D Export, Save to Disk | Both fixtures round-trip byte-identically (L0). |
| 7 | Project root and presets, Quick Export | |
| 8 | New P3D, Batch folder convert | The batch report on both fixtures says "identical". |

## 10. Resolved (D14)

The owner answered Q-1 to Q-13 as this document recommended (D14). The answers below are now rules. Where a rule
needs spec text, it is written in spec section 17.1 / 3.2 (task #7); this table does not restate it.

| # | Question | Resolved |
|---|---|---|
| **Q-1** | Validate error level | Default for Validate and Export is `Error (stop here)`, with the menu to switch to `Warning (keep geometry)`. Put the display flag above the red node while fixing (walkthrough 3). |
| **Q-2** | Warnings count as errors | Off. Warnings in the spec are things that may be intended (merges, case-equal names). |
| **Q-3** | A3OB per-LOD-type rules | Added to Validate as a second tier of switchable warnings (`Check LOD-type rules`, on), built in M4. Geometry, Geometry Buoyancy and Geometry PhysX are the geometry types, for these rules and for the Mass warning. Spec section 17.1 (task #7). |
| **Q-4** | How Assemble matches an edited LOD to its base LOD | By the stored `a3ob_lod_signature`, with the `replace` switch and the info message. It also works for LODs without polygons (Memory). Spec section 3.2 / 17.1 (task #7). |
| **Q-5** | Presets and Batch convert | Two built-in presets, `Lossless` and `Arma 3 release`. Batch convert is a re-export with a report. `.bgeo.sc` output may come later as an extra option. |
| **Q-6** | Slash normalisation | The Proxy and Material Assign nodes both turn `/` into `\` on entry, so the spreadsheet shows what is written. Export applies only lowercase and relative paths. Spec section 3.2 (task #7). |
| **Q-7** | Where settings live | One JSON in the user's Houdini preferences folder, with an environment variable override. |
| **Q-8** | Proxy options not in the spec | "Orient from points" stays, as a plain rule: the point's `orient` if present, else no rotation. No switch to force inline triangles. |
| **Q-9** | Show the bad elements | Yes, after M4: Validate puts offenders in temporary `__` groups (names defined by a spec revision), with the Python Panel for the selection browser. |
| **Q-10** | Flag editing | No flags node yet. Raw `a3ob_vertex_flags` and `a3ob_face_flags` can be edited with Attribute Wrangle. Revisit with real files (spec OQ-11). |
| **Q-11** | Other A3OB import options | None carried (absolute paths, translate selections, cleanup selections). Empty selections matter to `model.cfg` animations, so the spec keeps them. The `lods` filter stays. |
| **Q-12** | Scope of Lowercase | All four are lowercased: texture and material paths, selection names, and property names and values. The tooltip says so, and a warning appears when lowering the case makes two selection names equal. Spec section 17.1 (task #7). |
| **Q-13** | Skip invalid LODs | No. Export stops on errors. A model missing a LOD is worse than no model. |

## 11. Interpretations in this document

These are the places where this document filled a gap in D10 or the spec. All are listed above as parameters or
questions.

1. **Replace rule in Assemble LODs** (5.3, Q-4, resolved).
2. **"LODs" filter on Import** (5.1) from A3OB's "First LOD Only".
3. **Validate default level** and the `errorlevel` switch (5.8, Q-1), from the checked Houdini behaviour.
4. **Batch folder convert** means re-export with a report (7.4, Q-5).
5. **Mass node modes**, including copy from / to Houdini `mass` (spec §10) and the Weighted mode left out of version 1.
6. **Centre of mass** is a node message, not an attribute, because the spec defines no attribute for it.
7. **Proxy node inputs and placement parameters** (5.5): D10 says "path, index, placement"; the point input and
   auto-numbering are additions.
8. **Presets** `Lossless` and `Arma 3 release` (7.2, Q-5).
9. **Export options** `check`, `backup`, `dropuvs`, `czech`, `renumber` come from the spec's §16 and A3OB, not from D10.
10. **M3 and M4 split** of Validate, Selection browser and New P3D (section 9).
11. **Selection browser actions** (Create, Delete, Rename as Group nodes) are a version-1 proposal.

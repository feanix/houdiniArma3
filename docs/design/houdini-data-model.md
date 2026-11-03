# Houdini data model for P3D (M2 spec)

Status: draft for owner review, revision 2 (2026-11-03, after the M2 review). Normative for M3 (import) and M4 (export).

Revision 2 changes: the hou string boundary (§5, D-STR; aligned with M1's final codec by the lead); signatures defer to M1 and keep raw bits (§3.4); LOD
collection and packed transforms follow what Unpack actually does (§3.2); proxies carry their identity inside the
embedded geometry too (§13.3); pack points are defined by use (§5, §13.5); weight attribute defaults (§7.2); name
edge cases (§7.3); more fields kept or honestly levelled (§11, §15); validator and artist tooling plan (§17).

This is a design document for a GPL-3.0-or-later derivative of Arma 3 Object Builder by MrClock (present add-on) and
Hans-Joerg "Alwarren" Frieden (original ArmaToolbox). It translates A3OB's data tables and Blender mapping to Houdini,
and changes them where marked **(change)**.

## How to read this

- **`file:line`** refers to A3OB v2.5.1 @ 9f2b8bc, relative to `.reference/Arma3ObjectBuilder/Arma3ObjectBuilder/`.
- **`T#`, `U#`, `V#`** are Houdini 22.0.466 hython checks (`V#` added in revision 2), and **`R#`** are reproduced
  review findings. **`S#`** are fixture and pure-Python surveys. All are listed in [Evidence](#evidence); the scripts
  and their outputs are in `docs/design/evidence/m2/`.
- **M1** = the existing pure layer `python/a3ob_core` (`binary.py`, `p3d/model.py`, `p3d/lod_types.py`,
  `p3d/reader.py`, `p3d/writer.py`).
- **"docs: page"** is the offline Houdini 22.0.466 documentation, read through the `docs` tool.
- **L0** = byte-identical after import → export with no edits. **L1** = same P3D content, but bytes may differ.
  The goal is L0 on both fixtures.
- **OQ-n** = open question for the owner ([list](#open-questions-for-the-owner)).

## 1. Terminology

| P3D (A3OB code) | Houdini | Note |
|---|---|---|
| P3D file (MLOD v257) | **P3D geometry**: one SOP geometry with one packed primitive per LOD | §3 |
| LOD (`P3D_LOD`, P3DM 28.256) | **LOD geometry**: an ordinary (unpacked) geometry | §3 |
| vertex (`verts[i]`, `data_p3d.py:419`) | **point** `i` | point number = file vertex index |
| face (`faces[j]`) | **primitive** `j` (closed polygon, 3 or 4 vertices) | prim order = face order |
| face corner (`face[0][k]` + normal index + UV) | **vertex** (prim vertex) | |
| normals table (`normals`) | none; vertex attribute `N` | rebuilt on export (§9) |
| TAGG (`P3D_TAGG`) | attributes and groups (§6–§12) | |
| selection (named TAGG) | point group + optional weight attribute + optional prim group | §7 |
| proxy (`proxy:<path>.<NNN>` selection + triangle) | PackedGeometry primitive inside the LOD geometry | §13 |

## 2. Decisions at a glance

| # | Decision |
|---|---|
| D-LOD | Each LOD is an unpacked **LOD geometry**, and its per-LOD data lives in detail attributes. A P3D is a **P3D geometry**: one PackedGeometry primitive per LOD, in file order. The importer outputs the P3D geometry. The exporter takes explicit SOP path(s); it never scans the scene. |
| D-AXIS | `P_h = (x, y, −z)`. Polygon vertex order is reversed. `N_h = (−nx, −ny, nz)`. `uv_h = (u, 1 − v)`, stored as 64-bit floats. |
| D-SEL | Every selection becomes a point group named `hou.text.encodeAttrib(name)`. A point float `a3ob_w_<group>` is added only if some weight ≠ 1. A prim group of the same name is added only if the face bytes differ from the "all corners are members" rule. |
| D-RAW | Flags, the LOD header flags, named properties and unknown TAGGs are stored raw. A decoded view is optional and never authoritative. |
| D-PROXY | Each proxy becomes a PackedGeometry primitive. Its embedded geometry is the proxy triangle in proxy-local space, with all its attributes and groups. Its transform is A3OB's triangle frame expressed in Houdini axes. Prim attributes `a3ob_proxy_path` and `a3ob_proxy_index` carry the reference, on the packed prim **and** on the embedded triangle, so unpacking a proxy yields a valid inline proxy (§13.3). |
| D-STR | M1 strings are valid Unicode (cp1252, undefined bytes as C1) and go to `hou` unchanged; export encodes them back with M1's `encode_string`, and a character it cannot encode is a validator error (§5, V9). Lone surrogates crash Houdini (R1), and M1 never produces them. |
| D-PREFIX | Every custom attribute is named `a3ob_*`. The Houdini standard names `P`, `N` and `uv*` are used only where their Houdini meaning is exactly right (§14). |

## 3. P3D and LOD containers

### 3.1 Options

| Option | Per-LOD data | Merge / edit behaviour | Verdict |
|---|---|---|---|
| A. One SOP stream per LOD, with per-LOD data as detail attributes | Natural: one detail per LOD | Every Houdini SOP works unchanged. A P3D needs N streams, and merging two LODs keeps only the **first** input's detail values (T7c). | Good for **editing** a LOD |
| B. One geometry, with a per-prim `a3ob_lod` attribute | Must become per-prim attributes or detail arrays | Points cannot be shared between LODs. Groups and selections collide across LODs. Fuse, Normal and PolyReduce bleed across LODs. Loose points (Memory LOD) have no prim to carry the LOD id. | Rejected |
| C. One geometry, one PackedGeometry prim per LOD (embedded geometry = A) | Each embedded detail survives (T7, T7b) | Merging packed LODs keeps every LOD's detail (T7d). Unpacking one LOD gives back its detail attributes (T7e). Copies are cheap. | Good as the **container** |

**Recommendation: A for editing a LOD, C for the P3D.**

- **LOD geometry** = A. **P3D geometry** = C: a Merge of `Pack` SOP outputs, in LOD order.
- **Pack keeps the geometry in place.** The Pack SOP's packed prim has `fullTransform() == identity` and keeps the embedded detail attributes (T7b).
- **Unpack one LOD at a time.** Unpack's `detail_attributes` default is "Promote to Primitive if Different" (U8). Unpacking a single LOD keeps its detail attributes as detail (T7e). Unpacking several LODs at once would promote differing values to prim attributes.
- **Outer prim attribute `name`** (string) on each LOD prim holds the A3OB display name, `utilities/lod.py:27-31` (e.g. `Resolution 1`, `Geometry`). It is for Blast/Unpack by name only, and the exporter ignores it. Unpack does not copy outer attributes by default (`transfer_attributes` = `''`, U5).

### 3.2 LOD count, order and collection (replaces "scan for `is_a3_lod`")

A3OB exports every visible, parentless mesh with `is_a3_lod` (`export_p3d.py:26-37`, `335-337`). The port instead takes an explicit input list:

| Rule | Spec |
|---|---|
| Input | The exporter (ROP or shelf tool, M4) has a list of SOP paths, usually one. |
| LOD detection | For each SOP's cooked geometry, in list order:<br>• **A LOD geometry.** If the geometry itself has the detail attribute `a3ob_lod_type`, the whole geometry is one LOD. Any packed prims in it are proxies (§13), never LODs. Embedded proxy geometry never carries `a3ob_lod_type`.<br>• **A P3D geometry.** Otherwise, if **every** prim is packed and each unpacks (with the `unpack` SOP verb, so that PackedDisk and PackedFragment also work) to content with detail `a3ob_lod_type`, the prims are LODs, in prim order. Exception: **PackedFragment prims that share one source detail** (equal intrinsic `geometryid`, V6) are **one** LOD. They appear only when the Pack SOP's "Pack by Name" (`packbyname`) is on (default off, V6). They are unpacked together, at the position of the first fragment. Their prim and point order may differ from the source (L1, with a warning).<br>• **Errors.** A geometry that mixes packed LOD prims with loose polygons or points is an error ("unpack or blast them first"). So is a packed prim whose content lacks `a3ob_lod_type`, and so is a geometry where `a3ob_lod_type` is a prim attribute (the result of unpacking several LODs at once, §3.1). Anything else is an error too. |
| Count | Unlimited. The file's u32 LOD count = number of collected LODs + LOD copies (`data_p3d.py:787`, `811`). |
| Order | **Preserve the collected order** (L0). A3OB always sorts by signature (`export_p3d.py:716`); here sorting is an export option (OQ-3). |
| Packed transform | For a LOD that came from a packed prim:<br>1. Read `det = fullTransform()` 3×3 determinant from the LOD prim **before** unpacking.<br>2. Let the `unpack` verb bake the transform. Run it on a geometry that holds only that LOD prim (or only the fragments of one LOD), with `dotransform = 1` ("Apply Transform", default), `limit_iterations = 1` / `iterations = 1` (defaults, so nested proxies stay packed) and default `detail_attributes`. Unpack drops non-packed prims (docs: `nodes/sop/unpack`), which is one more reason mixed inputs are errors. It already transforms `P`, `N` (as a normal) and the transforms of nested proxy packed prims (V3: translate +5 moved `P`; V4: a nested proxy's `fullTransform()` = `P·T` exactly).<br>3. **The exporter never applies the LOD transform a second time.**<br>4. If `det < 0`, reverse the corner order of every polygon of that LOD when building P3D faces. Unpack does not do this: after a mirror-X, `prim.normal()` flips while `N` does not (V3). Warn when the transform is not identity (OQ-10).<br>Nested proxies with `det < 0` are handled in §13.5. |
| Duplicate signatures | After copies are expanded, a duplicate signature is an error by default. Option: skip or ignore, mirroring A3OB `lod_collisions` (`export_p3d.py:614-622`). |
| Object transform | Not applied: SOP space = P3D space. This mirrors the P3D origin = object origin rule. |

### 3.3 LOD identity and LOD-level data (detail attributes on the LOD geometry)

| Attribute | Class / storage | Default | Meaning and source |
|---|---|---|---|
| `a3ob_spec_version` | detail int32 | `1` | Version of this data model, for future migration. |
| `a3ob_lod_type` | detail int32 | `0` | M1 `LodType` 0..32, −1 = Unknown (= A3OB's enum, `data_p3d.py:258-292`, `utilities/data.py:67-101`). Its presence marks the geometry as a LOD. |
| `a3ob_lod_resolution` | detail float32 | `1.0` | Resolution or index (`props/object.py:189-204`): M1 `decode_signature`'s resolution, stored as f32 (§3.4). |
| `a3ob_lod_signature` | detail int32 | absent | The source signature's raw f32 bits, as two's-complement int32. Written at import; used on export only while `a3ob_lod_type` / `a3ob_lod_resolution` still describe it (§3.4). |
| `a3ob_lod_flags` | detail int32 (u32 bits) | `0` | LOD header flags (`data_p3d.py:483-484`). A3OB never uses them and writes 0 (`data_p3d.py:411`) **(change: preserved)**. |
| `a3ob_eof_active` | detail int32 | absent (→ 1) | Raw active byte of the LOD's `#EndOfFile#` TAGG (M1 `LOD.eof_active`). The importer creates it only if it is ≠ 1. |
| `a3ob_lod_copy_types` | detail int32[] | absent | LOD copies: extra signatures for the same mesh (`props/object.py:153-175`, `211-215`; `export_p3d.py:377-387`). |
| `a3ob_lod_copy_resolutions` | detail float32[] | absent | Parallel to `a3ob_lod_copy_types`. |
| `a3ob_prop_names`, `a3ob_prop_values` | detail string[] ×2, parallel | absent | Named properties, `#Property#` (§11). |
| `a3ob_prop_raw` | detail string[] (base64), parallel | absent | Raw 128-byte payload of each property (§11.1). |
| `a3ob_tagg_order` | detail string[] | absent | Order hint (§11.3). |
| `a3ob_tagg_active` | detail int32[], parallel to `a3ob_tagg_order` | absent (→ 1) | Raw active byte per ordered TAGG (§11.3). |
| `a3ob_tagg_unknown_names`, `a3ob_tagg_unknown_active`, `a3ob_tagg_unknown_data` | detail string[] / int32[] (raw byte 0..255) / string[] (base64) | absent | Opaque TAGGs (§11.2). |
| `a3ob_source_counts` | detail int32[2] | absent | Vertex and face counts at import, including proxies. Used to drop stale topology-bound data (§11.2, §9.2). |
| `a3ob_sharp_extra` | detail int32[] (flat pairs) | absent | `#SharpEdges#` pairs that are not polygon edges (§9.2). |

Constants that are not stored: `MLOD`/257 (`data_p3d.py:777-782`) and `P3DM`/28.256 (`data_p3d.py:472-478`).

**File-level data (P3D-geometry detail).** M1 keeps the bytes after the last LOD as `P3DModel.trailing`. Object
Builder writes none.

- **Storage.** The importer stores non-empty trailing bytes as detail string `a3ob_file_trailing` (base64) on the
  P3D geometry, i.e. the merge of the packed LODs.
- **Export.** The exporter writes them only when it has a single input SOP and that geometry carries the attribute.
  With several inputs it drops them with a warning.
- **Fragility.** A Merge keeps only its first input's detail (T7c), so a merged P3D geometry may silently gain or
  lose the attribute. The level is therefore L0 for an untouched container and L1 otherwise.

### 3.4 Signature storage **(change: raw bits kept; rules owned by M1)**

The data model does not restate the signature rules. It uses M1's `decode_signature` and `encode_signature`
(`a3ob_core/p3d/lod_types.py`):

- They are ports of A3OB `data_p3d.py:343-387`, without A3OB's `round()` (brief: bugs).
- Fixed types match within 1e-6 relative (`FIXED_SIGNATURE_TOLERANCE`).
- Resolutions are the shortest decimal that encodes back to the same f32.
- M1 `LOD.resolution_signature` holds the raw f32, and the M1 writer writes it as is.

**Import.** From `sig = lod.resolution_signature`:

- `a3ob_lod_signature` = the raw bits of `sig`;
- `(a3ob_lod_type, a3ob_lod_resolution) = decode_signature(sig)`, with the resolution rounded to f32.

**Export.**

1. **Reuse the stored bits while the view still matches.** If `a3ob_lod_signature` exists, let
   `(t, r) = decode_signature(from_bits(a3ob_lod_signature))`. If `t == a3ob_lod_type` and `f32(r)` equals
   `a3ob_lod_resolution` as **f32 bit patterns**, write the stored bits unchanged. Bit comparison makes NaN and
   −0.0 safe.
2. **Otherwise encode.** The view was edited, or the LOD was authored without the attribute: write
   `encode_signature(a3ob_lod_type, a3ob_lod_resolution)`. Its `ValueError` (out-of-range resolution, or an
   "Unknown" value that collides with a known type) becomes a validator error that names the LOD.
3. **LOD copies** (`a3ob_lod_copy_*`) always use `encode_signature`.

**Why the raw bits are needed.** Some signatures that M1 decodes as a fixed type do not re-encode to themselves:

- `f32(1e13·(1+3e−7))` decodes as Geometry, and `encode(GEOMETRY)` ≠ sig.
- `encode_signature(UNKNOWN, sig)` refuses that same value as "the signature of GEOMETRY".
- So no `(type, res)` pair can reproduce it. `f32(1e15·(1−5e−7))` (Memory) behaves the same way.

In S6, 9 of 200 000 random f32 bit patterns cannot be reproduced by re-encoding the decoded view. With the rule above,
200 000 / 200 000 patterns, both near-miss values and all 16 fixture LODs round-trip bit for bit (S6). The revision-1
"store Unknown with res = sig" fallback is dropped: M1 cannot encode it.

## 4. Coordinates, winding, normals, UV

### 4.1 Facts

| # | Fact | Ground |
|---|---|---|
| F1 | A3OB reads a vertex as file `(x, y, z)` and gives Blender `(x, z, y)`: swap Y/Z, no change to face index order. Writing applies the same swap. | `data_p3d.py:429-431`, `509-510`, `464` |
| F2 | A3OB gives Blender normal `n_b = −swap(n_f)`. Models display correctly in Blender, so the outward normal in file space is `−n_f`. | `data_p3d.py:437-439`, `516-517` |
| F3 | In both fixtures `dot((v1−v0)×(v2−v0), n_f) > 0` for every face checked, so stored normals follow the algebraic cross product of file corner order. With F2: outward = `−(e1×e2)` in file space, i.e. the file is left-handed with clockwise front faces. | S2 |
| F4 | Houdini is Y-up, right-handed. | docs: `unreal/coordinates` |
| F5 | A Houdini polygon with algebraic cross `+Z` gets `prim.normal() = (0,0,−1)`, and the Normal SOP agrees (point, vertex and prim modes). So Houdini's front = `−(e1×e2)`: clockwise seen from the front, the reverse of Blender. | T1, U3 |
| F6 | Fixture character: left wrist at file `+X` (`wristleft (0.593, 0.065, 0.068)`); toes toward file `−Z` (foot z −0.129..0.119, lower leg −0.036..0.119); up `+Y`. So it faces file `−Z`. | S2, S5 |
| F7 | File UV `v` runs top-down; A3OB flips it, `v' = 1 − v`, on read and write. | `data_p3d.py:455`, `118`, `128`; `export_p3d.py:460` |
| F8 | Houdini UV `v` increases upward: UDIM tile 1011 is "up by 1 unit in V". | docs: `solaris/udim` |
| F9 | Houdini matrices use row vectors: `p_world = p_local * M`, with the translation in row 4. | T8, U6 |

### 4.2 Derivation

1. **Choose the axis map.** File space is left-handed and Houdini is right-handed (F3, F4). An unmirrored conversion therefore needs a map with det = −1, i.e. one negated axis. Take **`M = diag(1, 1, −1)`**:
   - The model's left stays at +X and up stays at +Y, so the F6 character faces **+Z** in Houdini.
   - It is also what you get by taking A3OB's Blender coordinates (F1) through the standard Blender→Houdini Y-up rotation `(x_b, z_b, −y_b)`. Assets that went A3OB → Blender → FBX/Alembic → Houdini therefore land in the same place.
   - The alternative `diag(−1, 1, 1)` makes the model face −Z (OQ-1).
2. **Winding.** For an orthogonal diagonal M with det −1, `(M e1) × (M e2) = −M (e1 × e2)`.
   - Keeping file order, Houdini's front would be `−(−M(e1×e2)) = M(e1×e2) = −M·outward`. That points inward, so the order **must be reversed**.
   - After reversal, Houdini's front = `−M(e1×e2) = M·outward` ✓.
   - Blender, with the same det −1 swap and no reversal, gets counter-clockwise fronts. That is consistent with Blender's convention (F1, F5).
3. **Normals.** `N_h = M·(−n_f) = (−nx, −ny, +nz)`.
   - Check: after reversal, Houdini's own face normal is ∥ `−M(e1×e2)` ∥ `−M n_f` (F3) = `N_h`.
   - So the imported `N` agrees with what a Normal SOP would recompute ✓.
4. **UV.** The file is top-down (F7) and Houdini is bottom-up (F8), so `v_h = 1 − v_f`, the same flip as Blender.

### 4.3 Transform table (normative; import → Houdini, export = exact inverse)

| Quantity | Import | Export | L0? |
|---|---|---|---|
| Position | `P = (x, y, −z)` | `(Px, Py, −Pz)` | yes: negation is exact in f32 |
| Corner order of an n-gon | Houdini vertex `k` = file corner `(−k) mod n`, i.e. `[c0, c(n−1), …, c1]`; vertex 0 stays file corner 0 | Same map (it is its own inverse) | yes |
| Corner normal | `N = (−nx, −ny, nz)`, vertex float32, `transform_as_normal=True` (qualifier "Normal", U5). **No renormalisation** **(change:** A3OB renormalises on read (`data_p3d.py:488`, `572-584`); the fixtures' normals have length 0.99995 (S2), so renormalising would change bytes) | `(−Nx, −Ny, Nz)` | yes |
| UV `(u, v)` | `uv = (u, 1 − v, 0)`, vertex **float64** | `(uv.x, 1 − uv.y)`, cast to f32 | yes for `v = 0` or `abs(v) ≥ 2⁻³⁰` |

- **UV precision.** With float32 storage, `f32(1 − f32(1 − v)) ≠ v` for 33 388 of 100 000 random v; with float64 storage, 0 mismatches (T4c).
- **Making a float64 attribute.** HOM `addAttrib` always makes float32 (U2). A float64 attribute comes from the `attribcreate::2.0` SOP verb with `precision# = 3` (64-bit). This works on a standalone `hou.Geometry` (U7), and float64 survives Python SOP writes, Transform, `Geometry.merge` and bgeo save/load (U2).
- **UV type info.** Set `uv*` attributes to `setOption("type", "texturecoord")` → qualifier "Texture Coord" (U5).

## 5. Points, vertices, primitives (LOD geometry)

| P3D field | Houdini | Class / storage | Default if attribute absent at export | Source |
|---|---|---|---|---|
| vertex position | `P` | point float32×3 | n/a | `data_p3d.py:419`, `429-431` |
| vertex flags (u32) | `a3ob_vertex_flags` | point int32, bits as two's complement | `0x02000000` for all points; attribute default `33554432` | `data_p3d.py:419`; brief: flags |
| vertex mass (`#Mass#`) | `a3ob_mass` | point float32 | no `#Mass#` TAGG | §10 |
| selection vertex weight ≠ 1 | `a3ob_w_<enc>` | point float32 | weight 1.0 for members | §7 |
| corner normal | `N` | vertex float32×3 | computed (§9.1) | §4 |
| UV set k | `uv` (k = 0), `uv<k+1>` (k ≥ 1) | vertex **float64**×3 | no TAGG; face-data UV = (0,0) | §8 |
| face-data UV, if different from set 0 | `a3ob_face_uv` | vertex float64×3 | = `uv` if present, else (0,0) | §8 |
| face flags (u32) | `a3ob_face_flags` | prim int32 | `0` | `data_p3d.py:460`, `533` |
| face texture | `a3ob_texture` | prim string (raw file string) | `""` | `data_p3d.py:461`, `534` |
| face material (rvmat) | `a3ob_material` | prim string (raw file string) | `""` | `data_p3d.py:462`, `535` |
| selection face weight ∉ {0,1} | `a3ob_fw_<enc>` | prim float32 | weight 1.0 for members | §7 |
| inline proxy reference | `a3ob_proxy_path`, `a3ob_proxy_index` | prim string / int32 | `""` / `0`: not a proxy | §13.3, §13.6 |
| sharp edges | edge group `a3ob_sharp` | edge group (ordered, directed) | no TAGG | §9.2 |
| selection membership | point group `<enc>`; prim group `<enc>` | groups | — | §7 |

### Value rules

- **u32 in int32.** Houdini int attributes are signed 32-bit: `0xfe000000` is stored as `−33554432` (T4). Import converts `v ≥ 2³¹` to `v − 2³²`; export uses `value & 0xFFFFFFFF`.
- **Strings (normative, D-STR).**
  - **A3OB.** It decodes as UTF-8 with `errors="replace"` and writes ASCII (`io/binary_handler.py:84`, `146`), so it is lossy, or crashes on non-ASCII.
  - **M1.** `decode_string` / `encode_string` (`a3ob_core/binary.py`) use **cp1252**, with the 5 bytes cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) mapped to U+0081/8D/8F/90/9D, as Windows does. Every byte 0x01–0xFF round-trips and the result is always valid Unicode: no lone surrogates. `encode_string` raises on any character outside that set.
  - **Why it matters.** An earlier M1 codec (cp1252 + `surrogateescape`) produced lone surrogates, and Houdini cannot take them: `setAttribValue` kills hython 22.0.466 with a segmentation fault (R1: exit 139), and `hou.text.encodeAttrib` raises `TypeError` (V1). M1 no longer does this.
  - **Boundary (in `a3ob_houdini`).** M1 strings go to `hou` **unchanged** on import. On export, hou strings go back through `encode_string`. A character it cannot encode (e.g. an artist typed `ř`) is a **validator error** that names the attribute and element. It is never silently replaced.
  - **Result.** The map from file bytes to hou strings is a bijection. V9 checks all 255 non-NUL bytes for prim strings, detail string arrays and encoded group names (`encodeAttrib` → `createPointGroup` → `decodeAttrib`).
  - **Display.** Text shows as cp1252: `é`, `ß`, `€`, `Š` look right. The 5 undefined bytes show as C1 control characters. Czech CP1250 text would show as cp1252 look-alikes; the real code page of Arma files is still open (OQ-5).
  - **Scope.** It applies to `a3ob_texture`, `a3ob_material`, `a3ob_prop_names` / `a3ob_prop_values`, selection names (before `encodeAttrib`), `a3ob_proxy_path`, `a3ob_tagg_order` tokens, `a3ob_tagg_unknown_names` and the LOD `name` label. The pure layer stays hou-free.
- **Paths.** Stored exactly as in the file (backslashes, relative, no forced case). Lowercasing (`export_p3d.py:720-722`) and relative/absolute conversion (`utilities/generic.py:174-216`) are export options, off for a lossless round trip.
- **Polygon sides.**
  - P3D faces have 3 or 4 sides; triangles carry a 16-byte zero pad (`data_p3d.py:457-458`, `530-531`). The pad is not stored and is written as zeros; all fixture pads are zero (S2).
  - The exporter rejects polygons with more than 4 vertices, open polygons and non-polygon prims, other than proxy packed prims (validator, M4).
  - **Faces with 0–2 sides.** M1 reads them (it only rejects more than 4, `reader.py:173`). The importer makes closed polygons with that many vertices (the §4.3 map also works for n ≤ 2). The exporter writes 0–2-vertex closed polygons as such faces, with a validator warning. Their unused slots are not stored, and SOP survival of such polygons is not verified, so the level is **L1**.
- **Loose points.** Points referenced by no polygon (e.g. Memory, Land Contact, Hit-points LODs: S1) are ordinary points and are exported.
- **Pack points (normative).** A **pack point** is a point referenced by one or more packed prims and by **no polygon**. Pack points are not exported.
  - A point shared by a packed proxy and a polygon is exported as an ordinary mesh vertex. Fuse does this with default settings: `fuse::2.0` merged a proxy's pack point at the origin into mesh point 0 (4 → 3 points; V5, R2).
  - A proxy never uses its pack point. It uses only its embedded geometry and `fullTransform()` (§13.5).
  - Validator warning: "point N is shared by proxy prim M and polygons".
- **Interpolation hazard.** SOPs that interpolate point attributes (Subdivide, Resample, PolyExtrude fills) average int bitfields into meaningless flags. The M4 validator should warn on flag values that are not on the target LOD's known list.

## 6. Element order

| P3D order | Houdini | Export rule |
|---|---|---|
| vertex index | point number | Points in point-number order, skipping pack points (points used only by packed prims, §5); then, per packed proxy in prim order, its 3 points in embedded point order (§13.5). |
| face index (= section order, "sort sections") | prim number | Polygons in prim order, including inline proxy triangles in place; then packed proxy triangles appended in packed-prim order. Section sorting (`export_p3d.py:255-272`, `374-375`) is an export option, off by default. |
| corner order | vertex order within the prim (§4.3 map) | inverse map |
| normals table order | none | rebuilt in first-appearance order (§9.1) |
| TAGG order | `a3ob_tagg_order` hint | §11.3 |

In the character fixture all 11 proxies are the **last** 33 vertices and the **last** 11 faces of LOD 0 (S1, S2: backpack = vertices 5346–5348, face 10688 of 10699). The append rule therefore reproduces the file order.

## 7. Named selections

### 7.1 P3D facts

- **Layout.** A selection TAGG holds one byte per vertex, then one byte per face (`data_p3d.py:131-182`).
- **Weights.** Byte `b`: `0` = not a member; `1..255` → weight `(255 − b) / 254`. The special case `b = 1` → 1.0 gives the same value (`data_p3d.py:139-152`). So **byte 255 = member with weight 0.0**.
- **A3OB loses byte 255.** Its encode maps 0.0 → byte 0. The character's LOD 0 has 304 vertex bytes of 255 (S1) **(change: preserved)**.
- **A3OB skips face bytes on read** (`data_p3d.py:163`) and rebuilds them as "face is a member iff all its vertices are" (`export_p3d.py:546-553`).
  - In the character, **376 face bytes in LOD 0 differ** from that rule (all of them `face=1, derived=0`, on `spine2`, `rightarmupper`, `hip`, …), so face bytes carry real data (S1, S2) **(change: preserved)**.
  - No fixture face byte is outside {0, 1} (S1).
- **Names.**
  - A selection is any TAGG whose name neither starts nor ends with `#` (`data_p3d.py:254-255`).
  - Names include `-proxies` and `proxy:\a3\characters_f\proxies\backpack.001` (S1). They may contain spaces, dots, colons and backslashes.
  - Names are case-preserving.

### 7.2 Representation **(D-SEL)**

| Data | Houdini | Created by importer when | Export rule |
|---|---|---|---|
| vertex membership | **point group** `G = hou.text.encodeAttrib(name)` | always, even if empty | byte = 0 for non-members |
| vertex weight | point float32 `a3ob_w_G`, **attribute default 1.0** | some member byte ≠ 1 | member: `clamp(round(255 − 254·w), 1, 255)`; attribute absent → 1. All 255 bytes survive decode → f32 → encode (S4). |
| face membership | **prim group** `G` | the face bytes differ from the derived rule | prim group present → it is authoritative; absent → derived rule (all corner points in `G` → 1) |
| face weight | prim float32 `a3ob_fw_G`, **attribute default 1.0** | some face byte ∉ {0,1} | same formula as vertex weight |

**Weight defaults (normative).** `a3ob_w_*` and `a3ob_fw_*` are always created with default value 1.0
(`addAttrib(…, 1.0)`; the Attribute Create verb with default 1).

- **Why.** With the `addAttrib` / Attribute Create default of 0, any point an artist later adds to `G`, or any point a
  SOP creates, would export as byte 255: a member with weight 0.
- **Effect.** With default 1.0, a point created later reads weight 1.0 and exports as byte 1 (V7).
- **Non-members.** The export rule only reads the weight of group members, so a 1.0 on a non-member is harmless.
- **Interpolating SOPs.** They still interpolate weights (Subdivide gives 0.625 between 0.25 and 1.0, V7), which is
  the intended meaning of a weight.

Why this shape:

- **Groups for membership.** They are what users Blast, select and view.
- **Weights only where needed.** Most selections are all-1.0 (components, hidden selections, proxies; S1). This avoids hundreds of float attributes.
- **Explicit face groups only where they carry information.** Elsewhere, editing a point group keeps face membership consistent automatically.
- **Group alone loses information.** A group alone loses weights; a float alone cannot tell byte 0 from byte 255.

Further rules:

- **Which groups are selections (export).**
  - **Default.** Every point group except names starting with `__` (Houdini temporary convention) is a selection. The M4 export option is "selection mask", default `* ^__*`.
  - **Exception.** A point group whose **decoded** name appears in `a3ob_tagg_order` is a selection whatever the mask says. `encodeAttrib("__foo")` returns `__foo` unchanged (V2), so without this rule a real imported selection named `__foo` would be dropped silently.
- **Prim groups count only with a partner.** A prim group counts as a face selection only if a point group of the same name exists. The importer creates an empty point group for face-only selections. `a3ob_w_X` / `a3ob_fw_X` without group `X` are ignored with a warning, so stray PolyExtrude prim groups do not leak into the file.
- **Namespaces.** Point groups, prim groups and point attributes of the same name coexist (T3).

### 7.3 Name mapping (lossless)

- **Houdini rules.** Attribute and group names must be `[A-Za-z_][A-Za-z0-9_]*`. `-proxies`, `a b`, `x.y`, `1abc`, `proxy:\…` and `ň` all fail `addAttrib` / `createPointGroup` with `OperationFailed` (T2).
- **Mapping.** Use Houdini's own reversible encoding, **`hou.text.encodeAttrib` / `decodeAttrib`** (docs: `hom/hou/text`):
  - Valid names pass unchanged: `head`, `pos_driver`, `Component01`, `Light_L` (T2, T9).
  - Other names become `xn__…`: `-proxies` → `xn__proxies_cka`; `pos driver` → `xn__posdriver_7ia`; a 264-character proxy path → 276 characters, accepted as a group name (T2).
  - Names that already start with `xn__` are encoded again, so decode(encode(s)) == s for every string (docs; T2 `xn__abc`).
- **Where it runs.** The pure layer (`a3ob_core`) keeps raw names and never encodes. The hou glue encodes M1's name on import, and decodes it then checks it with `encode_string` on export (D-STR, §5). Every name made of bytes 0x01–0xFF survives (V9).
- **Weight attribute names** are `"a3ob_w_" + G`; the export strips the prefix and decodes.

**Edge cases (normative).**

| Case | Houdini fact | Rule | Level |
|---|---|---|---|
| Empty name `""` | M1 `is_selection_name("")` is True (`model.py:72`), but `encodeAttrib("")` is `""` and `createPointGroup("")` raises `OperationFailed` (V2) | The importer stores the TAGG as an opaque blob (§11.2) and warns. It is not a group. | L0 while counts are unchanged; dropped otherwise |
| Exact duplicate names | Two TAGGs would collide on one group | The first becomes the group; later duplicates are stored as blobs (§11.2), with a warning | L0 while counts are unchanged; dropped otherwise |
| Names that differ only in case (`Head`, `head`) | Houdini groups are case-sensitive and both coexist (V2). P3D lookup is case-insensitive (M1 `LOD.selection`, `model.py:510-520`) | Both import as separate groups and export as two selections, as in the source. The validator **warns** about case-insensitive duplicates, which Object Builder / the game may treat as one selection. | L0 |
| Names starting with `__` | `encodeAttrib` keeps them unchanged (V2) | Exported because of the `a3ob_tagg_order` exception (§7.2) | L0 |
| Names starting with `xn__` | encoded again (T2) | normal | L0 |

## 8. UV sets

| P3D | Houdini | Rule |
|---|---|---|
| `#UVSet#` id k: u32 id + 2 f32 per corner, in face/corner order (`data_p3d.py:107-128`) | vertex `uv` (k = 0) or `uv<k+1>` (k ≥ 1), float64×3 | id ↔ name is fixed: id 1 → `uv2`, id 3 → `uv4`. Gaps are allowed. Export writes one TAGG per `^uv(\d*)$` vertex float attribute (size ≥ 2), in ascending id order. **(change:** A3OB renames layers to ids 0..n−1 by layer order (`export_p3d.py:566-571`).) A **repeated id** cannot share one attribute name: the first occurrence becomes the attribute and later ones are stored as blobs (§11.2), with a warning. |
| UV embedded in face data (`data_p3d.py:452-455`) | = set 0 unless `a3ob_face_uv` exists | A3OB takes set 0 from face data and overrides it with TAGG 0 (`data_p3d.py:702-710`); on export it writes layer 0 into face data (`export_p3d.py:446-460`). The importer creates `a3ob_face_uv` only if face UVs ≠ TAGG 0, or if TAGG 0 is absent and some face UV is non-zero. Fixtures: face UV == TAGG 0 everywhere (S2), and the LODs without a TAGG have all-zero face UVs (S4), so no `a3ob_face_uv` is created. |
| no `#UVSet#` TAGG | no `uv*` attribute | export writes no TAGG and (0,0) face UVs |

The A3OB rule that drops UVs on non-UV LOD types (`export_p3d.py:275-277`, `utilities/data.py:150`) is an export option, off for a lossless round trip.

## 9. Normals and sharp edges

### 9.1 Normals

- **Import.** `N` (vertex) is imported for **every** LOD type, from the file's corner normal indices.
  - **(change:** A3OB applies normals only to visual LODs (`import_p3d.py:337`) and recomputes them on export for the other LOD types (`export_p3d.py:280-291`).)
- **Export with `N`.**
  - Read vertex `N`, or else point `N`, or else prim `N`, mapped per §4.3.
  - Build the normals table in **first-appearance order** over faces in file corner order, de-duplicated by exact f32 value.
  - This is A3OB's scheme (`export_p3d.py:409-421`). On both fixtures it reproduces the stored table exactly: all values unique, all referenced, first-appearance order (S2) → L0.
  - A file with duplicate or unreferenced table entries round-trips at L1.
  - The table is shared by mesh and proxy faces. If a proxy corner normal has the same f32 value as a mesh normal, it shares that index in the rebuilt table. A source file that stored it as a separate entry therefore round-trips at L1.
- **Export without `N`.** Area-weighted vertex normals, split across `a3ob_sharp` edges (M4 implements this in pure Python).
  - Houdini's Normal SOP has no hard-edge-group input: its parms are `cuspangle`, `method`, … (U3). So it cannot honour `a3ob_sharp` directly.

### 9.2 Sharp edges (`#SharpEdges#`: flat u32 point pairs, `data_p3d.py:34-64`)

| Case | Houdini | Notes |
|---|---|---|
| pair that is a polygon edge | edge group **`a3ob_sharp`** | Edge groups keep insertion order **and** direction through bgeo save/load (U4). They survive Transform, Fuse and PolyExtrude (T6). Fixtures: every pair is a real edge, none degenerate or duplicate, each matches an a→b corner traversal (S3). |
| pair that is not an edge, or degenerate (a == a) | detail `a3ob_sharp_extra` int32[] | `findEdge` returns `None` for non-edges (U4). Exported only while `a3ob_source_counts` matches; otherwise dropped with a warning. A3OB silently drops degenerate pairs (`data_p3d.py:49-50`). |
| TAGG present but empty | **empty** edge group `a3ob_sharp` | Every fixture LOD has an empty or non-empty `#SharpEdges#` (S1). Export writes the TAGG iff the group exists. This avoids A3OB's "always write" bug (`export_p3d.py:493`) while keeping L0. |
| export order | group order, then extras | |

- A3OB's "flat-shaded → all edges sharp" export shortcut (`export_p3d.py:483-489`) is **not** used: there is no smooth/flat face flag in Houdini.
- Vertex `N` carries the shading. `a3ob_sharp` carries the P3D topology hint.

## 10. Mass

- **P3D.** `#Mass#` is one f32 per vertex (`data_p3d.py:89-104`); its length is 4·nverts (S3).
- **A3OB.** Imports and exports it only on the Geometry LOD, as the `a3ob_mass` layer (`import_p3d.py:150-162`, `368`; `export_p3d.py:578-583`).
- **Port.** Point float32 **`a3ob_mass`**, imported for any LOD that has the TAGG. The TAGG is written iff the attribute exists. The validator warns if it exists on a non-Geometry LOD.
- **Why not Houdini `mass`?** It is a simulation attribute: Vellum Configure nodes compute "particle mass" (docs: `nodes/sop/vellumconstraints_grain`, "Physical Attributes"). A stray `mass` from a sim setup would silently emit a `#Mass#` TAGG. A helper can copy `a3ob_mass` ↔ `mass` (OQ-4).

## 11. Named properties, unknown TAGGs, TAGG order

### 11.1 Named properties (`#Property#`: two 64-byte ASCIIZ fields, `data_p3d.py:67-86`)

- **Storage.** `a3ob_prop_names[i]` and `a3ob_prop_values[i]`, parallel detail string arrays (T4), in file order.
- **Why not a dict.** A dict attribute (also works, T4) would lose order and duplicate keys.
- **Limits.** Maximum 63 characters each (`props/object.py:28-42`).
- **Bytes after the NUL.** A3OB drops the bytes after the NUL inside a fixed field (`io/binary_handler.py:86-95`). M1's `PropertyTagg` keeps the raw 128-byte payload until key or value change (`model.py:160-189`).
  - **Storage.** The importer stores it as `a3ob_prop_raw[i]` (base64), parallel to the name and value arrays, but only when it differs from the canonical NUL-padded form. Otherwise the entry is `""`. If no property has a tail, the attribute is absent.
  - **Export.** If `a3ob_prop_raw[i]` is non-empty and its decoded key/value equal the current `a3ob_prop_names[i]` / `a3ob_prop_values[i]`, the raw payload is written. Otherwise the canonical form is written. Either way the result is **L0** for an unedited property.
  - **Fixture values:** `lodnoshadow=1`, `autocenter=0`, `buoyancy=1`, `class=man` (S1).

### 11.2 Unknown and inactive TAGGs **(change: preserved)**

- **What A3OB drops.** It skips every TAGG it does not parse (`#Animation#`, `#Lock#`, `#Selected#`, `#Hidden#`, …) and every TAGG with `active = 0` (`data_p3d.py:228-230`, `500-501`).
- **What M1 keeps.** It keeps all of them. Unknown names become `RawTagg`. Known names are parsed whatever their active byte, and the raw byte 0..255 is kept in `Tagg.active` (`reader.py:128-145`, `model.py:100-126`).
- **Which TAGGs become blobs.** A TAGG is stored as a blob when any of these holds:
  - M1 gives a `RawTagg` (unknown name, or a known TAGG that M1 kept raw in non-strict mode);
  - its active byte is 0 (an *inactive known* TAGG: importing it as a live group or attribute would change its meaning);
  - it is a repeated `#UVSet#` id (§8);
  - it is an exact-duplicate or empty selection name (§7.3).
- **Port storage.** Each blob is one entry in three parallel detail arrays:
  - `a3ob_tagg_unknown_names[i]` (raw M1 name);
  - `a3ob_tagg_unknown_active[i]` (the **raw active byte** as int32, 0..255);
  - `a3ob_tagg_unknown_data[i]` (base64 of the raw payload, M1 `tagg.payload()`).
- **Live TAGGs with other active bytes.** A known, live TAGG whose active byte is neither 0 nor 1 keeps its normal representation; its byte goes to `a3ob_tagg_active` (§11.3).
- **Why base64.** Houdini string attributes cannot hold NUL bytes. These TAGGs are usually sized per vertex or face.
- **Staleness rule.** Export writes them unchanged only while the current vertex/face counts equal `a3ob_source_counts`; otherwise it drops them with a warning. This applies to every blob, including the ones (an inactive `#Property#`, say) whose size does not depend on the counts. That is simple and conservative: blob round trips are **L0 while counts are unchanged**.
- **Fixtures** contain none (S2), so this path needs a synthetic test in M3/M4.

### 11.3 TAGG order hint

- **What A3OB writes.** `#SharpEdges#`, `#UVSet#`…, `#Property#`…, `#Mass#`, then selections (`export_p3d.py:558-588`). Both fixtures use exactly this order (S2).
- **Port storage.** `a3ob_tagg_order` stores the file order as tokens:
  - `#SharpEdges#`, `#UVSet#<id>`, `#Property#` (one per entry), `#Mass#`;
  - the raw selection name;
  - `#?<i>` for unknown blob i.
- Real TAGG names end in `#` and selection names cannot (`data_p3d.py:254-255`), so tokens never collide.
- **Active bytes.** `a3ob_tagg_active[j]` is the raw active byte of the TAGG behind `a3ob_tagg_order[j]`.
  - The importer creates the array only if some non-blob TAGG has a byte ≠ 1. Blobs keep theirs in `a3ob_tagg_unknown_active`.
  - On export, a TAGG reached through a hint token takes the token's active byte. Every other TAGG (new data, or no array) gets 1, which is what Object Builder and A3OB write.
- **Export:**
  1. Emit the tokens whose data still exists, in hint order.
  2. Then everything else in the canonical order above, with selections in point-group order and unknown blobs last.
- The hint only **orders**; it never creates data. Stale entries are skipped.
- `#EndOfFile#` is always written last (`data_p3d.py:561-563`), with active byte `a3ob_eof_active` (default 1).

## 12. Textures, materials, sections

- **Storage.** Per-face raw strings `a3ob_texture` and `a3ob_material` (prim string; table §5). These include procedural textures such as `#(argb,8,8,3)color(r,g,b,a,TYPE)` and are not parsed.
  - A3OB parses them into a material property group (`props/material.py:55-97`). That is a UI concern; M-later can add a decoded view.
- **No Blender material slots.** The sections are the runs of equal `(texture, material)` in prim order (`data_p3d.py:608-631`). Prim order is preserved (§6).
- **Houdini materials.** The Houdini `shop_materialpath` / `material` attributes are **not** used for P3D data. They reference Houdini material nodes; a viewport-preview helper may set them from `a3ob_texture`.

## 13. Proxies

### 13.1 P3D facts

- **Name.** A proxy is a selection named `proxy:<path>.<index>`: regex `proxy:.*\.\d+` (`data_p3d.py:247-252`), written as `"proxy:%s.%03d"` (`data_p3d.py:697`).
- **Path.** No extension, because export strips it (`props/object.py:258-263`, `utilities/generic.py:206-216`); import re-adds `.p3d` (`import_p3d.py:260`).
- **Geometry.** One triangle. Index 0–999 (`props/object.py:249-255`).
- **Proxy vertices belong to other selections too**, e.g. `-proxies`, `injury_hands`, `lefthand` (S3). A3OB keeps these through separate/join (`import_p3d.py:252`; `export_p3d.py:179-207`). Proxy vertices and faces share no face with other geometry (S3).
- **A3OB rewrites proxy data on export.** It clears proxy UVs and zeroes proxy flags (`export_p3d.py:192-197`). Fixture proxies already have flags 0 (S2).

### 13.2 Representation options

| Option | Lossless | Houdini-native | Verdict |
|---|---|---|---|
| Packed **disk** prim referencing the proxy path | No: no per-vertex data. The path is a game path to a `.p3d`, which Houdini cannot load (docs: `model/packed`, "Packed disk primitives"). | partly | Rejected |
| Packed **fragment** | It references a named piece of a shared source geometry (docs: `model/packed`, "Packed fragments") and couples proxies to the LOD's points. | yes | Rejected |
| Point carrying a transform (`orient`/`transform`) + path | No: loses the triangle's groups, N, uv and flags. | yes (instancing) | Use only as a derived view for instancing |
| Inline triangle with prim attributes `a3ob_proxy_path`/`index` | Yes, trivially: order and data stay in place. | weak: moving a proxy means moving 3 points | Fallback (§13.6); alternative for OQ-2 |
| **PackedGeometry prim** (in-memory) | Yes: the embedded geometry holds the triangle with all its attributes and groups (T7: groups and detail survive embedding). It has a transform intrinsic (`transform`, `packedfulltransform`; T7). | yes: one prim, transformable, Unpack-able | **Recommended** |

### 13.3 Packed proxy, normative

| Item | Spec |
|---|---|
| Prim | `geo.createPacked("PackedGeometry")` + `setEmbeddedGeometry(inner.freeze())` (T7; docs: `hom/hou/PackedPrim`). Created **after** all polygons and loose points, so mesh point numbers stay 0..n−1. Proxy prims are created in **ascending file face index** of their proxy face. That is the order export appends them in (§6), so tail-ordered proxies keep their face and vertex order (L0). |
| Outer prim attributes | `a3ob_proxy_path` (string, raw M1 string, e.g. `\a3\characters_f\proxies\backpack`); `a3ob_proxy_index` (int32, e.g. 1); `a3ob_proxy_src_P` (float32[9]); `a3ob_proxy_src_N` (float32[9]). The last two are the snap guard (§13.5), defined below. |
| Snap-guard values | Both arrays hold **file-space** f32 values, i.e. exactly as stored in the P3D: no axis map, no normal negation.<br>• `a3ob_proxy_src_P` = x, y, z of the 3 proxy vertices, in **ascending file vertex index** (= embedded point order).<br>• `a3ob_proxy_src_N` = x, y, z of the proxy face's corner normals, in **file corner order** (corner 0, 1, 2). |
| Embedded geometry | 3 points + 1 closed polygon, in proxy-local coordinates. Embedded point order = ascending file vertex index. Polygon vertex order = the §4.3 map of the file corner order.<br>It carries the same attribute scheme as a LOD: `a3ob_vertex_flags`, `N`, `uv*`, `a3ob_face_flags`, `a3ob_texture`, `a3ob_material`, point groups (selections other than the proxy's own) and their weights.<br>It **also** carries prim attributes `a3ob_proxy_path` and `a3ob_proxy_index` on the triangle, with the same values as the outer prim. This is exactly the inline form of §13.6, so unpacking a proxy (Unpack's default `transfer_attributes` is `''`, U5) still yields a valid inline proxy: V4 shows that the embedded values survive a full unpack.<br>It never carries the detail `a3ob_lod_type` (§3.2). |
| Transform | `prim.setTransform(Matrix4(rows X, Y, Z, C))` (row-vector convention, F9). `setTransform` sets the 3×3 part and the point translation (docs: `hom/hou/PackedPrim`). |
| Proxy's own selection | Not a group. It is rebuilt on export from path + index: outer attributes first, embedded attributes if the outer ones are absent or empty. When both are present and differ, the **outer** values win, because the spreadsheet shows and edits the outer prim. The validator warns about the mismatch. |

### 13.4 Frame (derived from A3OB `utilities/proxy.py:9-29`)

**1. A3OB's rule, in Blender space** (`proxy.py:9-17`):

- Compute the interior angle at each vertex and sort descending.
- `C` (largest angle) = origin.
- `A` (second-largest angle) = end of the **short** leg. In a right triangle it is the vertex opposite the long leg.
- `B` (smallest angle) = end of the **long** leg.
- Axes: `y_b = n(A−C)`, `z_b' = n(B−C)`, `x_b = n(y_b × z_b')`, `z_b = n(x_b × y_b)`. `n()` = normalise. So `y_b` is the short leg, kept exact.

**2. Into Houdini axes.** Blender → Houdini is `R(x_b, y_b, z_b) = (x_b, z_b, −y_b)`. This is `M`∘(Y/Z swap) from §4.2, a proper rotation (det +1), so it preserves cross products. A proxy model's own local axes convert the same way. With `p_local_b = (x, y, z)` → `p_local_h = (x, z, −y)`:

- `X_h = R x_b`
- `Y_h = R z_b` (long leg)
- `Z_h = −R y_b` (minus the short leg)
- Check: `Y_h × Z_h = −R(z_b × y_b) = R x_b = X_h`, right-handed ✓.

**3. Normative Houdini formula** (all points in Houdini LOD space):

```
C, A, B = angle-sorted triangle points (largest, middle, smallest interior angle)
Z  = −normalize(A − C)          # short leg points along local −Z (kept exact, as A3OB keeps y_b)
Y' =  normalize(B − C)          # long leg ≈ local +Y (proxy "up")
X  =  normalize(cross(Y', Z))
Y  =  normalize(cross(Z, X))
M4 = rows (X,0), (Y,0), (Z,0), (C,1)
embedded local p = (p − C) · [X; Y; Z]ᵀ      (orthonormal, so inverse = transpose)
```

- **Check: the canonical triangle gives the identity.** A3OB's `create_proxy` triangle in Blender, `(0,0,0)`, `(0,0,2)`, `(0,1,0)` (`proxy.py:34`), is `C=(0,0,0)`, `B=(0,2,0)`, `A=(0,0,−1)` in Houdini. The formula gives X=(1,0,0), Y=(0,1,0), Z=(0,0,1). In file terms: proxy up (+Y) = long leg, proxy forward (file +Z) = short leg.
- **Degenerate shapes.** Isosceles or degenerate triangles make the angle sort ambiguous, as in A3OB. The validator should warn.

### 13.5 Export rebuild

The exporter accepts both proxy forms in one LOD:

- **packed:** a packed prim with a non-empty proxy path (outer, else embedded);
- **inline:** a polygon with a non-empty `a3ob_proxy_path` prim attribute, e.g. an unpacked packed proxy.

A packed prim that has no proxy path anywhere is an error ("packed prim is neither a LOD nor a proxy").

1. For each packed proxy prim, in prim order:
   - `F = fullTransform()` of the prim **after** the LOD was unpacked. Unpack already composed the LOD's own transform into it (V4), so it is not applied again (§3.2).
   - `world = local_P * F`, and `N_world = N_local * rotation part` (inverse-transpose for non-orthogonal F).
   - Convert to file space (§4.3).
   - **Mirror.** If `det(F 3×3) < 0`, reverse the triangle's corner order as for LOD polygons (§3.2).
   - **Snap guard:** compare the converted values with `a3ob_proxy_src_P` (by embedded point = ascending source index) and `a3ob_proxy_src_N` (by file corner, through the §4.3 map). If every component is within 1e-5, write the stored source values exactly. This makes an untouched proxy L0 despite f32 local-space rounding. A proxy moved further than that takes its new transform.
   - The prim's **pack point** (§5) is never read.
2. If the embedded geometry is not exactly 3 points + 1 triangle (e.g. a user-made proxy with display geometry), write the canonical triangle `C=(0,0,0)`, `B=(0,2,0)`, `A=(0,0,−1)` transformed by `fullTransform()`. File corner order is `C, B, A`, the same as A3OB's `create_proxy`; the Houdini order is the §4.3 map of that. Flags 0, UV 0, empty texture.
3. Append the 3 points, in embedded point order, after all mesh points. Append the face, whose corners reference those points in the embedded polygon's order (through the §4.3 map), after all mesh faces.
4. Add the selection `"proxy:" + path + "." + "%03d" % index` with byte 1 on the 3 vertices and the face.
5. Merge the embedded groups and weights into the LOD's selections.

**Inline proxies** stay where they are in point and prim order and are exported as ordinary triangles. Their
selection `"proxy:" + path + "." + "%03d" % index` is the union of two things:

- the point group of that encoded name, if it exists (the importer's inline form keeps it, §13.6);
- the points and face of every polygon whose `a3ob_proxy_path` / `a3ob_proxy_index` format to that name.

Where the group (with its weights and prim group, §7.2) covers an element, its bytes win, so an imported inline proxy is
L0. Elements reached only through the prim attributes get byte 1. The validator warns if an inline proxy polygon is not
a triangle.

### 13.6 When the importer makes a packed proxy (otherwise: inline fallback, still L0)

All of the following must hold:

- The name matches `^proxy:(.*)\.(\d+)$` **and** `"%03d" % int(digits) == digits`, so the name re-formats identically.
- The members are exactly 3 vertices and 1 face: a triangle on exactly those vertices, all bytes = 1.
- No other face uses those vertices.

Otherwise the triangle stays inline. Its selection stays a normal point group. If the name passes the first test, prim attributes `a3ob_proxy_path` / `a3ob_proxy_index` are set on the selection's member faces. That is the same form an unpacked packed proxy has, so the exporter treats both the same way (§13.5).

Lossless order requires the proxy vertices and faces to be the **tail** of the vertex and face lists in prim order. That holds in the fixture (§6). If proxies are interleaved, the round trip is L1 (OQ-2).

## 14. Reserved and meaningful Houdini names

| Name | Houdini meaning | Our use |
|---|---|---|
| `P` | point position | ✔ position (§4) |
| `N` | normal; with `transform_as_normal` it is rotated by transforms (docs: `hom/hou/Geometry` addAttrib) | ✔ vertex corner normal |
| `uv`, `uv2`… | texture coordinates (qualifier "Texture Coord") | ✔ UV sets, float64 |
| `mass` | point mass for Vellum/RBD (docs: vellum grain "Physical Attributes") | ✘ avoided → `a3ob_mass` |
| `name` | piece name. The Pack SOP makes **packed fragments** per `name` value only when "Pack by Name" (`packbyname`) is on. The default is off, and then Pack makes a single PackedGeometry prim (docs: `model/packed`; V6). | ✔ only as the outer label on LOD prims in the P3D geometry; never written inside a LOD geometry. A user's `name` inside a LOD is ignored by the exporter. If such a LOD is packed with "Pack by Name", its fragments share one `geometryid` and are collected as one LOD (§3.2, L1). |
| `path` | Alembic/USD prim path | ✘ unused |
| `Cd`, `Alpha` | colour | ✘ unused; free for viewport preview |
| `material`, `shop_materialpath` | Houdini material assignment; `material` is special-cased on packed prims (docs: `model/packed`) | ✘ not P3D data |
| `orient`, `pscale`, `transform`, `scale` | instancing on points | ✘ unused; only the derived proxy-instancing view may use them |
| `varmap` | local-variable map | ✘ never created (`create_local_variable=False`) |
| `xn__*` | `encodeAttrib` prefix (docs: `hom/hou/text`) | group names that start with `xn__` decode on export |
| `__*` groups | conventional temporary groups | excluded from export by default (§7.2) |
| `a3ob_*` | ours | the reserved prefix. Selection-derived names always carry `a3ob_w_` / `a3ob_fw_`, so they cannot collide with fixed names. |

## 15. Round-trip invariant table

| P3D field | Houdini representation | Back to P3D | A3OB today | Port |
|---|---|---|---|---|
| MLOD sig + version 257 | constant | constant | ✔ | L0 |
| LOD count | number of LOD prims (+copies) | count | ✔ | L0 |
| LOD order | P3D-geometry prim order | prim order | sorted | L0 (sort optional) |
| `P3DM`, 28.256 | constant | constant | ✔ | L0 |
| LOD header flags | `a3ob_lod_flags` | raw | lost (0) | L0 |
| resolution signature (f32) | `a3ob_lod_signature` (raw bits) + decoded `a3ob_lod_type` / `a3ob_lod_resolution` (M1 rules) | stored bits while the view matches; else M1 `encode_signature` | rounded | L0 for every f32 value (S6) |
| `#EndOfFile#` active byte | `a3ob_eof_active` (only if ≠ 1) | raw | always 1 | L0 |
| bytes after the last LOD (`P3DModel.trailing`) | P3D-geometry detail `a3ob_file_trailing` (base64) | only with a single input SOP | lost | L0 for an untouched container; else L1 (dropped with a warning) |
| vertex count | points − pack points (used only by packed prims) + 3·packed proxies | count | ✔ | L0 |
| vertex xyz | `P = (x, y, −z)` | inverse | ✔ | L0 |
| vertex flags | `a3ob_vertex_flags` raw | `& 0xFFFFFFFF` | lossy groups | L0 |
| normals table | vertex `N`, rebuilt first-appearance | rebuild | renormalised; visual only | L0 if unique, referenced and first-appearance (fixtures ✔); else L1 |
| proxy normal equal to a mesh normal, stored as a separate entry | shares one index after exact-f32 dedup | rebuild | — | L1 (fixtures: all values unique, S2) |
| normal values | `(−nx, −ny, nz)` f32 | inverse | renormalised | L0 |
| face sides 3–4, corner order | polygon, §4.3 map | inverse | ✔ | L0 |
| faces with 0–2 sides | closed polygon with 0–2 vertices | written with a warning | — | L1 |
| triangle pad | not stored | zeros | zeros | L1 if the source pad ≠ 0 (fixtures: 0) |
| face-data UV | `uv` or `a3ob_face_uv` (f64) | `1 − v` → f32 | ✔ | L0 for `v = 0` or `abs(v) ≥ 2⁻³⁰` |
| face flags | `a3ob_face_flags` | raw | lossy groups | L0 |
| texture, rvmat | `a3ob_texture`, `a3ob_material` (D-STR: M1 string unchanged) | `encode_string`; a character it cannot encode is a validator error | ✔ ASCII only | L0 for every byte 0x01–0xFF (V9) |
| face order / sections | prim order | prim order | slot-merged option | L0 |
| `#SharpEdges#` | edge group `a3ob_sharp` (+`a3ob_sharp_extra`) | ordered pairs | ✔ (degenerate dropped; empty always written) | L0 |
| `#Property#` | `a3ob_prop_names` / `a3ob_prop_values` (+ `a3ob_prop_raw` when the payload has bytes after a NUL) | raw payload while key/value are unchanged; else canonical | ✔ (tail bytes lost) | L0 |
| `#Mass#` | `a3ob_mass` | per vertex | Geometry LOD only | L0 |
| `#UVSet#` id k | `uv` / `uv<k+1>` f64 | id from name | ids renumbered | L0 |
| repeated `#UVSet#` id | first → attribute; later → blob | blob | — | L0 while counts are unchanged; dropped otherwise |
| selection vertex bytes | point group + `a3ob_w_G` (default 1.0) | encode, clamp 1..255 | byte 255 lost | L0 |
| selection face bytes | derived, or prim group (+`a3ob_fw_G`, default 1.0) | derived / explicit | dropped, then derived | L0 |
| selection names | `encodeAttrib` of the M1 name | `decodeAttrib`, then `encode_string` | ✔ (`@proxy_N` placeholders) | L0 |
| selection named `__…` | point group `__…` (unchanged by `encodeAttrib`) | exported because the name is in `a3ob_tagg_order` | ✔ | L0 |
| empty or exact-duplicate selection name | blob, with a warning | blob | — | L0 while counts are unchanged; dropped otherwise |
| selection names equal up to case | two groups | two selections, with a warning | ✔ | L0 |
| proxy selection + triangle | PackedGeometry prim + path/index (outer and embedded) + snap guard; or an inline triangle with path/index | rebuild + append, or in place | ✔ (UV/flags blanked) | L0 if untouched and tail-ordered; else L1. An unpacked proxy stays a proxy (inline form) |
| unknown TAGGs (`RawTagg`) | base64 blobs | raw | lost | L0 while counts are unchanged; dropped otherwise |
| inactive (active = 0) TAGGs | base64 blobs, raw active byte | raw | lost | L0 while counts are unchanged; dropped otherwise |
| TAGG active bytes other than 0/1 | `a3ob_tagg_active` (live TAGGs), `a3ob_tagg_unknown_active` (blobs) | raw | written as 1 | L0 |
| TAGG order | `a3ob_tagg_order` | hint + canonical | canonical | L0 |
| LOD copies | `a3ob_lod_copy_*` | extra LODs | ✔ | n/a (authoring only) |

## 16. Import and export outline (for M3/M4)

### Import, per LOD

1. **Read with `a3ob_core`** (M1): no renormalising, no rounding. M1 strings go to `hou` unchanged (D-STR, §5).
2. **Points and loose points.**
3. **Polygons**, with the §4.3 corner map (0–2-sided faces included, §5).
4. **Prim strings and flags.**
5. **`N` and UV attributes** (float64 via the verb, U7).
6. **Groups and weights** (`a3ob_w_*` / `a3ob_fw_*` created with default 1.0); blobs for empty or duplicate names and repeated UVSet ids (§7.3, §8).
7. **Sharp edges.**
8. **Detail attributes**, including `a3ob_lod_signature`, `a3ob_source_counts`, `a3ob_tagg_order` and, when needed, `a3ob_tagg_active`, `a3ob_eof_active` and `a3ob_prop_raw`.
9. **Proxies last**, as packed prims in ascending proxy face index, with path/index both outside and inside (§13.3).
10. **Pack** each LOD and merge into the P3D geometry, setting `name`. Store `a3ob_file_trailing` if the file has trailing bytes.

### Export

1. **Collect** LODs (§3.2). For each LOD prim, read the determinant of `fullTransform()` **before** unpacking, then unpack it with the `unpack` verb. The verb bakes `P`, `N` and nested proxy transforms (V3, V4); **nothing applies the LOD transform again**. Unpack never reverses winding, so for `det < 0` the face builder reverses polygon corner order (and warns).
2. **Validate** (§17). Errors stop the export.
3. **Per LOD, build `a3ob_core` objects in file order:**
   - vertices: points minus pack points (§5), then the packed proxies' triangle points;
   - faces: polygons, inline proxies included, then the packed proxies' faces (§13.5);
   - normals table;
   - TAGGs, by hint plus canonical order, with active bytes (§11.3);
   - strings encoded back with `encode_string` (D-STR);
   - signature by the §3.4 rule.
4. **Expand copies**, then check for duplicate signatures.
5. **Write.** Append `a3ob_file_trailing` if it applies (§3.3).

Export options that change data (lowercase, relative paths, sort sections, sort LODs, auto-components `export_p3d.py:294-303`, renumber components, Czech translation `export_p3d.py:591-593`) all default to off. They are applied in `a3ob_core` after collection.

## 17. Validator and artist tooling (M3/M4 plan)

### 17.1 Validator checks that this spec requires

| Check | Severity | § |
|---|---|---|
| Hou string with a character `encode_string` cannot encode (texture, rvmat, property, selection, proxy path) | error | §5 |
| `encode_signature` raises for an edited or new LOD type/resolution | error | §3.4 |
| Packed LOD prims mixed with loose polygons or points; packed prim that is neither LOD nor proxy; prim-level `a3ob_lod_type` | error | §3.2, §13.5 |
| Polygon with > 4 vertices, open polygon, non-polygon prim | error | §5 |
| Duplicate signatures after copies are expanded | error (option: skip) | §3.2 |
| LOD prim transform not identity, and the mirror flip when det < 0 | warning | §3.2 |
| Pack-by-name fragments collected as one LOD (order may change) | warning | §3.2 |
| Point shared between a proxy packed prim and polygons | warning | §5 |
| Polygon with 0–2 vertices; inline proxy polygon that is not a triangle | warning | §5, §13.5 |
| Outer and embedded proxy path/index disagree | warning | §13.3 |
| Selection names equal up to case | warning | §7.3 |
| **LOD looks merged.** `a3ob_source_counts` exists and the current point and face counts are both higher than it. The message names the counts and explains that a Merge of unpacked LODs keeps only the first input's detail (T7c; V8: two 3-point LODs merged give the first LOD's detail, with counts 6/2 against source 3/1). | warning | §3.1 |
| Stale blobs / sharp extras dropped because counts changed | warning | §9.2, §11.2 |
| Flag values not on the LOD type's known list; `a3ob_mass` on a non-Geometry LOD; degenerate proxy triangle | warning | §5, §10, §13.4 |

The "looks merged" check is a heuristic: legitimate modelling also adds points. It is a warning only (see OQ-13).

### 17.2 Artist tooling (deferred HDAs, D3)

HDAs are deferred (D3), so M3/M4 ship Python tools that need no `.hda`:

- **"A3OB LOD" shelf tool / Python SOP preset.** It creates or edits the LOD detail attributes from a **menu** of LOD
  types (display names from M1 `lod_types.LOD_TYPES`) plus a resolution field. It validates with `encode_signature`
  and shows the resulting signature and LOD name. Nobody has to type a bare enum integer.
- **Selection browser.** A shelf tool or Python panel that lists point groups by **decoded** name (`-proxies`, not
  `xn__proxies_cka`). It can create or rename a selection by its P3D name, and makes the encoded group.
- **Proxy tool.** It creates a packed proxy from a path and index, using the canonical triangle and both attribute
  copies (§13.3).
- **Unpack LOD k / Pack LODs helpers** (OQ-7), which unpack one LOD at a time so that detail attributes stay detail
  (§3.1).

## Open questions for the owner

| # | Question | Recommendation |
|---|---|---|
| **OQ-1** | Which axis map: `(x, y, −z)` (model faces +Z in Houdini; equal to A3OB-Blender + the standard Blender→Houdini conversion) or `(−x, y, z)` (keeps file Z; model faces −Z)? | `(x, y, −z)` |
| **OQ-2** | How should proxies be represented: PackedGeometry prims (Houdini-native, needs the snap guard and a tail-order assumption) or inline triangles tagged with prim attributes (trivially lossless, less ergonomic)? | Packed, with inline as an automatic fallback |
| **OQ-3** | What is the default LOD order on export: keep collected order (L0) or sort by signature like A3OB? | Keep, with a validator warning when unsorted |
| **OQ-4** | Should mass be stored as `a3ob_mass` (safe) or Houdini `mass` (works with Houdini sim/mass tools, but sims can create it by accident)? | `a3ob_mass` + a copy helper |
| **OQ-5** | **Code page of Arma P3D strings (open, owner).** Current state, not a decision: M1 uses cp1252 with C1 code points for the 5 undefined bytes (the owner said keep cp1252 for now), and §5 passes M1 strings to `hou` unchanged because they are valid Unicode (V9). Open: whether real files use CP1250 (Czech) or another code page. CP1250 also has 5 undefined bytes and would need the same C1 handling; switching would change only `binary.py`. To be settled with Bohemia's Arma 3 Samples MLOD files (D6). | Keep cp1252 until the Samples files show otherwise. |
| **OQ-6** | When should face selections become explicit prim groups: only when they differ from the derived rule, or always (simpler mental model, but stale after point-group edits)? | Only when they differ, with an import option for always |
| **OQ-7** | Should the importer output only the P3D geometry (packed LODs), or also offer "unpack LOD k" / per-LOD streams? That is an M3 UX choice, outside the data model. | Container, plus an unpack helper/option |
| **OQ-8** | Is the float64 UV requirement acceptable? Any SOP that rebuilds `uv` as float32 (UV tools) reintroduces about 6e-8 error, which counts as an edit. | Yes |
| **OQ-9** | Which point groups export as selections: all except `__*`? Or require an explicit marker? | All except `__*`, with a mask parameter |
| **OQ-10** | For a LOD packed prim with a non-identity transform: accept it (Unpack bakes it once; the exporter only fixes winding for det < 0, §3.2) or reject? | Accept and warn |
| **OQ-11** | The fixtures contain no unknown/inactive TAGGs, interleaved proxies, non-edge sharp pairs or duplicate normal values. Can the owner supply a real MLOD with `#Animation#`/`#Lock#`/`#Hidden#` from Object Builder to test the blob path? | Obtain one; otherwise M3/M4 use synthetic tests |
| **OQ-12** | Where review fixes were suggested as "L1 fallback", revision 2 chose blobs instead: empty selection names, exact-duplicate selection names and repeated `#UVSet#` ids are kept as raw blobs (L0 while counts are unchanged) rather than renamed (L1). The cost is that they are invisible as groups or attributes. Is that acceptable, or should they be imported as renamed, editable groups/attributes? | Blobs: these cases are corrupt or near-corrupt files, and losing them on edit (with a warning) beats inventing names |
| **OQ-13** | The "LOD looks merged" validator check (§17.1) is a count heuristic and fires on legitimate modelling. Is a stronger marker wanted, e.g. a per-prim `a3ob_lod_uid` int written at import, so that a Merge is detected exactly (two uids in one LOD)? It costs one prim attribute per LOD. | Start with the heuristic; add the uid if artists hit silent merges |

## Evidence

All scripts and outputs are in `docs/design/evidence/m2/` (copied from the session scratchpad).

### Fixture surveys

Run with system Python 3.12: `python -I <script> tests/fixtures/p3d/sample_2_crate.p3d tests/fixtures/p3d/sample_1_character.p3d`. They are independent minimal parsers; A3OB code was not executed.

| Id | Script → output | Shows |
|---|---|---|
| S1 | `survey_p3d.py` → `survey_out.txt` | Per LOD: signature, header flags (all 0), counts, sides, flag histograms, TAGG names, properties, selection byte histograms (byte 255 present), face-byte mismatches (376 in char LOD 0), names that are illegal identifiers, empty `#SharpEdges#` everywhere |
| S2 | `survey2_p3d.py` → `survey2_out.txt` | TAGG order; no inactive TAGGs; triangle pads zero; normals table unique and equal to first-appearance rebuild; normal lengths; `dot(cross, n) > 0` for all faces; UVSet 0 == face UV; face-mismatch detail; proxy indices at the tail; memory points `wristleft` / `wristright` |
| S3 | `survey3_p3d.py` → `survey3_out.txt` | Sharp pairs are all real, directed edges; mass length = 4·nv; proxy vertices are in other selections and share no faces |
| S4 | `survey4.py` → `survey4_out.txt` | LODs without `#UVSet#` have zero face UVs; weight bytes 1..255 survive the f32 round trip |
| S5 | `facing.py` → `facing_out.txt` | Character foot/leg extents: toes at file −Z |
| S6 | `sigcheck.py` → `sigcheck_out.txt` (uses M1; run `python -I sigcheck.py <fixtures>`) | §3.4 rule: all 16 fixture LODs, both near-miss signatures (`f32(1e13·(1+3e−7))` → Geometry, `f32(1e15·(1−5e−7))` → Memory; `encode_signature(UNKNOWN, sig)` raises for both) and 200 000 random f32 bit patterns round-trip bit for bit; 9 of those patterns are not reproduced by re-encoding the decoded view; edits take the encode path |

### Houdini checks

Run headless with `"E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" <script>`; the outputs print Houdini 22.0.466 and Python 3.13.10.

| Id | Script → output | Shows |
|---|---|---|
| T1 | `hcheck_datamodel.py` → `hcheck_out.txt` | `prim.normal()` = (0,0,−1) for an order with algebraic cross +Z |
| T2 | `hcheck_datamodel.py` | Illegal names fail `addAttrib` / `createPointGroup`; `encodeAttrib` round-trips; a 276-character encoded group name works |
| T3 | `hcheck_datamodel.py` | A point group, a prim group and a point attribute of the same name coexist |
| T4 | `hcheck_datamodel.py` | Detail dict, string[] and int[] attributes work; HOM float detail is f32; u32 values above 2³¹ are stored as negative int32 |
| T4c | `hcheck_datamodel.py` | UV flip error: f32 33 388/100 000 mismatches; f64 0 |
| T6 | `hcheck_datamodel.py` | Edge group survives bgeo, Transform, Fuse and PolyExtrude |
| T7 | `hcheck_datamodel.py` | `createPacked("PackedGeometry")`, `setEmbeddedGeometry`, `setTransform`/`fullTransform`; the embedded detail and groups survive; Unpack keeps the detail attributes and applies the transform |
| T7b–e | `hcheck_datamodel.py` | Pack SOP: identity transform and embedded detail kept; Merge of unpacked LODs loses the second detail value; Merge of packed LODs keeps both; Unpack of one LOD keeps its detail |
| T8 | `hcheck_datamodel.py` | Row-vector matrices |
| T9 | `hcheck_datamodel.py` | `encodeAttrib` samples |
| U2 | `hcheck2.py` → `hcheck2_out.txt` | HOM `addAttrib` floats are Float32; a 64-bit attribute (from Attribute Create) stays Float64 through Python SOP writes, Transform, `merge` and bgeo |
| U3 | `hcheck2.py` | The Normal SOP agrees with T1; its parameter list has no hard-edge group |
| U4 | `hcheck2.py` | Edge group order and direction are kept after reload; `findEdge` returns None for a non-edge |
| U5 | `hcheck2.py` | Unpack defaults; `N` with `transform_as_normal` has qualifier "Normal"; `uv` with `setOption("type", "texturecoord")` has qualifier "Texture Coord" |
| U6 | `hcheck2.py` | Proxy matrix sanity check |
| U7 | `hcheck2.py` | The `attribcreate::2.0` verb with `precision#: 3` makes a Float64 vertex `uv` on a standalone `hou.Geometry` and stores exact doubles |
| U8 | `hcheck2.py` | Unpack `detail_attributes` menu labels |
| V1 | `hcheck3.py` → `hcheck3_out.txt` | (Against the earlier M1 codec; superseded by V9.) M1's decoding of bytes 0x01–0xFF has 5 lone surrogates; the D-STR conversion has max code point U+00FF, is bijective back to the bytes, and round-trips through a prim string, a detail string[] and `encodeAttrib` → `createPointGroup` → `decodeAttrib` for every byte; `encodeAttrib` of a lone surrogate raises `TypeError` |
| V2 | `hcheck3.py` | `encodeAttrib("")` = `""` and `createPointGroup("")` raises `OperationFailed`; `__foo`, `Head`, `head` pass unchanged; groups `Head` and `head` coexist |
| V3 | `hcheck3.py` | The `unpack` verb bakes a packed translate into `P`; with a mirror-X transform, `prim.normal()` flips to +Z while `N` stays (0,0,−1): Unpack does not reverse winding |
| V4 | `hcheck3.py` | Unpacking a LOD (default parms) keeps a nested proxy packed, with `fullTransform()` = proxy·LOD exactly, its outer prim attribute and the LOD detail; unpacking the proxy too yields its embedded `a3ob_proxy_path` / `a3ob_proxy_index` prim values |
| V5 | `hcheck3.py` | `fuse::2.0` (defaults) merges a proxy pack point at the origin with mesh point 0 (4 → 3 points); the shared point is used by both the packed prim and the polygon, and no point is pack-only |
| V6 | `hcheck3.py` | Pack verb `packbyname` default 0 → one PackedGeometry; with 1 → PackedFragment per `name`, both with the same `geometryid`, each unpacking to its piece with the full LOD detail |
| V7 | `hcheck3.py` | A point attribute created with default 1.0 gives new points 1.0; Subdivide interpolates weights |
| V8 | `hcheck3.py` | Merging two unpacked LODs keeps the first's `a3ob_lod_type` and `a3ob_source_counts` (3, 1) while counts become (6, 2) |
| V9 | `hcheck4_codec_direct.py` → `hcheck4_codec_direct_out.txt` | M1's final `decode_string` of every byte 0x01–0xFF holds no surrogates and round-trips unchanged through a prim string, a detail string[] and `encodeAttrib` → `createPointGroup` → `decodeAttrib`, and back to the same bytes through `encode_string`; 0x80 shows as `€` |
| R1 | `hcheck3_surrogate_crash.py` → `hcheck3_surrogate_crash_out.txt` (review evidence `rv_sur.py`, reproduced) | `setAttribValue` with an M1 string containing U+DC81 kills hython with "Fatal error: Segmentation fault" (exit 139) |
| R2 | review evidence `rv_check2.py` case F (not copied; same result as V5) | Fuse merges a packed proxy's point with a mesh point |

### Docs pages read

`unreal/coordinates`, `hom/hou/PackedPrim`, `hom/hou/Geometry` (Attributes), `hom/hou/Attrib`, `hom/hou/text`, `model/packed`, `solaris/udim`, `nodes/sop/vellumconstraints_grain`, `nodes/sop/pack`, `nodes/sop/unpack` (revision 2).

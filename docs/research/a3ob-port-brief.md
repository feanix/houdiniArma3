# A3OB → Houdini port: research brief

Written 2026-11-03 by the advisor, from a read of A3OB v2.5.1 (`MrClock8163/Arma3ObjectBuilder` @ 9f2b8bc, 2025-04-23,
still the latest release) and a survey of Houdini MCP servers. File references are relative to the add-on folder
`Arma3ObjectBuilder/`. User docs: https://mrcmodding.gitbook.io/arma-3-object-builder (index at `/llms.txt`).

## Environment

- **Target: Houdini Indie 22.0.466 (Steam)**, `E:\SteamLibrary\steamapps\common\Houdini Indie`.
  - Default Python is 3.13 (3.11 libs also ship). Headless `hython` works.
  - Prefs are in `~/Documents/houdini22.0`; the only package is SideFXLabs22.0.
  - H21.0.596 is also installed, but it isn't the target.
- **Arma 3 Tools** is installed at `E:\SteamLibrary\steamapps\common\Arma 3 Tools`. Object Builder can serve as the reference reader for exported P3Ds.
- **System Python** is 3.12.10; uv is not installed.

## License

- **A3OB is GPL-3.0-or-later.** Credit "MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox)" (`__init__.py:4`).
- **Derivative work:** translating its code, data tables (LOD enum, flag bits, known named properties, Czech↔English names, OFP2_ManSkeleton) or rvmat templates makes the port GPL-3.0-or-later. Ship the LICENSE, keep attribution and mark changes.
- **Clean room:** a rewrite from the format specs alone is the owner's legal call.
- **Exported files** are not covered by the license.

## What A3OB is

- About 18k lines of Python in four layers: `io/` (formats), `props/` (data stored on Blender objects), `ui/` (panels, operators, tools) and `utilities/` (algorithms, tables, validator).

### Formats (io/)

| Format | Read | Write | Notes |
|---|---|---|---|
| P3D MLOD v257 (LOD `P3DM` 28.256) | ✔ | ✔ | The core format. No ODOL (binarised) or SP3X. |
| RTM_0101 (+ MDAT frame events) | ✔ | ✔ | Export needs a skeleton (bone list from model.cfg). |
| BMTR v3–5 (binarised RTM) | ✔ | ✘ | LZO, parent-relative; needs numpy. |
| PAA | ✔ | ✘ | DXT1/DXT5 only, mip 0, slow per-pixel Python. |
| model.cfg | ✔ | ✔ (CfgSkeletons only) | Text parser rejects `#include`/`#define`, then falls back to `CfgConvert.exe` + its own derapifier. Only the skeleton bone lists are used. |
| ESRI .asc, Terrain Builder object list | ✔ | ✔ | Terrain side features. |

- **About 60–65% of `io/` has no bpy dependency:** `binary_handler`, `compression` (LZO, DXT), `data_*.py`, `config/*` and most of the mcfg modules.
  - `io/__init__.py` imports all the Blender modules, so the pure modules must be lifted out rather than imported.
  - `import_*`/`export_*` are Blender glue: bmesh plus bpy.ops separate/join/apply.
- **Tests run only inside Blender.** They are smoke tests plus a round trip that only compares file sizes. Fixtures: `tests/inputs/sample_1_character.p3d` (9 LODs, 11 proxies, mass, UV sets), `sample_2_crate.p3d` (7 LODs incl. Roadway) and one model.cfg.
- **Port plan for tests:** test the data layer in plain CPython with parse → write byte-identical round trips.

### P3D data model and how A3OB maps it to Blender

- **One mesh object per LOD.**
  - LOD type: 33 types plus Unknown; the enum is duplicated in `io/data_p3d.py:258` and `utilities/data.py:67`.
  - Resolution: only visual, view-cargo, shadow and edit LODs have one.
  - LOD signature: a float, e.g. Visual = res, Shadow = 10000+res, Geometry = 1e13.
- **Export collects** every visible top-level mesh with `is_a3_lod`; one scene = one P3D. Mesh children are merged in as sub-objects or proxies. Origin = P3D origin.
- **Coordinates:** Y↔Z swap with no winding change. Normals are swapped and negated, and stored as custom normals on visual LODs only. UV V is flipped.
- **Named selections** become weighted vertex groups.
  - Weight byte: 0 = not a member, 1 = 1.0, otherwise (255−b)/254.
  - Face selection bytes are dropped on read. On write a face is a member if all its vertices are.
  - Conventions: `ComponentNN`, `OccluderNN`, `inNN`, bone names, `proxy:<path>.NNN`.
- **Proxies:** one-triangle child objects with path and index (0–999).
  - Frame from the triangle: right-angle vertex = origin, short leg = +Y, long leg = +Z (`utilities/proxy.py:9-29`).
  - The `@proxy_N` placeholder only exists because of Blender's 63-character vertex group name limit; Houdini doesn't need it.
- **Per-face texture + rvmat** become Blender materials carrying an A3OB property group.
  - Texture types: TEX path / COLOR procedural `#(argb,8,8,3)color(r,g,b,a,TYPE)` / CUSTOM.
  - Face order follows material slots and controls alpha draw order ("sort sections").
- **TAGGs handled:** `#SharpEdges#` (edge pairs), `#Property#` (named props, 63+63 chars), `#Mass#` (f32 per vertex, Geometry LOD only), `#UVSet#` (per face corner), and selections.
  - Unknown TAGGs (`#Animation#`, `#Lock#`, ...) and the LOD header flags are lost.
- **Flags:** a u32 per vertex and per face. A3OB decodes them into "flag groups", which is lossy. The port should store the raw u32.
  - Vertex default 0x02000000 (fixed normals); face default 0.
- **Paths:** relative to the project root (default `P:\`), backslashes, lower-cased on export by default.

### User-facing features (props/, ui/, utilities/)

- **Core:**
  - P3D import/export with all of the per-LOD data above.
  - LOD copies: one mesh written under several signatures.
  - Export pre-processing: section sort, auto `ComponentNN`, duplicate-signature check, UV and normal cleanup per LOD type.
  - Validation: per-LOD-type rule sets (`utilities/validator.py`), mostly topology checks.
  - Named-property, proxy and material presets from `common_data` plus a custom JSON.
  - Project-root path handling.
  - Basic mass tools: set, uniform, from density via signed volume, center of mass.
- **Secondary:**
  - Hit-point cloud: inside-test on a grid → in Houdini, VDB + Points from Volume.
  - Voronoi-weighted mass: O(n²) bisects → Voronoi Fracture.
  - RTM, Action frame lists/events, skeleton manager, armature from Memory-LOD pivots, weight cleanup (max 4 bones, normalize, prune) → KineFX.
  - RVMAT templates, sRGB↔linear converter, path/selection renaming, ASC, Terrain Builder lists, PAA, batch scripts, proxy extract.
- **Skip (Blender-specific or native in Houdini):**
  - Outliner and Proxy Access lists, depsgraph handlers.
  - Edit-mode assign/select operators.
  - Convex hull, normals and face-order wrappers.
  - ATBX conversion, icons, compatibility shims.

### Bugs not to carry over

- `binary_handler.read_asciiz` loops forever at EOF: it compares to `""` instead of `b""`.
- `skeletonInherit` never resolves, so inherited bones are silently dropped (`io/import_mcfg.py:115`).
- An empty `#SharpEdges#` is always written (`io/export_p3d.py:493`).
- The config tokenizer doesn't handle hex literals. The derapifier crashes on root-level class inheritance and reads longs as unsigned.
- PAA swizzle handling looks wrong (`io/data_paa.py:95-116`).
- Resolutions are rounded to int, including Unknown LODs. Signatures are matched as `%.3e` strings.
- Flags: `remove_group_face` edits the vertex layer (`utilities/flags.py:161`). Flag decoding is lossy.
- `lod_copy_remove` clamps against the wrong list (`ui/props_object_mesh.py:250`).
- The Paths validator "position" check (`utilities/validator.py:579`).
- `normalize_weights` has no zero-sum guard.

## Blender concepts that need a Houdini design decision

1. **What a LOD and a P3D are.**
   - Suggestion: one SOP stream per LOD, with detail attributes for LOD type, resolution and named properties.
   - A container (e.g. a "P3D LOD" HDA per LOD, merged into a "P3D ROP/output" HDA) replaces "scan the scene".
2. **Weighted selections.** Houdini groups are unweighted: use group + float attribute, or float attributes only.
3. **Proxies:** packed primitives (packed disk or intrinsic transform) carrying path and index, instead of triangle children.
4. **Materials/sections:** prim string attributes for texture and rvmat, plus ordering.
5. **Sharp edges and custom normals.** Houdini has no edge attributes: use vertex normals plus an edge group.
6. **Flags:** raw int point/prim attributes, with an optional decode UI.
7. **Mass:** point `mass` attribute.
8. **Sub-objects and modifiers:** usually unnecessary, since SOP networks are already procedural.
9. **Skeletons/RTM:** KineFX skeleton plus animation, with a skeleton definition stored as detail data or a JSON file.
10. **Packaging:** a Houdini package (json) with Python modules in `python3.Xlibs` (3.13 for H22), HDAs, a shelf, and possibly a Python panel. Format I/O stays in a pure-Python package testable outside Houdini.

## Risks / open questions

- **Indie licensing.** HDAs saved from Houdini Indie are limited-commercial (`.hdalc`), which affects anyone loading them in full Houdini. Decide how HDAs are built and distributed; a source-built or expanded-text HDA build step may avoid this. Not yet verified.
- **Houdini version floor:** H22 only, or also H21 / H20.5? This affects the Python version (3.13 vs 3.11) and APIs.
- **License stance:** GPL derivative (fastest: reuse the pure io code) or clean room.
- **model.cfg with preprocessor directives** needs `CfgConvert.exe` (Windows, Arma 3 Tools) unless we write a preprocessor.

## Houdini MCP server (track 2)

- **"houdini-mcp-server" is JTCHE/houdini-mcp** (PyPI `houdini-mcp-server`).
  - MIT, v0.8.1 (2026-10-23), claims H20.5/21/22 support. Young: 14★, one maintainer.
  - About 20 mode-based tools: `scene_overview`, `node_inspect`, `geometry_inspect` (points, prims, attribs, groups), `node_edit`, `parm_set`, `hda`, `batch` (undoable), `cook`, `capture` (viewport image returned inline), `execute` (Python/HScript/VEX), offline `docs`, and more.
  - Houdini side: package + shelf, TCP on 127.0.0.1 with an OS-chosen port, runs on the main thread. Claude talks to a stdio bridge, `houdinimcp-bridge`.
  - Installer: `houdinimcp-install --houdini-version 22.0 --harness claude-code --dry-run`. It can also auto-start headless hython.
  - Gotcha: Houdini launched from Git Bash may not load the package (HOME differs); set `HOUDINI_USER_PREF_DIR`. We launch from Steam, so this probably doesn't apply.
- **Fallback: healkeiser/fxhoudinimcp.**
  - 392★, v2.28.3, 215 tools, uses hwebserver on 127.0.0.1:8100. An H22 threading issue (#12) is closed.
  - Screenshots go to disk only.
- **Avoid:** capoomgit/houdini-mcp (stale, Windows bugs) and raw `hrpyc` (binds 0.0.0.0 with no auth).
- **Security:** both run Python in Houdini over localhost without auth. Don't auto-approve the `execute` tool. Be careful opening third-party .hip/.hda files while the agent can execute code (prompt injection).

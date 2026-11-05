# a3ob_houdini

The Houdini glue of houdiniArma3. It turns `a3ob_core` data into Houdini geometry and back, and holds the code of
every A3OB node. The format code stays in `a3ob_core`, which never imports `hou`.

## How a node is built (D13)

Every A3OB node is a Python-type SOP asset written as text. It is never saved from the Houdini GUI.

| Piece | Where | What it does |
|---|---|---|
| Parameter table | `nodes/<kind>.py`, `SPEC` | The only list of the node's parameters (`params.NodeSpec`). |
| Cook | `nodes/<kind>.py`, `cook(node, report)` | The work. Raises `UserError` for problems the artist can fix. |
| Entry functions | `entry.py` | `cook(kind, node)` and `button(kind, action, kwargs)`. Frozen once shipped. |
| HDA text files | `houdini/otls/a3ob_<kind>.hda/` | Generated from the table by `scripts/build_hdas.py`. Do not edit. |

The `PythonCook` of an asset is one line: `import a3ob_houdini.entry as e; e.cook('<kind>', hou.pwd())`. Nothing else
lives in the asset. `entry.cook` turns every exception into a node error (a `UserError` shows its own text; any other
exception shows a short "bug" text and the traceback goes to the Log Viewer). If the HDA route is ever dropped,
`params.add_spare_parms` builds the same parameters on a Python SOP that calls the same entry function.

A Python-type SOP starts with a copy of its first input, so a node that edits its input writes straight into
`node.geometry()` (Mass and Proxy do) and must not merge the input again. A node module imports `hou` only inside a
`try`, because the parameter tables are also read under plain Python.

**Do not edit a loaded A3OB asset in Houdini.** `hou.HDADefinition.setParmTemplateGroup`, or any edit in the GUI (Type
Properties, Apply, Save), rewrites the asset's HDA folder on disk and writes a copy to `houdini/otls/backup`. Both break
the checked-in text files. To change a parameter, change the table in `nodes/<kind>.py` and run
`python scripts/build_hdas.py`. `houdini/otls/backup` is in `.gitignore`; delete it if it appears.

To add a node: write `nodes/<kind>.py` (`nodes.KINDS` finds it by itself), run `python scripts/build_hdas.py`, and commit
the generated folder. A test fails when the checked-in files differ from the generated ones.

Every node also needs a user docs page (the `?` icon of the parameter pane opens the HDA's generated `Help` section; its last line names `docs/user/reference/<type name>.html`
section). The page is made from the table: run `python scripts/build_user_docs.py` after `build_hdas.py`, add a short
intro to `NODE_INTRO` in that script, and commit `docs/user/`. A test fails when the pages are out of date.

## Parameter kinds and fields (`params.Parm`)

`KINDS`: `string file dir color toggle button int float menu multiparm folder`. The fields below are what the node
parts use. `tests/test_node_generator.py` has a small SPEC with every one of them (read it for working examples).
`hda_source.py` writes them into the DialogScript; the HTML reference and the Help section show them.

```python
# Visibility: show_when / enable_when = ((parm name, (values...)), ...). All pairs must hold; a pair holds when the
# parm has any of its values (a menu token, or "1" / "0" for a toggle). Written as Houdini hidewhen / disablewhen;
# the docs say "Shown when Mode is Uniform total" / "Editable when Set Texture is on".
Parm("mass_total", "Total Mass (kg)", "float", 1.0, "...", minimum=0, show_when=(("mode", ("total",)),))
Parm("texture", "Texture File", "file", "", "...", enable_when=(("settex", ("1",)),))

# Folders: kind "folder", folder_type "simple" (group box), "collapsible" or "tabs" (one tab; neighbouring tabs form
# one row). The children are ordinary top-level parms: read_parms and the cooks look through folders and never see
# the folder. A folder takes no show_when / enable_when (put them on its children). Folder names must not clash
# with parm names. Houdini renames the 2nd and later tab of a row to <first tab name>_1, _2, ... when it loads the
# asset, so only the first tab name is kept as written; the generated text stays stable.
Parm("props_box", "Properties", "folder", folder_type="simple", children=(Parm("props", "Properties", "multiparm", ...),))
Parm("tab_checking", "Error Checking", "folder", folder_type="tabs", children=common.level_parms())

# File picker: kind "file" with file_pattern (as Houdini filechooser_pattern) and file_mode "read" or "write".
Parm("tex", "Texture File", "file", "", "...", file_pattern="*.paa *.tga *.png")
# Folder picker (Houdini directory chooser): kind "dir".
Parm("projectroot", "Project Root", "dir", "", "...")
# Colour with alpha: kind "color", four floats 0..1, default (r, g, b, a) (default white). read_parms gives a tuple.
Parm("color", "Color", "color", (1.0, 1.0, 1.0, 1.0), "...")

# Toggle menu on a string parm: a menu with menu_action (a function in the node module's MENUS, as for any dynamic
# menu) and menu_toggle=True. Picking an entry adds or removes its token in the string (Houdini menutoggle);
# read_parms gives the string as typed. The default may be any text.
Parm("lods", "LODs", "menu", "*", "...", menu_action="lod_choices", menu_toggle=True)

# Group picker: kind "group", a string that behaves like the Group field of Houdini's own SOPs. The dropdown lists the
# input's groups (toggle menu, hou.SopNode.generateInputGroupMenu, via entry.group_menu / group_menu.py) and the arrow
# button runs soputils.selectGroupParm (tags script_action, script_action_help, script_action_icon). group_type is
# "prims" or "points"; group_follow names a menu with those tokens (Group Type) that the picker obeys instead.
# read_parms gives the string as typed, so names, patterns and numbers (0-3) still work in the cook.
Parm("group", "Group", "group", "", "...", group_follow="grouptype")
Parm("group", "Group", "group", "", "...", group_type="points")

# Menu choices that need their own help: item_help=((token, text), ...). A menu whose help reads
# "Intro. Label A: text; Label B: text" is split by itself. A choice with no text is only listed by name.
```

`NodeSpec.on_created=True` writes an OnCreated section (Python, `ExtraFileOptions`) that calls the node module's
`created(node)` once when an artist makes the node, never when a scene loads (P3D Export uses it for D26: start from
the active preset). The HDA defaults stay as the table says.

`NodeSpec.viewer_state="<module>"` writes a ViewerStateModule section (a call to `<module>.create_template(state_name)`) and a DefaultState
section, so the node has a Python viewer state that starts with Enter in the viewport (Rig Landmarks). A float parm may have an `action` (a `BUTTONS` callback that
runs when the artist edits it).

`NodeSpec.on_loaded=True` does the same for the OnLoaded event (a scene with the node loads) and calls `loaded(node)`;
Mass and Material Assign use it to move an old hidden parameter into its new one. The section guards its import, so a
session without the package loads the node quietly. A toggle may have an `action` (a `BUTTONS` callback), as Mass
"Visualize" does. `node_visualizer.py` makes node Color visualizers (only in a session with the Houdini window).

`NodeSpec.input_help` is a tuple of one plain sentence per input (the Help `@inputs` section and the reference show it).
Text in the Help section goes through `docs_common.wiki_escape`: the wiki has no backslash escape, so words that would
read as markup (`* ^__*`) are put in a code span. Write plain text in the table; do not add wiki markup yourself.

## Testing: which HDAs the suite uses

The hou tests install the HDAs of the checkout under test from `houdini/otls` (`tests/hou_support.py`). A Houdini package
can load other copies of the same HDAs first (for example one that points `A3OB_HOUDINI` at another checkout). So after
each install the helper makes the checkout's definition the preferred one (`hou.HDADefinition.setIsPreferred`) and checks
that the node type resolves to a library inside this checkout. If not, the run stops at once and names the other library.
`tests/test_hda_preference.py` proves it with a second copy installed first. This makes the suite safe to start from both
Bash and PowerShell, with or without your package. To run fully clean anyway, set `HOUDINI_PACKAGE_SKIP=1` (no packages)
or point `HOUDINI_USER_PREF_DIR` at an empty folder before starting hython.

## Naming convention for nodes

| Thing | Rule | P3D Import |
|---|---|---|
| Kind | short snake_case, no `a3ob_` prefix | `p3d_import` |
| Houdini type name | `a3ob_` + kind | `a3ob_p3d_import` |
| Label | the name used in `docs/design/ux-tools.md` | `P3D Import` |
| Tab menu | submenu `A3OB` (from the table's `submenu`) | A3OB > P3D Import |
| HDA folder | `houdini/otls/<type name>.hda/` | `a3ob_p3d_import.hda/` |
| Section folder in the HDA | `Sop_1` + type name with each `_` doubled | `Sop_1a3ob__p3d__import/` |
| Parameter names | short, lowercase, no underscores where one word does | `file`, `lods`, `facegroups` |
| Attributes and groups | `a3ob_*` (spec section 14) | |

Type names carry no version suffix yet. A change that old scenes cannot take gets a new type name
`a3ob_<kind>::2.0`, as Houdini does it.

## Modules

| Module | Needs `hou` | Purpose |
|---|---|---|
| `entry` | at cook time | Frozen entry functions called by the assets: `cook`, `button`, `menu`. |
| `reload` | no | `reload_all`: the Reload A3OB shelf tool. Forgets the A3OB modules after a package update, no restart. |
| `params` | no | `Parm` (kinds below), `NodeSpec`, `read_parms`, `add_spare_parms`. |
| `nodes` | no | One module per node kind; `KINDS`, `load`, `all_specs`. |
| `hda_source` | no | Makes the HDA text files from a `NodeSpec`. |
| `messages` | no | `Report`, `message`, `warning`, `error`, text helpers (ux-tools.md section 2). |
| `errors` | no | `UserError`. |
| `names` | only the two group-name wrappers | D-STR helpers, `encode_group_name` / `decode_group_name`, `lod_uid`, int32 helpers. |
| `mass_math` | no | The arithmetic of the Mass node: closed components, volume, colour ramp, centre of mass. |
| `hull_math` | no | 3D convex hull in plain Python (`convex_hull`); the Components node's hull mode uses it (this Houdini build has no hull verb). |
| `geo_edit` | at call time | For nodes that edit their input: `copy_input` (output = copy of input 1), detail read, set and remove. |
| `lod_props` | no | LOD Properties decisions: type menu, signature update (through `a3ob_core`), property and copy merge. |
| `material_text` | no | Material Assign text rules: slash normalisation, procedural colour text, known endings. |
| `lod_container` | yes (lazily) | The two shapes of geometry (LOD geometry, P3D geometry): `classify`, `lod_content`, detail copy. Extract LOD and Assemble LODs use it. |
| `lod_match` | no | The Assemble LODs match rule (spec 3.2) as a pure function, `match_lods`. |
| `selection_browser` | yes | Selection browser without a dialog: `list_groups`, `create_selection`, `delete_selection`, `rename_selection`. Each inserts its node inline after the chosen node (D21; `_add_after`, `ROW_STEP`). |
| `selection_browser_dialog` | GUI only | The `hou.ui` layer called by `houdini/toolbar/a3ob_selection_browser.shelf`. |
| `p3d_import` | `lod_builder` and `import_model` | The importer: `plan` (decisions, no `hou`), `lod_builder` (writes geometry), `proxies`, `lod_filter` (numbers, ranges, names, `*` patterns), `lod_list` (LOD names of a file without reading it). |
| `layout_record` | no | The layout record `a3ob_layout` (spec 19, D23): `build_record` (import) packs the file layout Houdini cannot hold, `apply_record` (export, from `lod_build`) writes it back into the built LOD while the positions and topology still match its digest. Uses `permutation`. |
| `p3d_export` | `collect_lods` and `read_geo` | The exporter core: `collect`, `build`, `to_bytes`, `write_file`, `export_geometry`. The rest works on plain data (`data`) and runs under CPython. |
| `p3d_export/renames`, `p3d_export/czech_names` | no | The D17 options (spec 18): `renumber_components`, `translate_names` (table in `czech_names`) act on the ordered selection items of a LOD; `strip_uvs` drops the UVs of a built LOD (also of a LOD copy, by the copy's own type). Fields `renumber_components`, `czech_names`, `drop_uvs`; node parms `renumber`, `czech`, `dropuvs`. |
| `settings` | no | The user prefs JSON (`A3OB_SETTINGS` overrides its place, `A3OB_PROJECT_ROOT` the root), project root, export presets (`Lossless`, `Arma 3 release`, own ones) mapped onto `ExportOptions`, common data lists, the Arma 3 Samples folder (`samples_dir`, `effective_samples`, `samples_defaults`; D37 Q1). Never raises on a bad file. |
| `nodes/rig_skeleton` | yes | Rig Skeleton (task #61): `model.cfg` + pivots `.p3d` to a KineFX skeleton (points with `name` and `transform`, polylines parent to child). The logic is in `a3ob_core/rig` (`model_cfg`, `pivots`, `skeleton`; no `hou`). |
| `nodes/weights_convert`, `nodes/weight_cleanup`, `rig_weights` | yes | Weights Convert and Weight Cleanup (task #62): bone selections (point groups with `a3ob_w_*`) to KineFX capture weights (`boneCapture`) and back, and the Arma clean-up (at most 4 bones, k/254 grid, sum exactly 1, `a3ob_rig_changed`). The maths is `a3ob_core/rig/weights.py` (numpy); `rig_weights` reads and writes the geometry. The capture layout is in `character-rigging.md` 4.2. |
| `nodes/weight_transfer` | yes | Weight Transfer (tasks #65, #73, D41): the weights of a source body onto the Body group of the user's mesh by closest point and inpainting (`character-rigging.md` 2.1 to 2.5, 6.2). Inputs: mesh, source body, skeleton. The maths is `a3ob_core/rig/transfer.py` (numpy only, `transfer_body`); `rig_weights` reads and writes the selections (`read_mesh`, `group_mask`, `write_point_int` are shared with Accessory Weights). Writes `a3ob_rig_origin`. |
| `nodes/accessory_weights` | yes | Accessory Weights (task #73, D41): weights accessories (Target Group) from weighted points (Source Group) of the same mesh, Rigid (Auto, Fixed blend or a picked Bone) or Non-rigid. Inputs: mesh, skeleton (a Rig Fit output gives its `fitted_skeleton`). The Bone menu (`MENUS["bones"]`) lists the joints of input 2. The maths is `transfer.accessory_layer`. Writes `a3ob_rig_origin` and `a3ob_rig_piece`. |
| `nodes/rig_fit` | yes | Rig Fit (task #66): fits the Arma skeleton and the sample body to the user's mesh from 14 landmarks (the detail vectors `a3ob_lm_*` that Rig Landmarks writes, or a guess). Inputs: mesh, skeleton, sample body. A Python-type SOP has one output (D40), so it holds the fitted skeleton, the fitted sample body and the landmarks as three packed prims named by the prim string `a3ob_rig_part`; `rig_weights.read_part` takes one out (Weight Transfer input 2 does it for `fitted_body`). The `show` menu only hides parts in the viewport. The maths is `a3ob_core/rig/landmarks.py` (guess, symmetry, snap) and `fit.py` (rigid fit down the hierarchy, linear blend skinning); numpy only. |
| `nodes/rig_landmarks`, `landmark_io`, `rig_landmark_state` | yes | Rig Landmarks (tasks #71 and #74, D42): 14 float3 fields (positions, all zero = not set) in the order of `a3ob_core.rig.LANDMARKS`, a Mirror toggle (Left is +X), Guess Landmarks / Guess This Landmark / Set From Selected Points buttons. Writes the input mesh plus one detail vector `a3ob_lm_*` per set landmark (`landmark_io.write_landmarks`, read by Rig Fit with `read_landmarks`). `landmark_io.sample_side` and `guess_for_mesh` are the one guess code that Rig Fit and Rig Landmarks share. `rig_landmark_state` is the Python viewer state (a dynamic `translate` handle and a text drawable per landmark); `NodeSpec.viewer_state` makes the asset's `ViewerStateModule` and `DefaultState` sections. |
| `paa_cache` | no | `cached_image(paa_path)` returns the path of a PNG decoded from a `.paa` (task #53, D29; decoder `a3ob_core.paa`, writer `a3ob_core.png`). Folder: the `paa_cache_dir` setting, else `$HOUDINI_TEMP_DIR/a3ob_paa_cache`. Raises `a3ob_core.paa.PaaError`. |
| `viewport_material` | `apply` only | Material Assign "Show in Viewport" (task #54, D29): `resolve` (texture line to image or colour, no `hou`), `apply` (one `principledshader::2.0` per texture in the matnet `a3ob_viewport_materials`, sets `shop_materialpath`). The export ignores it. `material_text.paa_texture` is the export's `.paa` ending rule. |
| `quick_export` | yes | Quick Export without a dialog: `choose_node`, `export_node` (calls `p3d_export.export_geometry` directly). |
| `quick_export_dialog`, `project_settings_dialog` | GUI only | The `hou.ui` layers of the Quick Export and Project root and presets shelf tools. |
| `p3d_export` | `collect_lods` and `read_geo` | The exporter core: `collect`, `build`, `to_bytes`, `write_file`, `export_geometry`, `run`. The rest works on plain data (`data`) and runs under CPython. |
| `new_p3d` | yes | New P3D without a dialog: `build_starter(parent, name)` builds the starter network of ux-tools.md 7.5 (Res 1, Geometry with component and mass, Memory, Assemble LODs, P3D Export). |
| `batch_convert` | yes | Batch folder convert without a dialog: `convert_folder(input, output, recursive, preset)` re-exports every `.p3d` with a preset into another folder, never into the source folder, and writes `a3ob_batch_report.txt` (identical, different or failed per file). |
| `new_p3d_dialog`, `batch_convert_dialog` | GUI only | The `hou.ui` layers of the New P3D and Batch folder convert shelf tools. |
| `nodes/_export_common` | no | Shared parameters, options and error-level policy of Validate and P3D Export. |

The parameter table (`Parm`, one per parameter) knows these kinds: `string`, `file` (with `file_pattern`), `toggle`,
`button` (needs `action`), `int`, `float`, `menu` and `multiparm`. The fields:

- `name`, `label`, `kind`, `default`, `help` for every kind.
- `action`: for a button, and for a menu that fills other parameters. It calls `a3ob_houdini.entry.button(kind, action, kwargs)`.
- `items`: for a menu, `((token, label), ...)`. A menu is a string parameter and its value is the token; `default` is a token.
- `menu_action`: for a menu whose items depend on the scene (Extract LOD lists the input's LODs). A name in the node
  module's `MENUS`, a function `f(kwargs)` that returns `[(token, label), ...]`. It is called through
  `a3ob_houdini.entry.menu(kind, name, kwargs)`, which never raises. Give `items` or `menu_action`, not both. The
  value is still the token, and the artist may type any other value.
- `size`: for `int` and `float`, how many components; `default` is then a tuple and `read_parms` gives a tuple. A float
  of size 3 is a vector (components `x`, `y`, `z`); other sizes are numbered (a colour of size 4 has `1` to `4`).
- `minimum`, `maximum`: for `int` and `float`. An end that is given is strict (Houdini clamps to it); an end left out is
  not, so the artist can type past the slider (0 to 10).
- `children`: for a multiparm, the parameters of one row, with names ending in `#`. `read_parms` gives a list of row dicts.

`Parm.__post_init__` rejects a wrong combination (unknown kind, button without action, menu without items or with a
default that is not a token, size on a kind that has none, a tuple default of the wrong length, range on a kind that has
none, minimum above maximum).

## The importer in short

`p3d_import.import_model(model, geo, source_key)` follows spec section 16. For each LOD, `plan.make_plan` decides
what every TAGG becomes (group, attribute, packed proxy or blob) and which points and faces stay in the mesh;
`lod_builder.build_lod_geometry` then writes one small function per step: points and polygons (axis map `(x, y, -z)`,
corner map `[c0, c(n-1), ..., c1]`), vertex and face data, normals, float64 UVs, groups and weights, sharp edges, detail
attributes, and last the packed proxies. Each LOD is packed into one prim of `geo` with the outer attribute `name`.

When proxies are interleaved the import moves points and faces (mesh first, proxies last: the order the export writes).
`permutation.Permutation` holds that move (`point_old`, `face_old`, and the derived inverse and corner order) and
`permutation.remap_payload` rewrites a TAGG payload through it. `plan._remap_blobs` and `a3ob_sharp_extra` use it, so masks
and sharp pairs keep marking the same points; a record that cannot be moved is dropped with a warning.

`layout_record.build_record(lod, permutation, sharp)` writes the hidden detail `a3ob_layout` (normal table entry of each
corner, unused triangle slots, sharp pair payload, the permutation, the signs of zero, and a checksum of positions and
topology). On export `lod_build` calls `apply_record` after the TAGGs are in; it moves the built LOD back to the file
order and writes the layout only if the counts and the checksum match, else it leaves the LOD alone and the report gets one
info line. The record is a detail, not a parameter, so Extract LOD and Assemble LODs carry it with the other details.

## The exporter in short

`p3d_export` mirrors the importer: a `hou` layer reads, a plain-Python layer decides. `collect` (spec 3.2) finds the LODs,
reads the determinant of each packed LOD prim, opens it with the `unpack` verb and fills `data.LodData` (`read_geo`).
`build(collection, options, report)` then makes one `a3ob_core` LOD per `LodData` (`lod_build`): signature, `set_mesh`
first (`mesh_build`: axis map, corner map, first-appearance normal table, UVs), then the TAGGs (`selections` for the bytes,
`taggs` for the TAGGs and their order), then `structure_errors()` (hard stop) and `check()` (advisory). Packed proxies are
rebuilt from their embedded triangle and transform (`proxy_rebuild`). `to_bytes` and `write_file` finish; the write goes to a
temporary file and is renamed. Every problem is a message in the `Report`; nothing the artist can fix raises.

`p3d_export.run(geometries, options, report)` is `collect` + `build` + `to_bytes` in one call and returns a `BuildResult`
(`data`, `lod_count`). It is the one place the **Validate** and **P3D Export** nodes get their messages, so both say the
same for the same input. `build_lod` also runs `flag_check` (flag values Object Builder does not know) and `type_rules`
(the 22 tier-2 per-LOD-type rules of spec 17.1.1; `ExportOptions.rules_off` holds the ids that are switched off). First
of all, `build_lod` calls `harden.harden_lod` (task #48): Shadow and Underground LODs always get every edge in
`#SharpEdges#` and face normals, in the written file only. Packed
proxies with a collapsed or isosceles triangle are warned about in `proxy_rebuild`.

Before that, `collect` (`collect_lods._add_lod`) calls `triangulate.triangulate_lod` (task #49, D28; `ExportOptions.triangulate`,
the `triangulate` parameter in the LOD Rules tab, on in both presets): the polygons that the checks reject (more than 4
points; quads that T2-TRI rejects) are divided with the Divide verb on a copy of the LOD geometry, with one warning per
LOD. This is the one place for it, so Validate, P3D Export, Quick Export and Batch convert all do it.

### The Validate and P3D Export nodes

Both nodes build their parameters from `nodes/_export_common.py`: `level_parms` (Errors Show As, Warnings Count As
Errors, Duplicate LODs, Max Messages), `rule_parms` (a master switch and one toggle per tier-2 rule, named `t2<id>`,
for example `t2tri`) and `options_from` (parameter values to `ExportOptions`). `apply_policy` turns errors into
warnings (level `Warning`, text starts with `Error:`) and warnings into errors (strict). A node cooks to its input
unchanged; only the messages are new.

P3D Export (`nodes/p3d_export.py`) never writes while cooking. **Save to Disk** is `save_button`: `run_save` checks the
input again and stops on any error (or on a warning while strict is on), then writes through the temporary file. It
keeps its messages and cooks the node again, because a node can add messages only while it cooks. Scripts and tests call
`nodes.load("p3d_export").save(node)`, which raises `UserError` when nothing was written. `OPTION_PARMS` lists the
parameters a preset has to fill. The **Preset** menu (`preset`, filled from `settings`) and **Apply** (`apply_preset`) set them; the option parameter names never change. An empty Project Root means the shelf setting, then `P:\`.

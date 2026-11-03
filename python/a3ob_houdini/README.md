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
exception shows a short "bug" text and the traceback goes to the console). If the HDA route is ever dropped,
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
| `geo_edit` | at call time | For nodes that edit their input: `copy_input` (output = copy of input 1), detail read, set and remove. |
| `lod_props` | no | LOD Properties decisions: type menu, signature update (through `a3ob_core`), property and copy merge. |
| `material_text` | no | Material Assign text rules: slash normalisation, procedural colour text, known endings. |
| `lod_container` | yes (lazily) | The two shapes of geometry (LOD geometry, P3D geometry): `classify`, `lod_content`, detail copy. Extract LOD and Assemble LODs use it. |
| `lod_match` | no | The Assemble LODs match rule (spec 3.2) as a pure function, `match_lods`. |
| `selection_browser` | yes | Selection browser without a dialog: `list_groups`, `create_selection`, `delete_selection`, `rename_selection`. |
| `selection_browser_dialog` | GUI only | The `hou.ui` layer called by `houdini/toolbar/a3ob_selection_browser.shelf`. |
| `p3d_import` | `lod_builder` and `import_model` | The importer: `plan` (decisions, no `hou`), `lod_builder` (writes geometry), `proxies`, `lod_filter`. |
| `p3d_export` | `collect_lods` and `read_geo` | The exporter core: `collect`, `build`, `to_bytes`, `write_file`, `export_geometry`, `run`. The rest works on plain data (`data`) and runs under CPython. |
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
(the 20 tier-2 per-LOD-type rules of spec 17.1.1; `ExportOptions.rules_off` holds the ids that are switched off). Packed
proxies with a collapsed or isosceles triangle are warned about in `proxy_rebuild`.

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
parameters a preset has to fill.

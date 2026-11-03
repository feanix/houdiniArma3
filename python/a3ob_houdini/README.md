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
| `entry` | at cook time | Frozen entry functions called by the assets. |
| `params` | no | `Parm`, `NodeSpec`, `read_parms`, `add_spare_parms`. |
| `nodes` | no | One module per node kind; `KINDS`, `load`, `all_specs`. |
| `hda_source` | no | Makes the HDA text files from a `NodeSpec`. |
| `messages` | no | `Report`, `message`, `warning`, `error`, text helpers (ux-tools.md section 2). |
| `errors` | no | `UserError`. |
| `names` | only the two group-name wrappers | D-STR helpers, `encode_group_name` / `decode_group_name`, `lod_uid`, int32 helpers. |
| `selection_browser` | yes | Selection browser without a dialog: `list_groups`, `create_selection`, `delete_selection`, `rename_selection`. |
| `selection_browser_dialog` | GUI only | The `hou.ui` layer called by `houdini/toolbar/a3ob_selection_browser.shelf`. |
| `p3d_import` | `lod_builder` and `import_model` | The importer: `plan` (decisions, no `hou`), `lod_builder` (writes geometry), `proxies`, `lod_filter`. |

## The importer in short

`p3d_import.import_model(model, geo, source_key)` follows spec section 16. For each LOD, `plan.make_plan` decides
what every TAGG becomes (group, attribute, packed proxy or blob) and which points and faces stay in the mesh;
`lod_builder.build_lod_geometry` then writes one small function per step: points and polygons (axis map `(x, y, -z)`,
corner map `[c0, c(n-1), ..., c1]`), vertex and face data, normals, float64 UVs, groups and weights, sharp edges, detail
attributes, and last the packed proxies. Each LOD is packed into one prim of `geo` with the outer attribute `name`.

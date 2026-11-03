# Packaging spike: how the Houdini nodes ship (D11)

Task #5. Answers the questions D11 needs before M3 builds nodes. Everything marked
"checked" was run on this machine: Houdini Indie 22.0.466, Python 3.13, licence
category `Indie`. Each claim names the check script and its saved output in
`docs/design/evidence/packaging/` (called "evidence" below). Docs claims name the page.

Related decisions: D3 (HDA licensing deferred), D10 (node and shelf split), D11 (this).

## Recommendation

**Ship the nodes as Python-type HDAs written as text (hybrid), and keep a Python SOP
fallback possible.**

1. Write each node as a *Python-type SOP asset*: a folder of plain text files
   (`DialogScript`, `PythonCook`, a few small files). It loads straight from the folder,
   with no build step and no GUI save. It cooks, takes several inputs and has a
   "Save to Disk" button that calls into `python/a3ob_houdini`.
2. Never save a node asset from the Indie GUI. A GUI-saved asset with a node network
   inside is marked non-commercial, cannot be unpacked to text, and cannot go in git.
3. Keep all logic in `python/a3ob_houdini`. The node file only calls a small set of
   stable entry functions. The same entry functions work from a Python SOP, so the
   fallback costs little.
4. Before a first public release, get one answer from SideFX or a free Apprentice
   test (see "What the owner must decide"). It is a licence question, not a technical one.

Why not the other two:

| Option | Verdict | Reason |
|---|---|---|
| Real HDAs saved from the Indie GUI | No | Binary, marked non-commercial, cannot be expanded to text (Q1, Q2). |
| Python SOP fallback only | Fallback | Works, but no real node type: no name in the Tab menu, no icon or help, and the code and parameters are copied into every `.hip` (Q3). |
| Text-authored Python-type HDA | Yes | Real node type, source in git, loads from the folder, same Python modules (Q2, Q4). |

## Short answers

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Save an HDA from Indie: what file and licence? | Saves whatever name you give (`.hda` or `.hdalc`). Default extension is `.hdalc`. Contents with a node network inside are non-commercial encoded; the name does not matter. | `q1_save_hda`, `q1b_hip_and_taint` |
| 2a | Expand to text and collapse back? | Yes with `hotl -t` / `hotl -l` (and `hou.hda.expandToDirectory` / `collapseFromDirectory`). Not for an Indie-saved asset with a network: "Cannot convert non-commercial HDAs". | `q1b_hip_and_taint`, `q2_text_source` |
| 2b | Does a text-collapsed asset carry the tag? | No marker that `hotl` can see (a commercial asset survives collapse and re-expand byte for byte). Whether SideFX still treats it as Indie output is a licence question I cannot test here. | `q2_text_source` part B |
| 2c | Python SOP HDA from text only? | Yes. Four routes all produced a loadable asset that cooked. It also loads directly from the folder. | `q2_text_source` part C, `q2b_expanded_install` |
| 3 | Python SOP fallback? | Works, survives `.hip` save and load. Breaks when parameters are added or functions renamed, unless entry points are stable and old nodes are upgraded. | `q34_driver` |
| 4 | Save to Disk button? | Works in both options with `pressButton`. Button errors do not raise. | `q34_driver` |
| 5 | Apprentice? | Docs show no Python restriction. Not tested (no Apprentice licence here). | `docs_quotes` |

## Q1. Saving an HDA from this Indie install

Checked (`q1_save_hda.py`, output `q1_save_hda.out.txt`):

- `hou.licenseCategory()` is `licenseCategoryType.Indie`; `hou.isApprentice()` is False.
- `createDigitalAsset(..., hda_file_name="q1_test.hda")` wrote `q1_test.hda`. Naming it
  `q1_test2.hdalc` wrote an `.hdalc`. Indie does **not** rename the file.
- `hou.hda.defaultFileExtension()` returns `.hdalc` in this session. SideFX docs
  (`hom/hou/hda`) say it is "based on the taint".
- `hou.HDADefinition` has no licence field. `hotl -V` prints a `License:` line, but it is
  an empty author text field, not the category.
- The `.hda` file bytes hold no readable words such as "Indie" or "limited".

The marker is in the contents, not the name (`q1b_hip_and_taint.py`):

- `hotl -t` on the Indie-saved `q1_test.hda` prints `Cannot convert non-commercial HDAs`
  and the contents file comes out empty. `hotl -X` fails with `0 blocks`.
- A `.hip` saved from this session under the names `plain.hip`, `plain.hiplc` and
  `plain.hipnc` has the same header (`HouLC`) in all three. So Indie output is marked
  inside the file, whatever extension it has.
- This Indie session loads its own saved `.hda` without trouble and saves a `.hiplc` that
  uses it.

What SideFX documents about opening these files elsewhere (`docs_quotes.out.txt`):

| Source | Statement |
|---|---|
| Licensing page | Commercial uses `.hip/.hda`; Indie uses `.hiplc/.hdalc`; Education and Apprentice use `.hipnc/.hdanc`. Indie is "limited commercial", Education is "non-commercial". Each category "function[s] as a complete pipeline". |
| Houdini Indie page | Indie "cannot be used in the same pipeline as commercial versions of Houdini" and "uses its own file format for saving scenes and assets". |
| Houdini Apprentice page | Apprentice "cannot be used in the same pipeline as commercial/indie versions", uses its own format, and assets made in Apprentice cannot be used with Houdini Engine or Engine Indie. |
| Indie licensing page | A commercial Houdini Engine licence "will not be able to read the HDALC files". |
| `hou.hipFile.saveMode` docs | Text `.hip` is "only available with a full commercial license". Indie cannot save a text hip. |

**Not found in any page I could reach:** what exactly a Houdini FX or Apprentice session
does when it opens an Indie `.hdalc` or `.hiplc` (refuse, load with a mark, or load
freely). Only Indie is installed here, so I cannot run it. The documented statements
above point to "separate pipelines". Treat an Indie-saved asset as not usable by FX
users until SideFX says otherwise.

## Q2. Source build (text in git)

### Expand and collapse

`hotl` in the Indie `bin` folder supports `-x/-X` (expand), `-t` (expand to a
version-control-friendly layout, contents as MIME text) and the matching `-c/-C/-l`
collapse. SideFX docs (`ref/utils/hotl`) recommend `-t` for git. The Python equivalents
are `hou.hda.expandToDirectory` and `hou.hda.collapseFromDirectory`.

Checked (`q2_text_source.py`, `q2_text_source.out.txt`):

| Step | Result |
|---|---|
| `hotl -t` on SideFX's own `OPlibSop.hda` | Works (commercial file). |
| One asset with a node network, `hotl -l` back to `.hda` and `.hdalc` | Works. Loads (`hotl -V`), and `hotl -t` expands it again. |
| Network text `Contents.mime` after collapse and re-expand | Identical (46903 bytes), for both extensions. |
| `hotl -t` / `-X` on an asset saved by Indie that has a network | Fails (see Q1). |

So the usual SideFX source workflow works for text that did not come from an Indie GUI
save. In practice Indie users cannot start from a GUI-built network asset and put it in
git. That is why the recommendation uses Python-type assets, which have no network.

### Does a text-collapsed asset carry the limited-commercial tag?

I found no marker I could detect. After Indie `hotl -l`, the asset expands again with
`hotl -t` and no error. The only on-disk marker found in Q1 is in the encoded `Contents`
section of an Indie GUI save. A Python-type asset has no `Contents` section at all.
Re-saving the text-built Python SOP with `copyToHDAFile` from Indie gives a file the
same size whose only differences are five time-stamp bytes (`q2_text_source`, part D).

Two limits on that finding. I cannot see how a commercial Houdini treats the file.
And the Indie licence terms apply to what the owner makes with Indie, whatever the file
contains. Do not read "no detectable marker" as "cleared by SideFX". See the owner list.

### Python SOP asset from text only

Checked (`q2_text_source.py` part C). A folder with these files, written by hand:

```
hda_src/
  houdini.hdalibrary          (empty)
  INDEX__SECTION              (listing: name, label, inputs, "Python: true")
  Sections.list
  Sop_1a3ob__spike__export/
    DialogScript              (name, label, parameters, button)
    PythonCook                (the cook code)
    TypePropertiesOptions
    Sections.list
```

Collapsed four ways (`hotl -l` to `.hdalc`, `hotl -l` to `.hda`, `hotl -c`,
`hou.hda.collapseFromDirectory`). All four loaded in hython and cooked a box with
`errors ()`. Notes:

- `INDEX__SECTION` must not be empty. An empty one collapses without error but the file
  is then refused ("Cannot load library file"). It is the text listing that `hotl -V`
  prints, so it is easy to write.
- The input limit lives in that listing (`Inputs: 1 to 4`), not in `DialogScript`
  (`q4b_multi_input`). A node with three connected inputs works. This covers
  "Assemble LODs".
- Python-ness is decided by having a `PythonCook` section; `hotl -V` then prints `Python: true`.
- The `.hda` and `.hdalc` outputs are byte-identical in size (992 bytes); the extension
  is only a name.

Loading the folder directly (`q2b_expanded_install.py`): copy the folder into an
`otls` directory under a name ending `.hda`, and Houdini loads it as the library
(`loaded library paths: ['a3ob_spike_export.hda (directory)']`). It cooked. This matches
the `hotl` docs ("install assets from an expanded directory directly"). So the package
can ship the source folder as is: `houdini/otls/<name>.hda/`.

Line endings (`q2c_crlf.py`): git on Windows may check the files out with CRLF. With
every file converted to CRLF, collapse and load still worked, in both file and folder
form, and the parameters and button callback were intact. No `.gitattributes` rule is
needed for this, though one pinning LF would keep the files stable across machines.

## Q3. Python SOP fallback

Setup (`spike_v1/`, `spike_v2/`, `q34_phase.py`, `q34_driver.py`). A throwaway module
`a3ob_spike` stands in for `python/a3ob_houdini` (scope of this task is docs only; the
real package is on the path too, and imports fine: `real packages importable: True True`).
Its `shelf.create_python_sop` is what a shelf script would call. It creates a Python
SOP, sets the code, and adds spare parameters (`tag`, `path`, a `save` button) through
`parmTemplateGroup`.

Two wrapper styles were stored in the same `.hip`:

| Name | Code stored in the node |
|---|---|
| `stable` | `import a3ob_spike.entry as e` then `e.cook(hou.pwd())` |
| `naive` | `from a3ob_spike.nodes import cook` then `cook(hou.pwd())` |

Results (`q34_driver.out.txt`):

| Step | stable | naive |
|---|---|---|
| Create, cook, set parameter | ok | ok |
| New hython process, same code, load the `.hip` | ok, parameters and values kept | ok |
| Package updated to v2 (cook renamed `run_cook`, new parameter `scale`, new default for `tag`), load the old `.hip` | cook fails: `Invalid parm name` (the old node has no `scale`) | cook fails: `ImportError: cannot import name 'cook'` |
| Fix: call `entry.upgrade(node)` (adds missing parameters) | ok | still broken until its stored code is rewritten |
| Button after update | callback runs, then fails on the cooked geometry (the cook failed) | same |

What the drift tests show:

- Parameters and wrapper code are stored **in the `.hip`**. A package update does not
  touch them. An old scene keeps the old layout.
- A **renamed function** breaks every node whose stored code imports it. The stable
  entry layer fixed that: v2 changed `nodes.cook` to `run_cook` and `stable` did not notice.
- **New parameters** break old nodes the moment the new code reads them. `evalParm`
  on a missing parameter raises `hou.OperationFailed`.
- **Changed defaults** reach only new nodes. The old node kept its value (`from-stable`);
  a node made by the v2 shelf function got `hello-v2`.
- Self-healing inside the cook (calling `upgrade` from the node code) worked in this
  test (`selfheal errors=none`), but the SideFX Python SOP page says "Do not set node
  parameters inside a Python SOP" because it "creates broken dependencies". I do not
  recommend it. Use an explicit upgrade (shelf button, or run on scene load).

### Where node logic must live

So that either wrapper works:

1. All logic in `python/a3ob_houdini/` modules (and `a3ob_core` for formats). Node
   code never lives in the wrapper.
2. One small module of **stable entry functions** (name it e.g.
   `a3ob_houdini.entry`). The wrapper text (Python SOP code, HDA `PythonCook`, button
   callbacks) calls only these. They take the node (or `kwargs`) and, once there are
   several node kinds, a kind name. Their names and signatures are frozen once shipped.
   Everything behind them can be renamed.
3. One place that describes each node's parameters (in Python). The shelf fallback adds
   them with `parmTemplateGroup`. The HDA `DialogScript` is the same list in text. A
   build script generating the second from the first would avoid two copies; I did not
   test that.
4. Read optional parameters through a helper that returns a default when a parameter is
   missing, so a new module on an old node degrades instead of crashing.
5. Store a layout version on each node and provide an `upgrade` entry (shelf action).
   Only the fallback needs it; with the HDA, Houdini does it (next section).

## Q4. SOP-level output node with Save to Disk

Both options are the same pattern: a pass-through SOP with a file path parameter and a
button parameter whose callback is Python that calls the entry function, which writes the
file from the node's cooked geometry.

| | Python SOP fallback | Python-type HDA |
|---|---|---|
| Button defined by | `hou.ButtonParmTemplate(script_callback="import a3ob_spike.entry as e; e.save(kwargs)", script_callback_language=hou.scriptLanguage.Python)` | In `DialogScript`: `type button` with `parmtag { "script_callback" "..." }` and `parmtag { "script_callback_language" "python" }` |
| Callback gets | `kwargs["node"]` | `kwargs["node"]` |
| Where the callback text is stored | In the `.hip` | In the HDA, so a package update changes it |
| Checked | `pressButton()` wrote `{"module_version": 1, "points": 8, "tag": "from-stable"}` | `pressButton()` wrote `{"module_version": 1, "points": 8, "tag": "from-hda"}` |

Both checks ran headless in hython with `hou.Parm.pressButton()`; the file existed with
the expected JSON afterwards (`q34_driver.out.txt`, lines starting `pressButton`).

Findings that matter for the real export:

- **Errors in a callback do not raise.** The docs say `pressButton` raises
  `hou.OperationFailed` if the script "could not be run". In the checks, a callback that
  raised an `AttributeError` printed `Error running callback:` and `pressButton` returned
  normally. The real export function must catch its own errors and report them (node
  error or warning, message in the GUI) and must not rely on the caller seeing an exception.
- A failed callback can leave an empty file behind (the spike opened the file before it
  failed). Write to a temp name and rename on success.
- A button in a Python SOP runs on the cooked node: `node.geometry()` is its cooked output.

### HDA update with the Python-type HDA (the drift comparison)

Same scenario as Q3, HDA version. v1 scene saved with the HDA found on the scan path
(`HOUDINI_PATH`/`otls`), then the HDA file replaced by v2 (adds `scale`, new `tag`
default) and the old `.hip` loaded:

| Item | Result |
|---|---|
| Parameters on the old node | `['tag', 'scale', 'path', 'save']`: the new parameter appeared on its own, value 1.0 |
| Value set in the old scene | Kept (`from-hda`) |
| Cook | ok, no errors |
| Button | callback from the new definition ran; JSON says `module_version: 2` |
| New node default | `hello-v2` |

So the HDA removes the parameter-layout drift that the fallback has. Renamed functions
are still avoided the same way (stable entry functions).

## Q5. Apprentice

Docs only, as asked.

- The Houdini Apprentice page lists its limits: non-commercial use, own file formats,
  1920x1080 renders with watermarks, no third-party renderers, and "Houdini Digital
  Assets created in Apprentice can not be used with Houdini Engine". It says Apprentice
  has "virtually all of the features" of Houdini FX. **No Python restriction is listed.**
- The Python SOP node page and the "define a SOP with Python" page
  (`hom/pythonsop`) carry no licence note. Python-type assets are an ordinary
  feature ("File > New Asset", operator definition `python`).

Answer: Python SOP and Python-type HDAs are available in Apprentice according to the
docs; functionally unchecked here.

Open point: whether an Apprentice session can *load* an asset created under Indie is the
cross-licence question in Q1, not documented in the pages I could reach.

## What the owner must decide

1. **Licence check before the first public release.** Two questions for SideFX support or
   a test with a free Apprentice licence:
   - Can Apprentice and Houdini FX load a text-authored Python-type asset (the folder
     loaded as `<name>.hda`), and a `.hdalc` made from it?
   - Is such an asset, written by hand and never saved by the Indie GUI, still Indie
     output under the Indie terms? (The code is GPL and owner-written.)
   A cheap test: run `evidence/packaging/q2b_expanded_install.py` under an Apprentice
   or FX session. I cannot do it here. Until answered, D3 stays open for the public
   release, not for owner use.
2. **Indie-only for now?** If the first audience is the owner on Indie, nothing above blocks
   building. If the community includes FX or Apprentice users, item 1 gates the HDA route.
   The Python SOP fallback has the same cross-licence exposure for the `.hip` files (they
   are Indie files either way), but no HDA file is shared, only the package.
3. **Accept "no GUI-built node networks".** Every node must be Python-driven. That fits
   D10 (the nodes are all code), but it rules out building a node in the GUI by wiring
   other nodes together and saving it as an asset. Confirm that is acceptable.
4. **Python speed.** Both options run Python for cooks. P3D import of large models in
   Python is a separate performance question for M3. Not measured here.

## Limits of this spike

- Only an Indie licence is installed. FX, Education and Apprentice behaviour comes from
  SideFX pages only; where those are silent I said so.
- The fallback and HDA tests use a throwaway module, not the real `python/a3ob_houdini`.
- I did not test the HDA in the GUI (Tab menu, icon, help). Docs say Python-type assets
  support them like other assets (`Tools.shelf` is a standard section in SideFX's own
  Python SOPs); not checked.
- Loading a `.hip` on a machine where the package is missing was not tested.
- Houdini 21 was not used.

## Evidence index

All under `docs/design/evidence/packaging/`. Scripts are re-runnable with hython (the
two driver scripts with plain Python); each `*.out.txt` is the saved output.

| Script | Output | Covers |
|---|---|---|
| `q1_save_hda.py` | `q1_save_hda.out.txt` | Q1 save, extension, category |
| `q1b_hip_and_taint.py` | `q1b_hip_and_taint.out.txt` | Q1 `.hip` header, `hotl` refusal |
| `q2_text_source.py` | `q2_text_source.out.txt` | Q2 expand, collapse, text-built asset, tag |
| `q2b_expanded_install.py` | `q2b_expanded_install.out.txt` | Q2 load from folder |
| `q2c_crlf.py` | `q2c_crlf.out.txt` | Q2 CRLF line endings |
| `q4b_multi_input.py` | `q4b_multi_input.out.txt` | several inputs |
| `q34_driver.py`, `q34_phase.py` | `q34_driver.out.txt` | Q3 fallback and drift, Q4 button, HDA update |
| `spike_v1/`, `spike_v2/` | | throwaway module and HDA text sources |
| `docs_quotes.py` | `docs_quotes.out.txt` | SideFX doc lines cited |
| `fetch_doc.py` | | prints any SideFX docs page as text |

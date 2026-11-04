# houdiniArma3

A port of [Arma 3 Object Builder](https://github.com/MrClock8163/Arma3ObjectBuilder)
(A3OB, a Blender add-on by MrClock) to SideFX Houdini: import and export of
Arma 3 P3D models and related formats.

**Status: pre-alpha.** Import, editing nodes, checks and export work in Houdini; see the
[install guide](docs/user/install.html) and the quick starts. Target: Houdini 22.0
(Indie 22.0.466, Python 3.13).

License: GPL-3.0-or-later, as a modified version of A3OB. See [LICENSE](LICENSE)
and [NOTICE.md](NOTICE.md).

**User documentation:** open [docs/user/index.html](docs/user/index.html) in a browser (works offline).

## Known limitations

Not ported yet (the full list is in [What's not here yet](docs/user/about.html#not-here-yet)): RTM animations,
model.cfg skeletons and rigging, PAA textures, and terrain files (ASC heightmaps, Terrain Builder object lists).

Not well supported or tested yet:

- Tested only on Windows 11 with Houdini Indie 22.0.466. Other Houdini versions, Linux and macOS are untested.
- Houdini FX and Apprentice are untested.
- Installed only by copying the package file into the Houdini preferences folder (Way A in the
  [install guide](docs/user/install.html)). Installing through `HOUDINI_PACKAGE_DIR` is untested.
- Real use so far: one model taken from Houdini through P3D Export and HEMTT into Arma 3, plus round trips of Bohemia's
  34 sample models. Large or complex models (vehicles, characters, buildings with many proxies) have not been tried in
  game.
- The HEMTT workflow is tested end to end. A P drive workflow is tested only by the automatic tests.
- Binarized (ODOL) .p3d files cannot be opened, only editable MLOD files.
- Textures are not shown in the Houdini viewport yet.

Found a problem? Please report it on the [issues page](https://github.com/feanix/houdiniArma3/issues).

## Layout

| Path                        | Contents                                                    |
| --------------------------- | ----------------------------------------------------------- |
| `python/a3ob_core/`         | Pure Python: format I/O and algorithms. Never imports `hou`. Standard library only. |
| `python/a3ob_houdini/`      | Houdini glue (needs `hou`).                                 |
| `houdini/`                  | Houdini content root (shelves, HDAs, menus, later).         |
| `packages/a3ob_houdini.json`| Houdini package file.                                       |
| `tests/`                    | `unittest` tests and fixtures (`tests/fixtures/p3d/`).      |
| `scripts/run_tests.py`      | Test runner.                                                |
| `docs/`                     | Research and design notes. `docs/user/` is the user documentation (static HTML; rebuild the generated reference with `python scripts/build_user_docs.py`). |

## Running the tests

The tests use the standard library `unittest` only; no packages to install.
The pure layer runs on system CPython 3.12+ and on hython:

```sh
python scripts/run_tests.py
"E:/SteamLibrary/steamapps/common/Houdini Indie/bin/hython.exe" scripts/run_tests.py
```

`-v` gives verbose output. The exit code is non-zero when a test fails.

## Installing the Houdini package

Do one of these, then restart Houdini:

- **Point Houdini at the repo** (no copying): set
  `HOUDINI_PACKAGE_DIR=<repo>/packages` before Houdini starts. The package
  finds the repo root on its own.
- **Copy the package file** into `$HOUDINI_USER_PREF_DIR/packages/`
  (on Windows, `%USERPROFILE%/Documents/houdini22.0/packages/`). A copied file
  cannot work out where the repo is, so also set the root, either in the
  environment or by editing the first `env` entry of the copy:

  ```sh
  A3OB_HOUDINI=E:/Projects/houdiniArma3
  ```

The package sets `A3OB_HOUDINI` (default: the folder above the package file),
adds `$A3OB_HOUDINI/houdini` to `HOUDINI_PATH` and `$A3OB_HOUDINI/python` to
`PYTHONPATH`. It only loads in Houdini 22.0 or later. To check it, run
`hconfig -ap` or, in a Python shell, `hou.getenv("A3OB_HOUDINI")`.

# houdiniArma3

A port of [Arma 3 Object Builder](https://github.com/MrClock8163/Arma3ObjectBuilder)
(A3OB, a Blender add-on by MrClock) to SideFX Houdini: import and export of
Arma 3 P3D models and related formats.

## AI disclosure

The code, the tools and the documentation were written with AI assistance (Claude, by Anthropic). The
project owner directed the work, tested it and checked it. If you would rather not use AI-assisted tools,
that is completely fine. Two alternatives are
[Arma 3 Object Builder for Blender](https://github.com/MrClock8163/Arma3ObjectBuilder) (A3OB, by MrClock) and
[Arma 3 Tools](https://store.steampowered.com/app/233800/Arma_3_Tools/) (including Object Builder), the
official tools from Bohemia Interactive.

**Status: pre-alpha.** Import, editing nodes, checks and export work in Houdini; see the
[install guide](docs/user/install.html) and the quick starts. Target: Houdini 22.0
(Indie 22.0.466, Python 3.13).

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

## Installing the Houdini package

Do one of these, then restart Houdini:

- **Point Houdini at the folder** (no copying): set
  `HOUDINI_PACKAGE_DIR=<folder>/packages` before Houdini starts, where `<folder>` is this download. The package
  finds the folder on its own.
- **Copy the package file** into `$HOUDINI_USER_PREF_DIR/packages/`
  (on Windows, `%USERPROFILE%/Documents/houdini22.0/packages/`). A copied file
  cannot work out where the download is, so also set the root, either in the
  environment or by editing the first `env` entry of the copy:

  ```sh
  A3OB_HOUDINI=C:/tools/houdiniArma3
  ```

The package sets `A3OB_HOUDINI` (default: the folder above the package file),
adds `$A3OB_HOUDINI/houdini` to `HOUDINI_PATH` and `$A3OB_HOUDINI/python` to
`PYTHONPATH`. It only loads in Houdini 22.0 or later. To check it, run
`hconfig -ap` or, in a Python shell, `hou.getenv("A3OB_HOUDINI")`.

## Using it

Open [docs/user/index.html](docs/user/index.html). The two quick starts take you through a first import and a first
export. The reference pages describe every node.

## License and credits

License: GPL-3.0-or-later, as a modified version of A3OB. See [LICENSE](LICENSE)
and [NOTICE.md](NOTICE.md) for the credits to A3OB and its authors (MrClock and Hans-Joerg "Alwarren" Frieden).

Found a problem? Please report it on the issues page of the project's repository.

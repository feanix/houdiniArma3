# Notice

houdiniArma3 (`a3ob-houdini`) is a port of **Arma 3 Object Builder** (A3OB),
a Blender add-on, to SideFX Houdini.

## Upstream

- **Arma 3 Object Builder**, by **MrClock** (present add-on) and
  **Hans-Joerg "Alwarren" Frieden** (original ArmaToolbox).
- Source: <https://github.com/MrClock8163/Arma3ObjectBuilder>
- Ported from version **2.5.1**, commit **9f2b8bc**.
- License: GNU General Public License, version 3 or (at your option) any later
  version.

## This is a modified version

This project is a derivative work of A3OB and is distributed under the same
license, GPL-3.0-or-later. The full license text is in [LICENSE](LICENSE).

It is not the original program. The A3OB code and data tables it reuses
(file format readers and writers, LOD and flag tables, named property lists
and similar) have been translated and modified: rewritten to run without
Blender (`bpy`), split into a pure-Python core (`python/a3ob_core`) and Houdini
glue (`python/a3ob_houdini`), and changed where the port fixes upstream bugs.
Changes started in 2026. Convention: every source file derived from A3OB
names the upstream file it came from in its header and states that it was
modified.

The upstream authors are not responsible for this port and do not endorse it.
Report problems with this port here, not upstream.

## Test fixtures

`tests/fixtures/p3d/*.p3d` (`sample_1_character.p3d`, `sample_2_crate.p3d`)
are copied unchanged from A3OB's test inputs at the commit above.

## Files you export

Models, textures and other files you create or export with this tool are not
covered by its license.

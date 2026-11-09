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

## Selection name translation table

The export option "Translate names to Czech" uses A3OB's table of selection names: `translations_czech_english`
(144 entries, Czech to English) in `utilities/data.py` of A3OB 2.5.1, commit **9f2b8bc**, and its inverse. The table is
copied from A3OB without changes to its entries, which is allowed because A3OB is licensed GPL-3.0-or-later like this
project. Credit for it goes to A3OB and its authors, **MrClock** and **Hans-Joerg "Alwarren" Frieden**. A3OB's source
does not name any other author or origin for the table, and none is claimed here. The file that holds it in this
project says that it is derived from A3OB and was modified (it runs without Blender, and adds two inverse-only
aliases for the misspelled `properller_a_axis` and `properller_b_axis`). The design notes for it are kept in
the development repository, not in this copy.

## Files you export

Models, textures and other files you create or export with this tool are not
covered by its license.

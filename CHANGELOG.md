# Changelog

Agent-facing: read the entries between your pinned version and the one you upgrade to.

## 0.1.0 (2026-11-04)

houdiniArma3 0.1.0: first release (D30, D31, D32). P3D (MLOD) import, editing nodes, checks and export in Houdini Indie 22.0.466.

Release-prep work in this version:
- #46 Node messages go to Houdini's Log Viewer (source A3OB) instead of opening the console.
- #47 New node A3OB Components: find components, convex hull per piece.
- #48 Shadow Volume and Underground LODs are always written with hard edges (T2-SHARP removed).
- #49 Export triangulates where the LOD rules need it (toggle "Triangulate Where Needed", on by default).
- #50 Group fields in Material Assign and Mass are Houdini group pickers.
- #55 Path mapping: $PBOPREFIX$ files with several keys read the prefix key; \\server paths count as absolute.
- #56 Docs: Concepts game-path section, About "What's not here yet", README Known limitations.
- #57 AI disclosure in README and About page.

Gates on 58fcfac: full PASS (CPython 3.12 + hython 3.13), samples PASS (all 34 Arma 3 Samples files).
Not ported yet: RTM, model.cfg/rigging, PAA, terrain files. Unplaced: #52, #53, #54; on hold: #58.


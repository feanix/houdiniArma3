# Changelog

Agent-facing: read the entries between your pinned version and the one you upgrade to.

## 0.2.0 (2026-11-04)

houdiniArma3 0.2.0: textures in the viewport (D29, D33, D34, D35).

- #53 PAA reader: pure-Python port of A3OB's reader (DXT1, DXT5, LZO), numpy when available; decoded images cached as PNG under $HOUDINI_TEMP_DIR/a3ob_paa_cache (settings.paa_cache_dir overrides). Swizzled textures (SWIZ TAGG: _nohq, _dt, _adshq) are decoded without the swizzle, as in A3OB.
- #54 Material Assign "Show in Viewport" (on by default): one principledshader per texture in the a3ob_viewport_materials matnet, white tint, named from the texture file; .png/.tga/.tif used directly, .paa via a sibling image or the PAA cache, procedural colours as flat colour; in-game paths not found on disk give one info line per node (showing them waits for #52).
- #54 / D34 export option "Write Textures as .paa": on in Arma 3 release, off in Lossless (Lossless keeps texture paths exactly as read); saved presets without the key follow Relative Paths.
- #59 Material Assign "Reload Textures" button: re-decodes shown .paa files, runs texcache -c and glcache -c, re-cooks.

Gates on 644186a: full PASS (CPython 3.12 + hython 3.13), samples PASS (all 34 Arma 3 Samples files, Lossless byte-identical).
Unplaced: #52 (unified path mapping), #58 (icons, on hold).

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


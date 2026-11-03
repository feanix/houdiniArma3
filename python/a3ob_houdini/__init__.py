"""Houdini glue for houdiniArma3, the Houdini port of Arma 3 Object Builder.

This package holds the code that needs Houdini's ``hou`` module: importers,
exporters and tools that turn ``a3ob_core`` data into Houdini geometry and
back. The format code itself lives in ``a3ob_core``, which never imports
``hou``.

Importing this package does not import ``hou``; modules that need it import
it themselves, so the package can be inspected outside Houdini.

Derived from Arma 3 Object Builder by MrClock (present add-on) and
Hans-Joerg "Alwarren" Frieden (original ArmaToolbox). Modified for Houdini.
See NOTICE.md.

SPDX-License-Identifier: GPL-3.0-or-later
"""

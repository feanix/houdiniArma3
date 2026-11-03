# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Reload A3OB: the work of the Reload A3OB shelf tool, without a dialog.

A Houdini session keeps every module it imported. After the package is updated, the session still runs the old code
until Houdini restarts. ``reload_all`` removes the A3OB modules from ``sys.modules`` and imports the entry module
again, so the next cook, button or menu uses the new files. The asset files on disk are read by Houdini itself and
are not touched here.
"""

import importlib
import sys

__all__ = ["PACKAGES", "reload_all"]

#: The top-level packages that make up A3OB.
PACKAGES = ("a3ob_houdini", "a3ob_core")


def _is_a3ob(name: str) -> bool:
    return any(name == package or name.startswith(package + ".") for package in PACKAGES)


def reload_all() -> int:
    """Forget every A3OB module and import ``a3ob_houdini.entry`` again. Returns how many modules were forgotten."""
    importlib.invalidate_caches()
    forgotten = [name for name in sys.modules if _is_a3ob(name)]
    for name in forgotten:
        del sys.modules[name]
    importlib.import_module("a3ob_houdini.entry")
    return len(forgotten)

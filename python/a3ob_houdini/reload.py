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
import logging
import sys

__all__ = ["PACKAGES", "reload_all", "reregister_states"]

#: The top-level packages that make up A3OB.
PACKAGES = ("a3ob_houdini", "a3ob_core")


def _is_a3ob(name: str) -> bool:
    return any(name == package or name.startswith(package + ".") for package in PACKAGES)


def reregister_states(hou_module, utils_module=None) -> list:
    """Register the embedded viewer state again for every installed a3ob node type that has a DefaultState section.

    A state that is registered keeps the old code, so it is unregistered first. Returns the type names done. A failure
    goes to the A3OB log and never stops the others."""
    done = []
    try:
        if utils_module is None:
            utils_module = importlib.import_module("viewerstate.utils")
        types = hou_module.sopNodeTypeCategory().nodeTypes().values()
    except Exception:
        logging.getLogger("a3ob").exception("Reload A3OB: cannot reach the viewer state registry")
        return done
    for node_type in types:
        try:
            definition = node_type.definition()
            if not node_type.name().startswith("a3ob_") or definition is None or "DefaultState" not in definition.sections():
                continue
            try:
                utils_module.unregister_pystate_embedded(node_type)
            except Exception:  # it was not registered
                pass
            utils_module.register_pystate_embedded(node_type)
            done.append(node_type.name())
        except Exception:
            logging.getLogger("a3ob").exception("Reload A3OB: cannot register the viewer state of %s", node_type.name())
    return done


def reload_all() -> int:
    """Forget every A3OB module and import ``a3ob_houdini.entry`` again. Returns how many modules were forgotten.

    With the Houdini window, the embedded viewer states are registered again, so a changed state takes effect."""
    importlib.invalidate_caches()
    forgotten = [name for name in sys.modules if _is_a3ob(name)]
    for name in forgotten:
        del sys.modules[name]
    importlib.import_module("a3ob_houdini.entry")
    try:
        import hou
        if hou.isUIAvailable():
            reregister_states(hou)
    except Exception:
        logging.getLogger("a3ob").exception("Reload A3OB: viewer states not registered again")
    return len(forgotten)

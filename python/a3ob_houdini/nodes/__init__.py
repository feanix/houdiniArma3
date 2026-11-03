# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The A3OB nodes. One module per node kind, named after the kind (``p3d_import``).

A node module has three public names:

- ``SPEC``: its ``NodeSpec`` (the parameter table, the only list of the node's parameters);
- ``cook(node, report)``: the work. Raise ``UserError`` for problems the artist can fix;
- ``BUTTONS``: ``{action name: function(kwargs)}`` for its button parameters (may be empty).

``KINDS`` lists every kind: every module in this package whose name does not start with ``_``. Adding a node is
adding its module; no list to edit, so node tasks built in parallel do not collide here.
"""

import importlib
import pkgutil
from typing import Dict, Tuple

from ..params import NodeSpec

__all__ = ["KINDS", "load", "all_specs"]

KINDS: Tuple[str, ...] = tuple(sorted(
    info.name for info in pkgutil.iter_modules(__path__) if not info.name.startswith("_")
))


def load(kind: str):
    """The module of a node kind. Raises ``KeyError`` for an unknown kind."""
    if kind not in KINDS:
        raise KeyError("unknown A3OB node kind %r" % kind)
    return importlib.import_module("%s.%s" % (__name__, kind))


def all_specs() -> Dict[str, NodeSpec]:
    """``{kind: NodeSpec}`` for every node."""
    return {kind: load(kind).SPEC for kind in KINDS}

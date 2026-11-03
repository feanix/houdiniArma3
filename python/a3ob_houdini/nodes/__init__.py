# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The A3OB nodes. One module per node kind, named after the kind (``p3d_import``).

A node module has three public names:

- ``SPEC``: its ``NodeSpec`` (the parameter table, the only list of the node's parameters);
- ``cook(node, report)``: the work. Raise ``UserError`` for problems the artist can fix;
- ``BUTTONS``: ``{action name: function(kwargs)}`` for its button parameters (may be empty).

``KINDS`` lists every kind. Add a kind here when its module is written. ``a3ob_houdini.entry`` is the only caller.
"""

import importlib
from typing import Dict, Tuple

from ..params import NodeSpec

__all__ = ["KINDS", "load", "all_specs"]

KINDS: Tuple[str, ...] = ("p3d_import",)


def load(kind: str):
    """The module of a node kind. Raises ``KeyError`` for an unknown kind."""
    if kind not in KINDS:
        raise KeyError("unknown A3OB node kind %r" % kind)
    return importlib.import_module("%s.%s" % (__name__, kind))


def all_specs() -> Dict[str, NodeSpec]:
    """``{kind: NodeSpec}`` for every node."""
    return {kind: load(kind).SPEC for kind in KINDS}

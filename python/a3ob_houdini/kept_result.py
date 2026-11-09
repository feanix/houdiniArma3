# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The kept result of a slow node (tasks #94 and #97): the one place that decides "nothing has changed, give back the last
result".

Houdini cooks a node again whenever a node in front of it is dirty, even when that node gives the same geometry. The rig
nodes (Rig Fit, Weight Transfer, Accessory Weights, Weight Cleanup, Unpose, LOD Weights) take seconds to cook, so each keeps
its last result under a key. The key is a content hash (``geo_fingerprint``) of every input geometry plus every parameter
except the ones the node declares display-only. A cook with the same key gives the kept result and the kept node messages
without computing again.

A node uses it like this::

    values = read_parms(node, SPEC)
    key = kept_result.make_key(node, values, display_only=("show",))   # display_only is optional
    if kept_result.replay_geometry(node, report, key):                 # or lookup() when the output is not a plain copy
        return
    ... compute into node.geometry() and fill the report ...
    kept_result.keep_geometry(node, report, key)                       # or store() with your own payload

At most ``LIMIT`` nodes keep a result, over all node kinds (the oldest use goes first), so memory stays bounded. ``STATS``
counts hits and misses (the tests and the measuring scripts read it).
"""

import hashlib
from collections import OrderedDict

from . import geo_fingerprint

try:
    import hou
except ImportError:  # the module is only used inside Houdini
    hou = None

__all__ = ["make_key", "lookup", "store", "replay_geometry", "keep_geometry", "forget", "LIMIT", "STATS"]

#: How many nodes keep a result, over all node kinds (a result holds geometry).
LIMIT = 24
#: ``{"hits": n, "misses": n}`` since the session began.
STATS = {"hits": 0, "misses": 0}
#: ``{(node type, node session id): (key, payload, report items)}``, the least recently used first.
_KEPT = OrderedDict()


def make_key(node, values: dict, display_only=()) -> bytes:
    """A hash of everything the node reads: the contents of all its input geometries and every parameter in ``values``
    except those named in ``display_only`` (they change what is drawn, not the result)."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(node.type().name().encode())
    for number in range(len(node.inputConnectors())):
        geo = node.inputGeometry(number)
        digest.update(b"none" if geo is None else geo_fingerprint.fingerprint(geo))
    digest.update(repr(sorted((name, value) for name, value in values.items() if name not in display_only)).encode())
    return digest.digest()


def _id(node):
    return node.type().name(), node.sessionId()


def lookup(node, key: bytes):
    """``(payload, report items)`` kept for ``key``, or None. Counts a hit or a miss."""
    kept = _KEPT.get(_id(node))
    if kept is None or kept[0] != key:
        STATS["misses"] += 1
        return None
    _KEPT.move_to_end(_id(node))
    STATS["hits"] += 1
    return kept[1], kept[2]


def store(node, key: bytes, payload, report) -> None:
    """Keep ``payload`` and the messages of ``report`` for ``key``, as the last result of this node."""
    if report.has_errors:  # an error clears the output; nothing to keep
        _KEPT.pop(_id(node), None)
        return
    _KEPT[_id(node)] = (key, payload, list(report.items))
    _KEPT.move_to_end(_id(node))
    while len(_KEPT) > LIMIT:
        _KEPT.popitem(last=False)  # a deleted node leaves its entry here until it is the oldest


def forget(node) -> None:
    """Drop the kept result of a node (tests)."""
    _KEPT.pop(_id(node), None)


def replay_geometry(node, report, key: bytes) -> bool:
    """When a result is kept for ``key``: make it the output of the node, add its messages to ``report``, return True."""
    found = lookup(node, key)
    if found is None:
        return False
    geo = node.geometry()
    geo.clear()
    geo.copy(found[0])
    report.items.extend(found[1])
    return True


def keep_geometry(node, report, key: bytes) -> None:
    """Keep the output of the node as it is now (a copy) for ``key``. Call it at the end of a cook that did not fail."""
    if report.has_errors:
        store(node, key, None, report)
        return
    kept = hou.Geometry()
    kept.copy(node.geometry())
    store(node, key, kept, report)

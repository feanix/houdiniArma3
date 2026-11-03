"""Shared helpers for the tests that need Houdini's ``hou`` module (they skip under plain Python).

Not a test file: the name does not start with ``test``.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import time

from a3ob_core import p3d

try:
    import hou  # noqa: F401
    HAS_HOU = True
except ImportError:
    hou = None
    HAS_HOU = False

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "p3d")
FIXTURE_NAMES = ("sample_1_character.p3d", "sample_2_crate.p3d")
SOURCE_KEY = "/obj/geo1/p3d_import1"

_cache = {}


def read_fixture(name):
    """The M1 model of a fixture (not cached: tests may edit it)."""
    return p3d.read_p3d_file(os.path.join(FIXTURES, name), strict=False)


def import_model(model, source_key=SOURCE_KEY, **kwargs):
    """Import a model into a fresh geometry. Returns ``(geo, report, seconds)``."""
    from a3ob_houdini import messages, p3d_import
    geo = hou.Geometry()
    report = messages.Report()
    started = time.perf_counter()
    p3d_import.import_model(model, geo, source_key, report, **kwargs)
    return geo, report, time.perf_counter() - started


def imported_fixture(name):
    """``(model, geo, report, seconds)`` of a fixture imported with the default key, cached for the whole run."""
    if name not in _cache:
        model = read_fixture(name)
        geo, report, seconds = import_model(model)
        _cache[name] = (model, geo, report, seconds)
    return _cache[name]


def polygons(geo):
    return [prim for prim in geo.prims() if isinstance(prim, hou.Polygon)]


def packed_prims(geo):
    return [prim for prim in geo.prims() if isinstance(prim, hou.PackedPrim)]


def group_numbers(group):
    return [item.number() for item in (group.points() if isinstance(group, hou.PointGroup) else group.prims())]

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


OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")

# The one owner of HDA installs for tests (#22). Each file has a count of the test classes that want it. The file is
# installed on the first request and uninstalled when the last holder lets go, so no module can take away a file
# another module still needs, whatever order the modules run in.
_holders = {}


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def install_hdas_for_class(paths):
    """Hold HDA files for a test class (installing those not yet loaded). Pass the result to ``uninstall_hdas``."""
    held = []
    for path in paths:
        path = path.replace("\\", "/")
        key = _key(path)
        if not _holders.get(key):
            hou.hda.installFile(path)
        _holders[key] = _holders.get(key, 0) + 1
        held.append(path)
    return held


def uninstall_hdas(paths):
    """Let go of files held by ``install_hdas_for_class``. A file is uninstalled only when nobody else holds it."""
    for path in paths:
        key = _key(path)
        _holders[key] = _holders.get(key, 1) - 1
        if _holders[key] <= 0:
            hou.hda.uninstallFile(path)
            _holders.pop(key, None)


def install_all_assets():
    """Hold every generated asset of ``houdini/otls`` for the rest of the run (never released)."""
    names = sorted(name for name in os.listdir(OTLS_DIR) if name.endswith(".hda"))
    install_hdas_for_class([os.path.join(OTLS_DIR, name) for name in names])


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

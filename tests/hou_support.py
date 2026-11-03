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

# A new P3D Export node starts from the active preset in the user's settings file (D26). Point the tests at a file
# that does not exist, so the owner's own settings never change what a test sees. A test that wants a file sets it.
if "A3OB_SETTINGS" not in os.environ:
    os.environ["A3OB_SETTINGS"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no_such_settings.json")

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


def _inside_checkout(path):
    root = _key(REPO_ROOT)
    return _key(path).startswith(root + os.sep)


def prefer_checkout_definitions(path):
    """Make every definition of an installed file the preferred one, then check the node types use this checkout.

    The owner's Houdini package can load other copies of the same HDAs (for example from the main checkout) before
    the suite starts. Without this, a run on a worktree could test those copies. Fails at once if it did.
    """
    for definition in hou.hda.definitionsInFile(path):
        definition.setIsPreferred(True)
        node_type = hou.nodeType(definition.nodeTypeCategory(), definition.nodeTypeName())
        used = node_type.definition().libraryFilePath() if node_type is not None and node_type.definition() else None
        if used is None or _key(used) != _key(path):
            raise RuntimeError(
                "Node type %s resolves to %s, not to this checkout's %s. Another HDA library (often a Houdini "
                "package) is shadowing it. Run clean: set HOUDINI_PACKAGE_SKIP=1 (skips all packages) or "
                "HOUDINI_USER_PREF_DIR to an empty folder before starting hython."
                % (definition.nodeTypeName(), used, path))
        if not _inside_checkout(used):
            raise RuntimeError("Node type %s uses %s, which is outside the checkout %s."
                               % (definition.nodeTypeName(), used, REPO_ROOT))


def install_hdas_for_class(paths):
    """Hold HDA files for a test class (installing those not yet loaded). Pass the result to ``uninstall_hdas``."""
    held = []
    for path in paths:
        path = path.replace("\\", "/")
        key = _key(path)
        if not _holders.get(key):
            hou.hda.installFile(path)
            prefer_checkout_definitions(path)
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

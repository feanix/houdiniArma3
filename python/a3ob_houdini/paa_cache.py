# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB). New file, not derived from A3OB.
"""Decoded .paa pictures for the viewport (task #53, D29).

``cached_image(paa_path)`` returns the path of a PNG made from the .paa. Houdini cannot read .paa, so the picture is
decoded once (``a3ob_core.paa``) and written to the cache folder. The file name holds a hash of the source path, size and
modification time, so an unchanged .paa is never decoded twice and a changed one is decoded again.

The cache folder: the ``cache_dir`` argument, else the ``paa_cache_dir`` setting, else ``$HOUDINI_TEMP_DIR/a3ob_paa_cache``
(the system temp folder when Houdini's variable is not known). Old pictures of a changed .paa stay in the folder; it is a
cache, so the artist may delete it at any time.

``cached_image`` raises ``a3ob_core.paa.PaaError`` when the .paa cannot be read or decoded.
"""

import hashlib
import os
import tempfile
from typing import Optional, Tuple

from a3ob_core import paa, png

from . import settings as settings_module

__all__ = ["FOLDER_NAME", "default_cache_dir", "cache_dir", "cache_name", "cached_image"]

FOLDER_NAME = "a3ob_paa_cache"


def default_cache_dir() -> str:
    """``$HOUDINI_TEMP_DIR/a3ob_paa_cache``; the system temp folder replaces the variable when it is not set."""
    base = os.environ.get("HOUDINI_TEMP_DIR", "").strip()
    if not base:
        try:
            import hou
            base = hou.text.expandString("$HOUDINI_TEMP_DIR")
        except Exception:  # no hou, or a Houdini without that variable
            base = ""
    if not base or "$" in base:
        base = tempfile.gettempdir()
    return os.path.join(base, FOLDER_NAME)


def cache_dir(configured: Optional[str] = None) -> str:
    """The folder in use: ``configured``, else the setting, else the default."""
    if configured is None:
        configured = settings_module.load()[0].paa_cache_dir
    configured = (configured or "").strip()
    return os.path.abspath(os.path.expandvars(os.path.expanduser(configured))) if configured else default_cache_dir()


def _identity(source: str) -> Tuple[str, int, int]:
    stat = os.stat(source)
    return os.path.normcase(os.path.abspath(source)), stat.st_size, stat.st_mtime_ns


def cache_name(source: str) -> str:
    """The PNG file name for a .paa: its stem and a hash of path, size and modification time."""
    path, size, mtime = _identity(source)
    digest = hashlib.sha1(("%s|%d|%d" % (path, size, mtime)).encode("utf-8")).hexdigest()[:16]
    stem = os.path.splitext(os.path.basename(source))[0]
    return "%s_%s.png" % (stem, digest)


def cached_image(source: str, cache_folder: Optional[str] = None) -> str:
    """The path of a PNG for the .paa ``source``; decodes and writes it only when it is not in the cache yet."""
    try:
        name = cache_name(source)
    except OSError as ex:
        raise paa.PaaError("%s cannot be read (%s)" % (source, ex.strerror or ex)) from None
    folder = cache_dir(cache_folder)
    target = os.path.join(folder, name)
    if os.path.isfile(target):
        return target
    width, height, rgba = paa.decode_paa(source)
    try:
        os.makedirs(folder, exist_ok=True)
        png.write_png(target, width, height, rgba)
    except OSError as ex:
        raise paa.PaaError("the picture cache %s cannot be written (%s)" % (folder, ex.strerror or ex)) from None
    return target

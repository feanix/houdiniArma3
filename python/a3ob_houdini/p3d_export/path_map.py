# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Task #51: the game path of a texture, material or proxy file when Relative Paths is on. No ``hou`` here.

One ``PathMapper`` lives for one export. P3D Export, Quick Export and Batch folder convert all reach it through
``model_build.build_model``, so the rule is the same in all three. First match wins:

(a) Walk up from the file's folder. The first folder that holds a ``$PBOPREFIX$`` file gives the prefix, and the rest of
    the path is appended. The file holds one line with the prefix, for example ``hyck\\ame\\addons\\props``. Some tools
    write a ``prefix=hyck\\ame\\addons\\props`` line instead, and that form is read too.
(b) Else a HEMTT project folder (one that holds ``.hemtt/project.toml``) above the file, with the file under
    ``addons/<name>/``. HEMTT builds that addon with the prefix ``mainprefix\\prefix\\addons\\<name>``.
(c) Else the old rule: strip the project root, or else only the drive letter (this one gets a warning).

Sources checked on 2026-11-03 (task #51):
    HEMTT book, "Minimum Configuration" (github.com/BrettMayson/HEMTT, book/configuration/minimum.md): the addon's
    ``$PBOPREFIX$`` is one line ``z\\{prefix}\\addons\\main``; ``z`` there is the main prefix.
    HEMTT book, "Addon Configuration" (book/configuration/addon.md): ``ignore_pboprefix`` exists "for a mismatched
    $PBOPREFIX$", so HEMTT compares the file with the prefix it builds from ``project.toml`` (``mainprefix``,
    ``prefix``) and the addon folder name. That is rule (b).
    The ``prefix=`` key form: Mikero's tools and Addon Builder write PBO header properties as ``key=value`` lines; HEMTT
    writes the plain line. Both are accepted, the plain line is the one documented.
    Owner layout read-only: E:/Projects/HettycKad (mainprefix hyck, prefix ame, addons/props/$PBOPREFIX$).

A path without a drive letter is not touched here. Lowercase is applied by the caller afterwards.
"""

import ntpath
import os
import re
import tomllib
from typing import Dict, List, Optional, Tuple

from .. import messages

__all__ = ["PathMapper", "read_prefix_file"]

PREFIX_FILE = "$PBOPREFIX$"
_DRIVE = re.compile(r"^[A-Za-z]:")
_KEY_FORM = re.compile(r"^prefix\s*=\s*(.*)$", re.IGNORECASE)
KINDS = ("texture", "material", "proxy")
_PLURAL = {"texture": "textures", "material": "materials", "proxy": "proxies"}

# A found prefix: (folder that holds the source, prefix, text that names the source in the info line).
_Found = Optional[Tuple[str, str, str]]


def _clean_prefix(text: str) -> str:
    return text.strip().strip("\"'").replace("/", "\\").strip("\\ ")


def read_prefix_file(path: str) -> str:
    """The prefix in a ``$PBOPREFIX$`` file: its first non-empty line (a ``prefix=`` key form is read), with
    backslashes and no leading or trailing backslash. Empty when the file cannot be read or has no text."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
    except OSError:
        return ""
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _KEY_FORM.match(line)
        if match:
            line = match.group(1).strip().rstrip(";")
        return _clean_prefix(line)
    return ""


def _hemtt_values(toml_path: str) -> Optional[Tuple[str, str]]:
    """``(mainprefix, prefix)`` of a HEMTT ``project.toml``; None if it has no prefix or cannot be read."""
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    prefix = _clean_prefix(str(data.get("prefix", "")))
    if not prefix:
        return None
    return _clean_prefix(str(data.get("mainprefix", ""))), prefix


def _key(folder: str) -> str:
    return ntpath.normcase(folder)


def _strip_root(path: str, root: str) -> Optional[str]:
    root = root.replace("/", "\\").strip()
    if not root:
        return None
    prefix = root.rstrip("\\") + "\\"
    if path.casefold().startswith(prefix.casefold()):
        return path[len(prefix):]
    return None


def _addons_label(folder: str) -> str:
    """The folder from its last ``addons`` part on (``addons\\props``), for the info line."""
    parts = folder.split("\\")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() == "addons":
            return "\\".join(parts[index:])
    return folder


class PathMapper:
    """Maps absolute paths to game paths for one export and remembers the answers per folder."""

    def __init__(self, project_root: str = "", enabled: bool = True) -> None:
        self.project_root = project_root
        self.enabled = enabled
        self._prefix_cache: Dict[str, _Found] = {}
        self._hemtt_cache: Dict[str, Optional[Tuple[str, str, str]]] = {}
        self._mapped: Dict[str, Dict[str, set]] = {}  # source label -> kind -> distinct input paths
        self._source_prefix: Dict[str, str] = {}
        self._warned: set = set()
        self.warnings: List[str] = []

    # -- lookups (cached per folder) ---------------------------------------------------------------------------------

    def _prefix_for(self, folder: str) -> _Found:
        """Rule (a): the nearest ``$PBOPREFIX$`` at or above ``folder``."""
        key = _key(folder)
        if key in self._prefix_cache:
            return self._prefix_cache[key]
        found: _Found = None
        candidate = os.path.join(folder, PREFIX_FILE)
        if os.path.isfile(candidate):
            prefix = read_prefix_file(candidate)
            if prefix:
                found = (folder, prefix, "%s\\%s" % (_addons_label(folder), PREFIX_FILE))
        if found is None:
            parent = ntpath.dirname(folder)
            if parent and parent != folder:
                found = self._prefix_for(parent)
        self._prefix_cache[key] = found
        return found

    def _project_for(self, folder: str) -> Optional[Tuple[str, str, str]]:
        """The nearest HEMTT project at or above ``folder``: (project folder, mainprefix, prefix)."""
        key = _key(folder)
        if key in self._hemtt_cache:
            return self._hemtt_cache[key]
        found = None
        toml_path = os.path.join(folder, ".hemtt", "project.toml")
        if os.path.isfile(toml_path):
            values = _hemtt_values(toml_path)
            if values is not None:
                found = (folder, values[0], values[1])
        if found is None:
            parent = ntpath.dirname(folder)
            if parent and parent != folder:
                found = self._project_for(parent)
        self._hemtt_cache[key] = found
        return found

    # -- the mapping -------------------------------------------------------------------------------------------------

    def map_path(self, path: str, kind: str, label: Optional[str] = None) -> str:
        """The game path for ``path``. A path without a drive letter comes back as typed."""
        if not path or not self.enabled:
            return path
        if not _DRIVE.match(path):
            return path.lstrip("\\") if path.startswith("\\") else path
        path = path.replace("/", "\\")
        if kind == "proxy" and path.lower().endswith(".p3d"):
            path = path[:-4]
        folder = ntpath.dirname(path)
        found = self._prefix_for(folder)
        if found is not None:
            base, prefix, source = found
            return self._done(path, kind, source, prefix, prefix + "\\" + path[len(base):].lstrip("\\"))
        project = self._project_for(folder)
        if project is not None:
            root, main, prefix = project
            rest = path[len(root):].lstrip("\\").split("\\")
            if len(rest) >= 3 and rest[0].lower() == "addons":
                full = "\\".join(part for part in (main, prefix, "addons", rest[1]) if part)
                source = ".hemtt\\project.toml of %s" % (ntpath.basename(root) or root)
                return self._done(path, kind, source, full, full + "\\" + "\\".join(rest[2:]))
        stripped = _strip_root(path, self.project_root)
        if stripped is not None:
            return stripped
        self._warn(path, kind, label)
        return _DRIVE.sub("", path).lstrip("\\")

    def _done(self, path: str, kind: str, source: str, prefix: str, result: str) -> str:
        self._mapped.setdefault(source, {}).setdefault(kind, set()).add(path.casefold())
        self._source_prefix[source] = prefix
        return result

    def _warn(self, path: str, kind: str, label: Optional[str]) -> None:
        key = path.casefold()
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(messages.format_problem(
            label, "the %s path %s matches no $PBOPREFIX$ file, no HEMTT addon folder and not the project root, so only "
            "the drive letter was removed" % (kind, path),
            "Put the file in an addon folder that has a $PBOPREFIX$ file, or set the project root so it holds the file"))

    def map_proxy_name(self, name: str, label: Optional[str] = None) -> str:
        """``proxy:<path>.<id>`` with the path mapped; any other name comes back unchanged."""
        match = _PROXY_PARTS.fullmatch(name)
        if match is None:
            return name
        return "proxy:%s.%s" % (self.map_path(match.group(1), "proxy", label), match.group(2))

    def flush_warnings(self, report: messages.Report) -> None:
        """Hand the warnings to ``report`` (once each) and forget them."""
        for text in self.warnings:
            report.warning(text)
        self.warnings = []

    def info_line(self) -> Optional[str]:
        """``Paths mapped with <source> (<prefix>): 3 textures, 2 materials.`` for all sources, or None."""
        parts = []
        for source, by_kind in self._mapped.items():
            counts = []
            for kind in KINDS:
                count = len(by_kind.get(kind, ()))
                if count:
                    counts.append("%d %s" % (count, kind if count == 1 else _PLURAL[kind]))
            parts.append("%s (%s): %s" % (source, self._source_prefix[source], ", ".join(counts)))
        if not parts:
            return None
        return "Paths mapped with %s." % "; ".join(parts)

    def report_to(self, report: messages.Report) -> None:
        """At the end of an export: the warnings and the one info line."""
        self.flush_warnings(report)
        line = self.info_line()
        if line is not None:
            report.info(line)


_PROXY_PARTS = re.compile(r"proxy:(.*)\.(\d+)", re.IGNORECASE | re.DOTALL)

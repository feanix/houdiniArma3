# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB). Written for this port (task #88); not derived from A3OB.
"""Where an animation comes from, with no Houdini (character-rigging.md, Phase 4b, task #88).

RTM Import takes three kinds of source, all turned into the same ``rtm.Rtm`` that the plain reader gives, so the pose path
(``rtm_pose.build_clip`` and the rest) is shared and not copied:

- a plain RTM file;
- a binarised RTM (BMTR) file: the frames are relative to the parent bone, so the skeleton's parents are needed to make
  the absolute matrices (``bmtr.BMTRFile.absolute_matrices``);
- an entry inside a PBO, written ``<path to the pbo>|<entry name>`` (``|`` cannot be part of a Windows file name).

The game's animations are in ``anims_f*.pbo`` next to the game's other addons: ``<Arma 3>/Addons`` and the ``Addons`` folder of
each expansion (``Expansion``, ``Enoch``, ``Jets``, ``Mark``, ``AoW``, ``Kart``, ``Contact``, ...).
"""

import dataclasses
import fnmatch
import glob
import os
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .. import bmtr, pbo
from . import rtm as rtm_file

__all__ = ["SEPARATOR", "AnimRef", "AnimSourceError", "split_source", "join_source", "bmtr_to_rtm", "read_animation",
           "find_anim_pbos", "list_animations", "read_source", "compare_rtm", "filter_refs", "ANIM_PBO_PATTERN"]

#: Between the PBO path and the entry name in a source text.
SEPARATOR = "|"
ANIM_PBO_PATTERN = "anims_f*.pbo"


class AnimSourceError(Exception):
    """The animation cannot be read; the text says why in plain words."""


@dataclasses.dataclass
class AnimRef:
    """One RTM inside an anims PBO."""
    #: The PBO path relative to the game folder, forward slashes (``Addons/anims_f_data.pbo``).
    pbo: str
    #: The entry name as the PBO spells it (backslashes).
    entry: str
    size: int

    @property
    def name(self) -> str:
        """The file name without the folder and the ``.rtm`` ending."""
        base = self.entry.replace("/", "\\").rsplit("\\", 1)[-1]
        return base[:-4] if base.lower().endswith(".rtm") else base

    @property
    def pbo_name(self) -> str:
        return os.path.basename(self.pbo)[:-4]

    @property
    def source(self) -> str:
        """The source text relative to the game folder (``Addons/anims_f_data.pbo|Anim/.../X.rtm``)."""
        return join_source(self.pbo, self.entry)


def join_source(pbo_path: str, entry: str) -> str:
    return pbo_path.replace("\\", "/") + SEPARATOR + entry.replace("\\", "/")  # ``PBO.find`` takes either slash


def split_source(text: str) -> Tuple[str, Optional[str]]:
    """``(path, entry)`` of a source text; ``entry`` is None for a plain file path."""
    text = text.strip()
    if SEPARATOR in text:
        path, entry = text.split(SEPARATOR, 1)
        return path.strip(), entry.strip()
    return text, None


def bmtr_to_rtm(data: bmtr.BMTRFile, parents: Optional[Mapping[str, str]] = None, source: str = "") -> rtm_file.Rtm:
    """The same pose data the plain reader gives: absolute matrices in A3OB's layout (``parents``: bone name to parent
    name, parents before children; a bone it does not name keeps its relative matrix)."""
    matrices = data.absolute_matrices(parents or {})
    out = rtm_file.Rtm(bones=list(data.bones), motion=tuple(float(v) for v in data.motion),
                       props=list(data.props), source=source)
    for f in range(data.frame_count):
        frame = rtm_file.Frame(float(data.phases[f]))
        for b, name in enumerate(data.bones):
            frame.transforms.append(rtm_file.Transform(name, matrices[f, b].tolist()))
        out.frames.append(frame)
    return out


def read_animation(data: bytes, parents: Optional[Mapping[str, str]] = None, source: str = "") -> rtm_file.Rtm:
    """A plain RTM or a BMTR from bytes. Raises ``AnimSourceError``."""
    try:
        if bytes(data[:4]) == b"BMTR":
            return bmtr_to_rtm(bmtr.read_bmtr(data), parents, source)
        return rtm_file.read_rtm(data, source)
    except (rtm_file.RtmError, bmtr.BMTRError) as ex:
        raise AnimSourceError(str(ex)) from None


def find_anim_pbos(game_dir: str) -> List[str]:
    """The anims PBOs of a game folder: ``Addons/anims_f*.pbo`` and ``<expansion>/Addons/anims_f*.pbo``, as paths relative
    to the folder with forward slashes, sorted (the main game first)."""
    found = []
    for pattern in (os.path.join(game_dir, "Addons", ANIM_PBO_PATTERN), os.path.join(game_dir, "*", "Addons", ANIM_PBO_PATTERN)):
        for path in glob.glob(pattern):
            found.append(os.path.relpath(path, game_dir).replace("\\", "/"))
    return sorted(set(found), key=lambda p: (p.count("/"), p.lower()))


def list_animations(game_dir: str, problems: Optional[List[str]] = None) -> Tuple[List[AnimRef], Dict[str, int]]:
    """``(refs, counts)``: every ``.rtm`` entry of the anims PBOs, and the number found per PBO (a PBO with none is in
    ``counts`` with 0). A PBO that cannot be read is skipped and named in ``problems``. Only the headers are read."""
    refs: List[AnimRef] = []
    counts: Dict[str, int] = {}
    for rel in find_anim_pbos(game_dir):
        try:
            with pbo.PBO(os.path.join(game_dir, rel)) as archive:
                found = [AnimRef(rel, e.name, e.original_size or e.data_size) for e in archive.entries if e.name.lower().endswith(".rtm")]
        except (pbo.PBOError, OSError) as ex:
            if problems is not None:
                problems.append("%s cannot be read: %s" % (rel, ex))
            continue
        counts[rel] = len(found)
        refs.extend(found)
    return refs, counts


def filter_refs(refs: Sequence[AnimRef], text: str) -> List[AnimRef]:
    """The refs whose entry name holds every word of ``text`` (case-insensitive), or matches a ``*`` pattern."""
    words = text.lower().split()
    if not words:
        return list(refs)
    out = []
    for ref in refs:
        haystack = (ref.entry + " " + ref.pbo_name).lower()
        if all(fnmatch.fnmatchcase(haystack, "*" + w + "*") if "*" in w or "?" in w else w in haystack for w in words):
            out.append(ref)
    return out


def read_source(text: str, game_dir: str = "", parents: Optional[Mapping[str, str]] = None) -> rtm_file.Rtm:
    """Read the animation a source text names. A PBO path may be relative to ``game_dir``. Raises ``AnimSourceError``."""
    path, entry = split_source(text)
    if entry is None:
        if path.lower().endswith((".pbo", ".ebo")):
            raise AnimSourceError("%s is a PBO: add |<entry name> after it, or pick the animation in the game list"
                                  % os.path.basename(path))
        try:
            with open(path, "rb") as file:
                data = file.read()
        except OSError as ex:
            raise AnimSourceError("%s cannot be opened: %s" % (path, ex.strerror or ex)) from None
        return read_animation(data, parents, path)
    full = path if os.path.isabs(path) or not game_dir else os.path.join(game_dir, path)
    try:
        with pbo.PBO(full) as archive:
            found = archive.find(entry)
            if found is None:
                raise AnimSourceError("%s has no entry %s" % (os.path.basename(full), entry))
            data = archive.read(found)
    except pbo.PBOError as ex:
        raise AnimSourceError(str(ex)) from None
    except OSError as ex:
        raise AnimSourceError("%s cannot be opened: %s" % (full, ex.strerror or ex)) from None
    return read_animation(data, parents, full + SEPARATOR + entry)


def compare_rtm(a: rtm_file.Rtm, b: rtm_file.Rtm, joints: Optional[Sequence[str]] = None) -> Dict[str, object]:
    """How two animations differ, bone by bone, for the cross-check of a plain RTM against its binarised twin.

    Bones are matched by name ignoring case; frames are matched by number when the counts are equal, else by the nearest
    phase. Returns a dict: ``frames`` (a, b), ``only_a`` / ``only_b`` (bone names), ``phase`` (largest phase difference of
    matched frames), ``motion`` (largest difference), and per common bone ``bones[name] = (rotation, translation)``: the
    largest difference over the frames, the rotation as the biggest element of ``Ra - Rb`` (unit matrices), the
    translation as the distance between the positions in metres. ``joints`` limits the common bones that are measured."""
    import numpy as np
    names_a = {n.lower(): i for i, n in enumerate(a.bones)}
    names_b = {n.lower(): i for i, n in enumerate(b.bones)}
    common = [n for n in names_a if n in names_b]
    if joints is not None:
        wanted = {j.lower() for j in joints}
        common = [n for n in common if n in wanted]
    phases_a = np.array([f.phase for f in a.frames])
    phases_b = np.array([f.phase for f in b.frames])
    if len(phases_a) == len(phases_b):
        pairs = list(range(len(phases_a)))
    else:
        pairs = [int(np.abs(phases_b - p).argmin()) for p in phases_a]
    result: Dict[str, object] = {
        "frames": (len(a.frames), len(b.frames)),
        "only_a": [n for n in a.bones if n.lower() not in names_b],
        "only_b": [n for n in b.bones if n.lower() not in names_a],
        "phase": float(max((abs(phases_a[i] - phases_b[j]) for i, j in enumerate(pairs)), default=0.0)),
        "motion": float(max(abs(x - y) for x, y in zip(a.motion, b.motion))),
        "bones": {},
    }
    for name in common:
        rotation = translation = 0.0
        for i, j in enumerate(pairs):
            ma = np.array(a.frames[i].transforms[names_a[name]].matrix)
            mb = np.array(b.frames[j].transforms[names_b[name]].matrix)
            rotation = max(rotation, float(np.abs(ma[:3, :3] - mb[:3, :3]).max()))
            translation = max(translation, float(np.linalg.norm(ma[:3, 3] - mb[:3, 3])))
        result["bones"][name] = (rotation, translation)
    return result

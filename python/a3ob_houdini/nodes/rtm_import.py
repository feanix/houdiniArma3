# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# The RTM import follows A3OB io/import_rtm.py in what a matrix means; the maths is ``a3ob_core.rig.rtm_pose``.
"""The RTM Import node (docs/design/character-rigging.md Phase 4, task #87): the Arma skeleton posed by a plain RTM
animation, so the timeline plays the animation and Bone Deform skins a rigged mesh with it.

Input 1 is the Arma rest skeleton (Rig Skeleton). The output is the same skeleton (points, names, bone lines) with ``P`` and
``transform`` of the animated joints. The RTM matrices are absolute and do not depend on the joints' own axes, so they are
composed with the rest frames of the skeleton (``rtm_pose.pose_world``): the skinned mesh is the same with any rest frames.
"""

import os
import time

import numpy as np

from a3ob_core.rig import anim_source
from a3ob_core.rig import rtm as rtm_file
from a3ob_core.rig import rtm_pose

from .. import geo_edit, rig_weights
from .. import settings as settings_module
from ..errors import UserError
from ..messages import Report, format_problem, list_elements
from ..params import NodeSpec, Parm, read_parms

try:
    import hou
except ImportError:  # the parameter table is also read under plain Python
    hou = None

__all__ = ["SPEC", "cook", "BUTTONS", "MENUS", "read_rtm_cached", "read_animation_cached", "game_folder", "MENU_LIMIT"]

NAME = "RTM Import"
#: Detail attributes: the frame in the cycle (1-based, a fraction between key frames), the phase 0 to 1, the cycle number.
FRAME_ATTRIB = "a3ob_rtm_frame"
PHASE_ATTRIB = "a3ob_rtm_phase"
CYCLE_ATTRIB = "a3ob_rtm_cycle"
#: Frames per cycle when the artist has not chosen (RTM files carry phases only; the speed lives in the config).
DEFAULT_FRAMES = 24
DEFAULT_SECONDS = 1.0
_CACHE = {}
#: Entries kept by ``read_animation_cached`` (a long game animation is 40 frames of 70 bones: a few MB as Python lists).
_CACHE_MAX = 12
_LISTING = {}
#: The Animation menu shows at most this many entries (a menu of thousands is slow); the Filter narrows the list.
MENU_LIMIT = 300

SPEC = NodeSpec(
    kind="rtm_import",
    label="RTM Import",
    summary="Poses the Arma skeleton with an Arma RTM animation file. The timeline plays the animation; Bone Deform "
            "skins a rigged mesh with it.",
    inputs=(1, 1),
    input_labels=("Arma skeleton",),
    input_help=("The Arma rest skeleton from Rig Skeleton. Its joints are posed; the points, names and bone lines stay.",),
    parms=(
        Parm("source", "Animation From", "menu", "file", "Where the animation comes from.", items=(
            ("file", "File"),
            ("game", "Arma 3 game"),
        ), item_help=(
            ("file", "A plain .rtm file, a binarised one (BMTR, as in the game's PBO files), or an entry inside a PBO "
                     "written as path/to/anims_f.pbo|a3/anims_f/data/anim/sdr/mov/erc/wlk/rfl/amovpercmwlksraswrfldf.rtm."),
            ("game", "One of the animations in the game's anims PBO files (anims_f_data.pbo and the expansions' own). Pick "
                     "it in Animation; Filter narrows the list."),
        )),
        Parm("file", "RTM File", "file", "",
             "A plain Arma animation (.rtm), a binarised one (BMTR, read with the parents of the skeleton), or an entry inside "
             "a PBO written as path/to/anims_f.pbo|a3/anims_f/data/anim/sdr/mov/erc/wlk/rfl/amovpercmwlksraswrfldf.rtm. Bones the skeleton lacks (Weapon, launcher, face and slot "
             "bones, Hips) are ignored; joints the file lacks stay at rest.",
             file_pattern="*.rtm *.pbo", show_when=(("source", ("file",)),)),
        Parm("arma3", "Arma 3 Folder", "dir", "", "The Arma 3 game folder (the one that holds Addons). Empty uses the "
             "environment variable A3OB_ARMA3, then the Arma 3 Folder stored in the A3OB settings file, then the Arma 3 "
             "folder of your Steam library.", show_when=(("source", ("game",)),)),
        Parm("filter", "Filter", "string", "", "Words that the animation name must contain (any case); a word may have * or ?. "
             "Example: wlk ras rfl finds the rifle walks of the standing man. The Animation menu then lists only those.",
             show_when=(("source", ("game",)),)),
        Parm("anim", "Animation", "menu", "", "The animation from the game's PBO files. The menu lists the animations that "
             "match Filter, %d at most, with the PBO each one is in." % MENU_LIMIT, menu_action="anims",
             show_when=(("source", ("game",)),)),
        Parm("playback", "Playback", "menu", "frames", "How the length of one cycle is given. An RTM file holds phases "
             "(0 to 1) only; how fast it plays in the game is set in the config, so you choose it here.", items=(
                 ("frames", "Frames per cycle"),
                 ("seconds", "Seconds per cycle"),
             ), item_help=(
                 ("frames", "One cycle takes the number of timeline frames in Frames per Cycle."),
                 ("seconds", "One cycle takes Seconds per Cycle, whatever the frame rate of the scene."),
             )),
        Parm("cycleframes", "Frames per Cycle", "int", DEFAULT_FRAMES, "How many timeline frames one cycle of the animation "
             "takes. The default is %d, one second at 24 frames per second." % DEFAULT_FRAMES, minimum=1,
             show_when=(("playback", ("frames",)),)),
        Parm("cycleseconds", "Seconds per Cycle", "float", DEFAULT_SECONDS, "How many seconds one cycle takes. The default is "
             "%g." % DEFAULT_SECONDS, minimum=0.01, show_when=(("playback", ("seconds",)),)),
        Parm("startframe", "Start Frame", "float", 1.0, "The timeline frame where the cycle starts (phase 0). Playback "
             "before it holds the first frame, or runs the cycle backwards when Loop is on."),
        Parm("wrap", "At the End", "menu", "loop", "What happens after the last frame of the cycle.", items=(
            ("loop", "Loop"),
            ("hold", "Hold"),
        ), item_help=(
            ("loop", "The cycle starts again. With Apply Displacement on, the character keeps walking on."),
            ("hold", "The last frame stays, and the first frame before the start."),
        )),
        Parm("displace", "Apply Displacement", "toggle", False, "Move the skeleton by the displacement of the animation (how "
             "far the character walks in one cycle, in the file) times the cycles played. Off keeps the character in place."),
    ),
)

_STATUS = ("Frame %.2f of %d in the cycle (phase %.3f, cycle %d).")


def read_rtm_cached(path: str):
    """The RTM at ``path``, read once per file change."""
    stamp = os.stat(path)
    key = (path, stamp.st_mtime_ns, stamp.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        rtm = rtm_file.read_rtm_file(path)
    except rtm_file.RtmError as ex:
        raise UserError(format_problem(NAME, "%s cannot be read: %s" % (os.path.basename(path), ex),
                                       "Pick a plain .rtm file")) from None
    _CACHE[path] = (key, rtm)
    return rtm


def _cycle(values) -> float:
    """The length of one cycle in timeline frames (the scene frame rate decides when it is given in seconds)."""
    if values["playback"] == "seconds":
        return max(values["cycleseconds"] * hou.fps(), 1e-6)
    return float(max(values["cycleframes"], 1))


def game_folder(override: str = "") -> str:
    """The Arma 3 game folder in use (the parameter, the environment, the stored setting, then Steam), or ''."""
    settings, _ = settings_module.load()
    return settings_module.effective_arma3(settings, override)


def _need_game(override: str) -> str:
    folder = game_folder(override)
    if not folder or not os.path.isdir(os.path.join(folder, "Addons")):
        raise UserError(format_problem(NAME, "the Arma 3 folder %s" % (
            "%s has no Addons folder" % folder if folder else "is not known"),
            "Set Arma 3 Folder, or the environment variable A3OB_ARMA3, to the folder that holds Addons"))
    return folder


def _stamp(path: str):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def read_animation_cached(text: str, game_dir: str, names, parents):
    """``(Rtm, seconds)`` of the animation a source text names (a plain RTM or BMTR file, or ``pbo|entry``), read once per
    file change. A binarised file needs the skeleton (``names`` and ``parents``: a joint's parent number, -1 for a root)."""
    path, entry = anim_source.split_source(text)
    if entry is not None and not os.path.isabs(path):
        path = os.path.join(game_dir, path)
    skeleton_key = (tuple(names), tuple(parents))
    key = (text, path, _stamp(path), skeleton_key)
    cached = _CACHE.get(text)
    if cached is not None and cached[0] == key:
        return cached[1], 0.0
    mapping = {name: (names[p] if p >= 0 else "") for name, p in zip(names, parents)}
    started = time.perf_counter()
    try:
        data = anim_source.read_source(text, game_dir, mapping)
    except anim_source.AnimSourceError as ex:
        raise UserError(format_problem(NAME, "%s cannot be read: %s" % (os.path.basename(path), ex),
                                       "Pick a plain .rtm, a BMTR file, or an entry of an anims PBO")) from None
    seconds = time.perf_counter() - started
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[text] = (key, data)
    return data, seconds


def _listing(game_dir: str):
    """``(refs, counts)`` of the anims PBOs, listed again when a PBO changed."""
    stamps = tuple((rel, _stamp(os.path.join(game_dir, rel))) for rel in anim_source.find_anim_pbos(game_dir))
    cached = _LISTING.get(game_dir)
    if cached is not None and cached[0] == stamps:
        return cached[1], cached[2]
    refs, counts = anim_source.list_animations(game_dir)
    _LISTING[game_dir] = (stamps, refs, counts)
    return refs, counts


def _menu_anims(kwargs) -> list:
    """The Animation menu: the RTMs of the anims PBOs that match Filter, up to ``MENU_LIMIT``. The chosen one stays on top."""
    node = kwargs["node"]
    folder = game_folder(node.evalParm("arma3"))
    if not folder or not os.path.isdir(os.path.join(folder, "Addons")):
        return [("", "(set Arma 3 Folder)")]
    refs, _ = _listing(folder)
    found = anim_source.filter_refs(refs, node.evalParm("filter"))
    items = [(ref.source, "%s  (%s)" % (ref.name, ref.pbo_name)) for ref in found[:MENU_LIMIT]]
    if len(found) > MENU_LIMIT:
        items.append(("", "... %d more: type words in Filter" % (len(found) - MENU_LIMIT)))
    elif not found:
        items.append(("", "(no animation matches the filter)"))
    current = node.evalParm("anim")
    if current and all(token != current for token, _ in items):
        name = current.replace("\\", "/").rsplit("/", 1)[-1]
        items.insert(0, (current, name[:-4] if name.lower().endswith(".rtm") else name))
    return items


def cook(node, report: Report) -> None:
    values = read_parms(node, SPEC)
    skeleton = rig_weights.read_skeleton(NAME, node.inputGeometry(0))
    game_dir = ""
    if values["source"] == "game":
        game_dir = _need_game(values["arma3"])
        text = values["anim"].strip()
        if not text:
            raise UserError(format_problem(NAME, "no game animation is chosen", "Pick one in Animation (Filter narrows the list)"))
    else:
        text = values["file"].strip()
        if not text:
            raise UserError(format_problem(NAME, "no RTM file is chosen", "Pick an .rtm file in RTM File"))
        path, entry = anim_source.split_source(text)
        if entry is None and not os.path.isfile(path):
            raise UserError(format_problem(NAME, "the file %s does not exist" % path, "Check RTM File"))
        game_dir = game_folder(values["arma3"]) if entry is not None and not os.path.isabs(path) else ""
        if entry is not None and not os.path.isfile(path if os.path.isabs(path) else os.path.join(game_dir, path)):
            raise UserError(format_problem(NAME, "the PBO %s does not exist" % path, "Check RTM File"))
    rtm, seconds = read_animation_cached(text, game_dir, skeleton.names, skeleton.parents)
    positions = np.array(skeleton.positions, dtype=np.float64).reshape(-1, 3)
    clip = rtm_pose.build_clip(rtm, skeleton.names, skeleton.parents, positions)
    length = _cycle(values)
    position = (hou.frame() - values["startframe"]) / length
    cycle, phase = rtm_pose.cycle_position(position, values["wrap"] == "loop")
    skin = rtm_pose.sample(clip, phase)
    shift = clip.motion * (cycle + phase) if values["displace"] else None
    rotations = None
    if all(r is not None for r in skeleton.rotations):
        rotations = np.array(skeleton.rotations, dtype=np.float64).reshape(-1, 3, 3)
    posed, transforms = rtm_pose.pose_world(positions, rotations, skin, shift)
    geo = node.geometry()  # a Python-type SOP starts with a copy of its first input
    geo.setPointFloatAttribValues("P", [float(v) for v in posed.reshape(-1)])
    if geo.findPointAttrib("transform") is not None:
        geo.setPointFloatAttribValues("transform", [float(v) for v in transforms.reshape(-1)])
    geo_edit.set_detail(geo, FRAME_ATTRIB, float(phase * length))
    geo_edit.set_detail(geo, PHASE_ATTRIB, float(phase))
    geo_edit.set_detail(geo, CYCLE_ATTRIB, int(cycle))
    _report(report, text.replace("\\", "/").rsplit("/", 1)[-1], rtm, clip, skeleton.names, phase, length, cycle, seconds)


def _report(report: Report, name: str, rtm, clip, joints, phase: float, length: float, cycle: int, seconds: float = 0.0) -> None:
    report.info("%s: %d frames, %d bones. " % (name, len(rtm.frames), len(rtm.bones)) + _STATUS % (
        phase * length, round(length), phase, cycle) + (" Read in %.0f ms." % (seconds * 1000) if seconds else ""))
    missing = [joints[i] for i, found in enumerate(clip.found) if not found]
    if missing:
        report.info("%s have no record in the file and stay at rest." % list_elements("Joints", missing))
    if clip.extra:
        report.info("%s of the file are not joints of the skeleton and are ignored. Weapon and launcher carry the weapon "
                    "model in the game; the rig has no joint for them." % list_elements("Bones", clip.extra))
    if rtm.long_names:
        report.warning(format_problem(NAME, "%s fill the whole 32-byte name field (over 31 characters), which the game cannot "
                                      "match" % list_elements("bone names", rtm.long_names)))
    for text in rtm.problems():
        report.warning(format_problem(NAME, text))


BUTTONS = {}
MENUS = {"anims": _menu_anims}

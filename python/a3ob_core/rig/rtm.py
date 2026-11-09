# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_rtm.py (RTM_Transform, RTM_Frame, RTM_MDAT, RTM_0101, RTM_File; v2.5.1, 9f2b8bc).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original:
# - the reader works on bytes in memory, every read is bounds-checked (``BinaryReader``) and a short or garbled file raises
#   ``RtmError`` instead of ``struct.error``;
# - a 32-byte bone name field with no terminator (a name of 32 characters, which Arma cannot match) is read whole and listed in
#   ``Rtm.long_names`` instead of raising;
# - added ``Rtm.problems`` (header and frame checks) and the helpers ``write_rtm`` / ``read_rtm_file``;
# - BMTR (the binarised form) is not read here.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""The plain RTM animation file (``RTM_0101``) as plain Python data, with no Houdini and no numpy (character-rigging.md,
Phase 4, test 4.1).

Format (https://community.bistudio.com/wiki/Rtm_(Animation)_File_Format, as A3OB reads it):

- an optional ``RTM_MDAT`` block (events: a phase, a name and a value each), then the ``RTM_0101`` block;
- ``RTM_0101``: signature, 3 floats (the **motion** vector: how far the character moves in one cycle), the frame count and
  bone count (uint32 each), then one 32-byte zero-padded name per bone, then per frame a float **phase** (0 at the start
  of the cycle, 1 at the end) followed by one record per bone: the 32-byte name and 12 floats.

A matrix is **absolute**: it is the move of the bone from the rest pose of the model to the pose, in model space, so a
vertex weighted to one bone lands at ``matrix * vertex`` (the rest pose is the identity). ``Transform.matrix`` is the 4x4
that A3OB builds (the 12 floats with the Y and Z axes swapped, see ``_matrix_from_floats``), column-vector form
(``x' = M x``), in A3OB's (Blender) axes. ``pose.houdini_matrices`` turns it into Houdini's axes.

``Rtm.motion`` is stored as A3OB reads it (the file's three floats as ``x, z, y``).
"""

import dataclasses
import struct
from typing import List, Optional, Sequence, Tuple

from .. import binary

__all__ = ["RtmError", "Transform", "Frame", "Rtm", "read_rtm", "read_rtm_file", "write_rtm", "NAME_FIELD", "MAX_NAME"]

#: A bone name is a 32-byte field with a zero terminator: 31 characters at most.
NAME_FIELD = 32
MAX_NAME = NAME_FIELD - 1
_SIGNATURE = b"RTM_0101"
_MDAT = b"RTM_MDAT"


class RtmError(Exception):
    """The file is not a plain RTM, or it is cut short."""


def _matrix_from_floats(data: Sequence[float]) -> List[List[float]]:
    """The A3OB matrix of the 12 file floats (``RTM_Transform.read``): the file holds the three axes and the position,
    and the Y and Z rows and columns change places."""
    return [
        [data[0], data[6], data[3], data[9]],
        [data[2], data[8], data[5], data[11]],
        [data[1], data[7], data[4], data[10]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _floats_from_matrix(matrix: Sequence[Sequence[float]]) -> Tuple[float, ...]:
    """The 12 file floats of an A3OB matrix (``RTM_Transform.write``)."""
    m = matrix
    return (m[0][0], m[2][0], m[1][0], m[0][2], m[2][2], m[1][2], m[0][1], m[2][1], m[1][1], m[0][3], m[2][3], m[1][3])


@dataclasses.dataclass
class Transform:
    """One bone in one frame: its name as the frame record spells it, and the 4x4 matrix (list of 4 rows)."""
    bone: str = ""
    matrix: List[List[float]] = dataclasses.field(default_factory=lambda: [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0],
                                                                         [0, 0, 0, 1.0]])


@dataclasses.dataclass
class Frame:
    """A frame: its phase (0 .. 1 along the cycle) and one transform per bone, in the order of ``Rtm.bones``."""
    phase: float = 0.0
    transforms: List[Transform] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Rtm:
    """A plain RTM. ``bones`` are the names of the header, ``frames`` the frames, ``motion`` the displacement of one cycle
    (A3OB's reading of the three floats), ``props`` the events of an ``RTM_MDAT`` block as ``(phase, name, value)``.
    ``long_names`` are the bone names that fill their whole 32-byte field (32 characters or more: Arma cannot match them)."""
    bones: List[str] = dataclasses.field(default_factory=list)
    frames: List[Frame] = dataclasses.field(default_factory=list)
    motion: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    props: List[Tuple[float, str, str]] = dataclasses.field(default_factory=list)
    long_names: List[str] = dataclasses.field(default_factory=list)
    source: str = ""

    def problems(self) -> List[str]:
        """What is odd about the file, as plain sentences (empty when nothing is): frames without a record for every bone,
        records whose name is not the header's, a phase outside 0 .. 1 or not rising."""
        found: List[str] = []
        for number, frame in enumerate(self.frames):
            names = [t.bone for t in frame.transforms]
            if names != self.bones:
                found.append("frame %d names its bones differently from the header" % number)
                break
        phases = [frame.phase for frame in self.frames]
        if phases and (min(phases) < -1e-6 or max(phases) > 1.0 + 1e-6):
            found.append("a frame phase lies outside 0 to 1 (%.3f to %.3f)" % (min(phases), max(phases)))
        if any(b < a for a, b in zip(phases, phases[1:])):
            found.append("the frame phases do not rise")
        return found


def _name(raw: bytes, longs: List[str]) -> str:
    end = raw.find(b"\x00")
    if end < 0:  # no terminator: the name fills the field
        text = binary.decode_string(raw)
        if text not in longs:
            longs.append(text)
        return text
    return binary.decode_string(raw[:end])


def _read_mdat(reader: binary.BinaryReader) -> List[Tuple[float, str, str]]:
    reader.take(4, "RTM_MDAT padding")
    count = reader.u32("event count")
    items = []
    for _ in range(count):
        phase = reader.f32("event phase")
        texts = []
        for what in ("event name", "event value"):
            length = reader.u8(what + " length")
            texts.append(binary.decode_string(reader.take(length, what)))
        items.append((phase, texts[0], texts[1]))
    return items


def _read_0101(reader: binary.BinaryReader, longs: List[str]):
    x, z, y = (reader.f32("motion") for _ in range(3))
    frame_count = reader.u32("frame count")
    bone_count = reader.u32("bone count")
    # Each frame is 4 + 80 bytes per bone; refuse a count the data cannot hold before allocating anything.
    if frame_count * (4 + bone_count * 80) + bone_count * NAME_FIELD > reader.remaining:
        raise RtmError("the header says %d frames of %d bones, which is more than the file holds" % (frame_count, bone_count))
    bones = [_name(bytes(reader.take(NAME_FIELD, "bone name")), longs) for _ in range(bone_count)]
    frames = []
    record = struct.Struct("<12f")
    for _ in range(frame_count):
        frame = Frame(reader.f32("frame phase"))
        for _ in range(bone_count):
            name = _name(bytes(reader.take(NAME_FIELD, "bone name")), longs)
            frame.transforms.append(Transform(name, _matrix_from_floats(record.unpack(reader.take(48, "bone matrix")))))
        frames.append(frame)
    return (x, y, z), bones, frames


def read_rtm(data: bytes, source: str = "") -> Rtm:
    """Read a plain RTM from bytes. Raises ``RtmError`` for anything that is not one (a BMTR file says so)."""
    if data[:4] == b"BMTR":
        raise RtmError("this is a binarised animation (BMTR), not a plain RTM; read it with anim_source.read_animation, "
                       "which needs the skeleton's parents")
    reader = binary.BinaryReader(data)
    out = Rtm(source=source)
    seen = False
    try:
        while reader.remaining:
            signature = bytes(reader.take(8, "block signature"))
            if signature == _SIGNATURE:
                out.motion, out.bones, out.frames = _read_0101(reader, out.long_names)
                seen = True
            elif signature == _MDAT:
                out.props = _read_mdat(reader)
            else:
                raise RtmError("unknown block signature %r (not a plain RTM)" % signature)
    except binary.BinaryEOFError as ex:
        raise RtmError("the file is cut short: %s" % ex) from None
    if not seen:
        raise RtmError("no RTM_0101 block (not a plain RTM)")
    return out


def read_rtm_file(path: str) -> Rtm:
    with open(path, "rb") as file:
        return read_rtm(file.read(), path)


def write_rtm(rtm: Rtm) -> bytes:
    """The bytes of a plain RTM (``RTM_File.write``): the events first when there are any. Raises ``RtmError`` for a bone name
    over 31 characters. The motion is written in the file's order (the reverse of the read)."""
    out = bytearray()
    if rtm.props:
        out += _MDAT + struct.pack("<II", 0, len(rtm.props))
        for phase, name, value in rtm.props:
            out += struct.pack("<f", phase)
            for text in (name, value):
                raw = binary.encode_string(text)
                if len(raw) > 255:
                    raise RtmError("event text %r is longer than 255 bytes" % text)
                out += bytes((len(raw),)) + raw
    out += _SIGNATURE + struct.pack("<3f", rtm.motion[0], rtm.motion[2], rtm.motion[1])
    out += struct.pack("<II", len(rtm.frames), len(rtm.bones))

    def field(text: str) -> bytes:
        raw = binary.encode_string(text)
        if len(raw) > MAX_NAME:
            raise RtmError("the bone name %r is longer than %d characters" % (text, MAX_NAME))
        return raw.ljust(NAME_FIELD, b"\x00")

    for bone in rtm.bones:
        out += field(bone)
    for frame in rtm.frames:
        out += struct.pack("<f", frame.phase)
        for transform in frame.transforms:
            out += field(transform.bone) + struct.pack("<12f", *_floats_from_matrix(transform.matrix))
    return bytes(out)

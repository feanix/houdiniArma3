# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# Modified from A3OB io/data_rtm.py (BMTR_* classes, v2.5.1) and io/compression.py (lzo1x_decompress).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# Changes from the original: the readers work on in-memory bytes with a bounds-checked cursor (no file objects); the
# frames come out as numpy arrays of 4x4 matrices instead of nested lists; the relative-to-absolute walk takes the
# parent map as an argument and multiplies by the already absolute parent; the LZO decoder keeps A3OB's logic but
# works on a bytes buffer and writes into a preallocated bytearray.
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Reader for the binarised animation format BMTR (versions 3 to 5), as found in the game's PBOs, and the LZO1X
decoder it needs. Pure Python plus numpy.

Format notes: https://community.bistudio.com/wiki/Rtm_Binarised_File_Format

A frame holds one transform per bone, relative to the parent bone: a quaternion (4 int16, divided by 16384) and a
location (3 float16). ``BMTRFile.absolute_matrices(parents)`` multiplies them down the hierarchy and returns matrices in
the same convention as the plain RTM reader (A3OB ``RTM_Transform.matrix``: 4x4, translation in the last column,
Arma axes with y up). The matrix of a bone is the parent's absolute matrix times its own.
"""

from __future__ import annotations

import struct

import numpy as np


class BMTRError(Exception):
    pass


class LZOError(BMTRError):
    pass


def lzo1x_decompress(data, pos, expected):
    """Decode one LZO1X stream from ``data`` starting at ``pos`` into exactly ``expected`` bytes.

    Returns ``(output bytes, position after the stream)``. Raises ``LZOError`` on a bad stream."""
    out = bytearray()
    size = len(data)
    state = 0

    def need_room(length):
        if expected - len(out) < length:
            raise LZOError("output overrun (free %d, match %d)" % (expected - len(out), length))

    def take(length):
        nonlocal pos
        if pos + length > size:
            raise LZOError("input ends early")
        chunk = data[pos:pos + length]
        pos += length
        return chunk

    def copy_match(distance, length):
        need_room(length)
        start = len(out) - distance
        if start < 0:
            raise LZOError("match reaches before the start of the output")
        if distance >= length:
            out.extend(out[start:start + length])
        else:  # overlapping: a repeating pattern
            piece = out[start:]
            out.extend(piece * (length // distance))
            out.extend(piece[:length % distance])

    def get_length(x, mask):
        nonlocal pos
        length = x & mask
        if not length:
            while True:
                if pos >= size:
                    raise LZOError("input ends early")
                x = data[pos]
                pos += 1
                if x:
                    break
                length += 255
            length += mask + x
        return length

    if pos >= size:
        raise LZOError("input ends early")
    x = data[pos]
    pos += 1
    if x > 17:
        length = x - 17
        need_room(length)
        out.extend(take(length))
        state = min(4, length)
        if pos >= size:
            raise LZOError("input ends early")
        x = data[pos]
        pos += 1

    while True:
        if x <= 15:
            if not state:
                length = 3 + get_length(x, 15)
                need_room(length)
                out.extend(take(length))
                state = 4
            elif state < 4:
                state = x & 3
                distance = (take(1)[0] << 2) + (x >> 2) + 1
                copy_match(distance, 2)
                need_room(state)
                out.extend(take(state))
            else:
                state = x & 3
                distance = (take(1)[0] << 2) + (x >> 2) + 2049
                copy_match(distance, 3)
                need_room(state)
                out.extend(take(state))
        elif x > 63:
            state = x & 3
            if x > 127:
                length = 5 + ((x >> 5) & 3)
            else:
                length = 3 + ((x >> 5) & 1)
            distance = (take(1)[0] << 3) + ((x >> 2) & 7) + 1
            copy_match(distance, length)
            need_room(state)
            out.extend(take(state))
        elif x > 31:
            length = 2 + get_length(x, 31)
            extra = struct.unpack("<H", take(2))[0]
            state = extra & 3
            copy_match((extra >> 2) + 1, length)
            need_room(state)
            out.extend(take(state))
        else:
            length = 2 + get_length(x, 7)
            extra = struct.unpack("<H", take(2))[0]
            distance = 16384 + ((x & 8) << 11) + (extra >> 2)
            state = extra & 3
            if distance == 16384:
                if length != 3:
                    raise LZOError("invalid end of stream (match length %d)" % length)
                break
            copy_match(distance, length)
            need_room(state)
            out.extend(take(state))
        if pos >= size:
            raise LZOError("input ends early")
        x = data[pos]
        pos += 1

    if len(out) != expected:
        raise LZOError("stream gave %d bytes, expected %d" % (len(out), expected))
    return bytes(out), pos


class _Cursor:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, count):
        if self.pos + count > len(self.data):
            raise BMTRError("the file ends early")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def floats(self, count):
        return struct.unpack("<%df" % count, self.take(4 * count))

    def asciiz(self):
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise BMTRError("the file ends early")
        text = self.data[self.pos:end].decode("cp1252", "replace")
        self.pos = end + 1
        return text


class BMTRFile:
    """A read BMTR file. ``phases`` is a float array (frames,), ``motion`` the (x, y, z) motion vector in Arma axes
    (A3OB order), ``bones`` the bone names in file order, ``props`` a list of (phase, name, value) (version 5),
    ``quaternions`` (frames, bones, 4) float64 and ``locations`` (frames, bones, 3) float64 both relative to the parent
    and already in A3OB's axis order for ``as_matrix``."""

    VERSIONS = (3, 4, 5)

    def __init__(self):
        self.version = 5
        self.motion = (0.0, 0.0, 0.0)
        self.bones = []
        self.props = []
        self.phases = np.zeros(0, dtype=np.float32)
        self.quaternions = np.zeros((0, 0, 4))
        self.locations = np.zeros((0, 0, 3))

    @property
    def frame_count(self):
        return len(self.phases)

    def relative_matrices(self):
        """(frames, bones, 4, 4) matrices relative to the parent (A3OB ``BMTR_Transform.as_rtm``)."""
        q = self.quaternions
        qx, qz, qy, qw = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
        m = np.zeros(q.shape[:-1] + (4, 4))
        m[..., 0, 0] = 1 - 2 * qy ** 2 - 2 * qz ** 2
        m[..., 0, 1] = 2 * qx * qy - 2 * qz * qw
        m[..., 0, 2] = -(2 * qx * qz + 2 * qy * qw)
        m[..., 1, 0] = 2 * qx * qy + 2 * qz * qw
        m[..., 1, 1] = 1 - 2 * qx ** 2 - 2 * qz ** 2
        m[..., 1, 2] = -(2 * qy * qz - 2 * qx * qw)
        m[..., 2, 0] = -(2 * qx * qz - 2 * qy * qw)
        m[..., 2, 1] = -(2 * qy * qz + 2 * qx * qw)
        m[..., 2, 2] = 1 - 2 * qx ** 2 - 2 * qy ** 2
        m[..., :3, 3] = self.locations
        m[..., 3, 3] = 1
        return m

    def absolute_matrices(self, parents):
        """(frames, bones, 4, 4) absolute matrices. ``parents`` maps a bone name to its parent name (empty string or a
        name that is not in the file means no parent), ordered so that a parent comes before its children (the order
        of a skeleton's bones). Bones of the file that ``parents`` does not name keep their relative matrix, as in
        A3OB. Names match case-insensitively."""
        matrices = self.relative_matrices()
        index = {name.lower(): i for i, name in enumerate(self.bones)}
        for bone, parent in parents.items():
            i = index.get(bone.lower())
            if i is None:
                continue
            j = index.get(parent.lower()) if parent else None
            if j is None:
                continue
            matrices[:, i] = matrices[:, j] @ matrices[:, i]
        return matrices


def read_bmtr(data):
    """Read a BMTR file from bytes. Raises ``BMTRError`` (``LZOError`` for a bad compressed block)."""
    data = bytes(data)
    cur = _Cursor(data)
    if cur.take(4) != b"BMTR":
        raise BMTRError("not a BMTR file (bad signature)")
    out = BMTRFile()
    out.version = cur.u32()
    if out.version not in BMTRFile.VERSIONS:
        raise BMTRError("unknown BMTR version %d" % out.version)
    cur.take(1)
    x, z, y = cur.floats(3)
    out.motion = (x, y, z)
    count_frames = cur.u32()
    cur.take(4)
    count_bones = cur.u32()
    if cur.u32() != count_bones:
        raise BMTRError("bone count mismatch")
    if count_bones > 4096 or count_frames > 1 << 20:
        raise BMTRError("implausible counts (%d bones, %d frames)" % (count_bones, count_frames))
    out.bones = [cur.asciiz() for _ in range(count_bones)]
    if out.version > 4:
        cur.take(4)
        for _ in range(cur.u32()):
            cur.take(4)
            name = cur.asciiz()
            phase = cur.floats(1)[0]
            out.props.append((phase, name, cur.asciiz()))
    if cur.u32() != count_frames:
        raise BMTRError("frame count mismatch")

    def block(expected):
        """Return the bytes of a block that may be LZO compressed, and move the cursor past it."""
        compressed = expected >= 1024
        if out.version > 4:
            compressed = cur.take(1)[0] != 0
        if not compressed:
            return cur.take(expected)
        raw, cur.pos = lzo1x_decompress(data, cur.pos, expected)
        return raw

    out.phases = np.frombuffer(block(count_frames * 4), dtype="<f4").astype(np.float32)
    quaternions = np.zeros((count_frames, count_bones, 4))
    locations = np.zeros((count_frames, count_bones, 3))
    record = np.dtype([("q", "<i2", 4), ("l", "<f2", 3)])
    for f in range(count_frames):
        bones_in_frame = cur.u32()
        if bones_in_frame != count_bones:
            raise BMTRError("frame %d holds %d bones, expected %d" % (f, bones_in_frame, count_bones))
        rec = np.frombuffer(block(bones_in_frame * 14), dtype=record)
        quaternions[f] = rec["q"] / 16384.0
        lx, lz, ly = rec["l"][:, 0].astype(np.float64), rec["l"][:, 1].astype(np.float64), rec["l"][:, 2].astype(np.float64)
        locations[f, :, 0] = -lx
        locations[f, :, 1] = -ly
        locations[f, :, 2] = lz
    out.quaternions = quaternions
    out.locations = locations
    if cur.pos != len(data):
        raise BMTRError("EOF not found (%d bytes left)" % (len(data) - cur.pos))
    return out

# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""The list of LODs in a P3D file, read without reading the LODs (for the LODs menu of P3D Import).

Points and normals are skipped by their counts, faces by their two path strings and TAGGs by their lengths. Nothing is
decoded or allocated, so a big file is listed quickly. The layout is the one ``a3ob_core.p3d.reader`` reads. Pure Python.
"""

import mmap
import struct
from typing import List

from a3ob_core.p3d import END_OF_FILE, P3DEOFError, P3DError, decode_signature, format_lod_name

__all__ = ["read_lod_names"]

_MLOD_MAGIC = b"MLOD"
_MLOD_VERSION = 257
_LOD_MAGIC = b"P3DM"
_TAGG_MAGIC = b"TAGG"
_POINT_SIZE = 16
_NORMAL_SIZE = 12
_FACE_FIXED = 72
_END_OF_FILE = END_OF_FILE.encode("ascii")


def read_lod_names(path: str) -> List[str]:
    """The display names of the LODs of a P3D file, in file order (the same as ``P3DModel.lods[i].name``).

    Raises ``P3DError`` for a file that is not an MLOD or whose framing is damaged."""
    with open(path, "rb") as file:
        try:
            data = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # an empty file cannot be mapped
            raise P3DError("%s: the file is empty" % path) from None
        try:
            return _lod_names(data)
        except (struct.error, IndexError):
            raise P3DEOFError("%s: P3D data is truncated" % path) from None
        except P3DError as ex:
            raise type(ex)("%s: %s" % (path, ex)) from None
        finally:
            data.close()


def _lod_names(data) -> List[str]:
    size = len(data)
    if bytes(data[:4]) != _MLOD_MAGIC:
        raise P3DError("not an MLOD P3D: signature is %r, expected %r" % (bytes(data[:4]), _MLOD_MAGIC))
    version, count_lods = struct.unpack_from("<II", data, 4)
    if version != _MLOD_VERSION:
        raise P3DError("unsupported MLOD version %d (only %d is supported)" % (version, _MLOD_VERSION))
    if count_lods * 53 > size - 12:
        raise P3DEOFError("LOD count %d does not fit in the remaining %d bytes" % (count_lods, size - 12))
    find = data.find
    pos = 12
    names = []
    for index in range(count_lods):
        if bytes(data[pos:pos + 4]) != _LOD_MAGIC:
            raise P3DError("LOD %d (offset %d): unsupported LOD signature" % (index, pos))
        points, normals, faces = struct.unpack_from("<3I", data, pos + 12)
        pos += 28 + _POINT_SIZE * points + _NORMAL_SIZE * normals
        for _face in range(faces):
            first = find(b"\x00", pos + _FACE_FIXED)
            second = find(b"\x00", first + 1) if first >= 0 else -1
            if second < 0:
                raise P3DEOFError("LOD %d: a face is truncated" % index)
            pos = second + 1
        if bytes(data[pos:pos + 4]) != _TAGG_MAGIC:
            raise P3DError("LOD %d: invalid TAGG section signature at offset %d" % (index, pos))
        pos += 4
        while True:
            end = find(b"\x00", pos + 1)
            if end < 0:
                raise P3DEOFError("LOD %d: a TAGG name is not terminated" % index)
            length = struct.unpack_from("<I", data, end + 1)[0]
            is_end = bytes(data[pos + 1:end]) == _END_OF_FILE
            pos = end + 5 + length
            if is_end:
                break
        signature = struct.unpack_from("<f", data, pos)[0]
        pos += 4
        names.append(format_lod_name(*decode_signature(signature)))
    return names

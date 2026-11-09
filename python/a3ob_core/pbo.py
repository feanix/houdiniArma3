# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB). Written for this port (task #88); not derived from A3OB.
"""A read-only PBO reader (Arma addon archives).

Format (https://community.bistudio.com/wiki/PBO_File_Format): a run of header entries, each a zero-terminated name and
five little-endian 32-bit numbers (packing method, original size, reserved, timestamp, data size); an entry with an
empty name ends the list. The data blocks follow in header order. The first entry may be the product entry ("Vers",
empty name) followed by zero-terminated property strings. A 20-byte SHA-1 trails the data.

Only the header is read when a file is opened. ``read`` seeks to one entry and reads its bytes.

- Packing 0 ("stored") is read as is.
- Packing "Cprs" (Arma's LZSS) is decoded. The Arma 3 anims PBOs on the owner's disk store their RTMs uncompressed.
- Packing "Enco" (encrypted, the ``.ebo`` files of Arma Reforger) raises ``PBOUnreadable``, as does a file without a
  readable header.

Pure Python, standard library only.
"""

from __future__ import annotations

import struct

PACK_STORED = 0
PACK_VERS = 0x56657273  # "Vers" read as a little-endian number
PACK_CPRS = 0x43707273  # "Cprs"
PACK_ENCO = 0x456E636F  # "Enco"


class PBOError(Exception):
    pass


class PBOUnreadable(PBOError):
    """The file is not a readable PBO (encrypted .ebo, or a broken header)."""


class PBOEntry:
    __slots__ = ("name", "packing", "original_size", "timestamp", "data_size", "offset")

    def __init__(self, name, packing, original_size, timestamp, data_size, offset):
        self.name = name
        self.packing = packing
        self.original_size = original_size
        self.timestamp = timestamp
        self.data_size = data_size
        self.offset = offset

    def __repr__(self):
        return "PBOEntry(%r, packing=%#x, size=%d)" % (self.name, self.packing, self.original_size)


def lzss_decompress(data, expected):
    """Decode Arma's LZSS (the "Cprs" packing). ``data`` is the packed block, ``expected`` the original size.

    Flag bytes are read low bit first; a set bit is one literal byte, a clear bit a two-byte match: 12 bits of absolute
    position into a 4096 byte ring (filled with spaces, written from 4078) and 4 bits of length minus 3. A 4-byte
    checksum trails the data (not checked)."""
    ring = bytearray(b" " * 4096)
    ring_pos = 4096 - 18
    out = bytearray()
    i = 0
    size = len(data)
    while len(out) < expected:
        if i >= size:
            raise PBOError("LZSS data ends early (%d of %d bytes)" % (len(out), expected))
        flags = data[i]
        i += 1
        for bit in range(8):
            if len(out) >= expected:
                break
            if flags & (1 << bit):
                if i >= size:
                    raise PBOError("LZSS data ends early")
                value = data[i]
                i += 1
                out.append(value)
                ring[ring_pos] = value
                ring_pos = (ring_pos + 1) & 4095
            else:
                if i + 1 >= size:
                    raise PBOError("LZSS data ends early")
                low, high = data[i], data[i + 1]
                i += 2
                src = low | ((high & 0xF0) << 4)
                length = (high & 0x0F) + 3
                for k in range(length):
                    value = ring[(src + k) & 4095]
                    out.append(value)
                    ring[ring_pos] = value
                    ring_pos = (ring_pos + 1) & 4095
    return bytes(out[:expected])


class PBO:
    """An open PBO. ``entries`` lists the file entries in archive order (not the product entry); ``properties`` holds the
    product/version strings as a list of (name, value) pairs. Use as a context manager or call ``close``."""

    def __init__(self, path):
        self.path = path
        self.entries = []
        self.properties = []
        self._file = open(path, "rb")
        try:
            self._read_header()
        except BaseException:
            self._file.close()
            raise

    def _read_header(self):
        f = self._file
        head = f.read(1 << 16)
        pos = 0
        raw = []

        def need(count):
            nonlocal head
            while len(head) < pos + count:
                more = f.read(1 << 16)
                if not more:
                    raise PBOUnreadable("%s: the PBO header ends early" % self.path)
                head += more

        def asciiz():
            nonlocal pos
            while True:
                end = head.find(b"\0", pos)
                if end >= 0:
                    break
                need(len(head) - pos + 1)
            text = head[pos:end].decode("cp1252", "replace")
            pos = end + 1
            return text

        first = True
        while True:
            name = asciiz()
            need(20)
            packing, original, _reserved, timestamp, size = struct.unpack_from("<5I", head, pos)
            pos += 20
            if first:
                first = False
                if packing == PACK_ENCO:
                    raise PBOUnreadable("%s: encrypted PBO (.ebo), not readable" % self.path)
                if packing == PACK_VERS:
                    while True:  # property strings: name, value, ... ended by an empty name
                        key = asciiz()
                        if not key:
                            break
                        self.properties.append((key, asciiz()))
                    continue
            if not name and not packing and not original and not size:
                break
            if not name:
                raise PBOUnreadable("%s: unexpected entry without a name" % self.path)
            if packing == PACK_ENCO:
                raise PBOUnreadable("%s: encrypted entry %s" % (self.path, name))
            raw.append((name, packing, original, timestamp, size))
        offset = pos
        for name, packing, original, timestamp, size in raw:
            self.entries.append(PBOEntry(name, packing, original, timestamp, size, offset))
            offset += size

    def find(self, name):
        """The entry called ``name`` (case-insensitive, slashes either way), or None."""
        key = name.replace("/", "\\").lower()
        for entry in self.entries:
            if entry.name.replace("/", "\\").lower() == key:
                return entry
        return None

    def read(self, entry):
        """The bytes of one entry (an ``PBOEntry`` or its name). Nothing else is read."""
        if isinstance(entry, str):
            found = self.find(entry)
            if found is None:
                raise KeyError(entry)
            entry = found
        self._file.seek(entry.offset)
        data = self._file.read(entry.data_size)
        if len(data) != entry.data_size:
            raise PBOError("%s: entry %s is cut off" % (self.path, entry.name))
        if entry.packing == PACK_STORED:
            return data
        if entry.packing == PACK_CPRS:
            return lzss_decompress(data, entry.original_size)
        raise PBOError("%s: unknown packing %#x" % (entry.name, entry.packing))

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

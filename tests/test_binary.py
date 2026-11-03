"""Tests for a3ob_core.binary (port of A3OB io/binary_handler.py).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import io
import struct
import unittest

from a3ob_core import binary


class TestFileReaders(unittest.TestCase):
    def test_read_asciiz(self):
        stream = io.BytesIO(b"abc\x00def\x00")
        self.assertEqual(binary.read_asciiz(stream), "abc")
        self.assertEqual(binary.read_asciiz(stream), "def")

    def test_read_asciiz_raises_at_eof_instead_of_looping(self):
        # A3OB compared file.read(1) to "" instead of b"" and looped forever here.
        with self.assertRaises(binary.BinaryEOFError):
            binary.read_asciiz(io.BytesIO(b"no terminator"))
        with self.assertRaises(EOFError):
            binary.read_asciiz(io.BytesIO(b""))

    def test_fixed_size_reads_raise_at_eof(self):
        for reader, data in ((binary.read_ulong, b"\x01\x02"), (binary.read_float, b""),
                             (binary.read_byte, b""), (binary.read_double, b"1234567")):
            with self.subTest(reader=reader.__name__), self.assertRaises(binary.BinaryEOFError):
                reader(io.BytesIO(data))
        with self.assertRaises(binary.BinaryEOFError):
            binary.read_ulongs(io.BytesIO(b"\x00" * 7), 2)

    def test_scalar_values(self):
        stream = io.BytesIO(struct.pack("<BhHiIefd", 200, -2, 65000, -5, 4000000000, 0.5, 1.25, 2.5))
        self.assertEqual(binary.read_byte(stream), 200)
        self.assertEqual(binary.read_short(stream), -2)
        self.assertEqual(binary.read_ushort(stream), 65000)
        self.assertEqual(binary.read_long(stream), -5)
        self.assertEqual(binary.read_ulong(stream), 4000000000)
        self.assertEqual(binary.read_half(stream), 0.5)
        self.assertEqual(binary.read_float(stream), 1.25)
        self.assertEqual(binary.read_double(stream), 2.5)

    def test_compressed_uint_round_trip(self):
        for value in (0, 1, 127, 128, 300, 16383, 16384, 2 ** 21 + 5, 2 ** 32 - 1):
            with self.subTest(value=value):
                stream = io.BytesIO()
                binary.write_compressed_uint(stream, value)
                stream.seek(0)
                self.assertEqual(binary.read_compressed_uint(stream), value)
                self.assertEqual(stream.read(), b"")

    def test_asciiz_field(self):
        stream = io.BytesIO()
        binary.write_asciiz_field(stream, "key", 8)
        self.assertEqual(stream.getvalue(), b"key\x00\x00\x00\x00\x00")
        stream.seek(0)
        self.assertEqual(binary.read_asciiz_field(stream, 8), "key")
        with self.assertRaises(ValueError):
            binary.write_asciiz_field(io.BytesIO(), "12345678", 8)
        with self.assertRaises(ValueError):
            binary.read_asciiz_field(io.BytesIO(b"12345678"), 8)
        with self.assertRaises(binary.BinaryEOFError):
            binary.read_asciiz_field(io.BytesIO(b"abc"), 8)

    def test_lascii(self):
        stream = io.BytesIO()
        binary.write_lascii(stream, "hello")
        stream.seek(0)
        self.assertEqual(binary.read_lascii(stream), "hello")
        with self.assertRaises(binary.BinaryEOFError):
            binary.read_lascii(io.BytesIO(b"\x05abc"))
        with self.assertRaises(ValueError):
            binary.write_lascii(io.BytesIO(), "x" * 256)


class TestStrings(unittest.TestCase):
    def test_every_byte_round_trips(self):
        raw = bytes(range(1, 256))
        self.assertEqual(binary.encode_string(binary.decode_string(raw)), raw)

    def test_no_surrogates(self):
        # Review major: surrogateescape turned the 5 undefined bytes into lone surrogates, which segfault hython when
        # stored in a string attribute. They now map to the C1 code points, as Windows does.
        raw = bytes(range(1, 256))
        text = binary.decode_string(raw)
        self.assertFalse([c for c in text if 0xD800 <= ord(c) <= 0xDFFF])
        self.assertEqual(len(text), 255)
        self.assertEqual(binary.encode_string(text), raw)
        for byte in binary.CP1252_UNDEFINED:
            self.assertEqual(binary.decode_string(bytes((byte,))), chr(byte))
            self.assertEqual(binary.encode_string(chr(byte)), bytes((byte,)))
        self.assertEqual(binary.read_asciiz(io.BytesIO(b"sel\x81x\x00")), "sel\x81x")

    def test_surrogates_are_not_encodable(self):
        for text in ("sel\udc81", "\udce9", "\ud800"):
            with self.subTest(text=ascii(text)), self.assertRaises(ValueError):
                binary.encode_string(text)

    def test_cp1252_text(self):
        self.assertEqual(binary.decode_string(b"caf\xe9 \x80"), "café €")
        self.assertEqual(binary.encode_string("café €"), b"caf\xe9 \x80")

    def test_unencodable_and_nul(self):
        with self.assertRaises(ValueError):
            binary.encode_string("ř")  # Czech r-caron is not in cp1252
        with self.assertRaises(ValueError):
            binary.encode_asciiz("a\x00b")
        with self.assertRaises(ValueError):
            binary.write_asciiz(io.BytesIO(), "a\x00b")


class TestArrays(unittest.TestCase):
    def test_float_bits_survive(self):
        # Signalling NaN, negative zero, denormal and infinity: array copies keep every bit.
        raw = struct.pack("<4I", 0x7F800001, 0x80000000, 0x00000001, 0x7F800000)
        values = binary.array_from_bytes(binary.F32, raw)
        self.assertEqual(len(values), 4)
        self.assertEqual(binary.array_to_bytes(values), raw)

    def test_u32(self):
        raw = struct.pack("<3I", 0, 0x02000000, 0xFFFFFFFF)
        values = binary.array_from_bytes(binary.U32, raw)
        self.assertEqual(list(values), [0, 0x02000000, 0xFFFFFFFF])
        self.assertEqual(binary.array_to_bytes(values), raw)


class TestBinaryReader(unittest.TestCase):
    def test_reads(self):
        reader = binary.BinaryReader(struct.pack("<BIf", 7, 123, 1.5) + b"name\x00" + struct.pack("<2I", 4, 5))
        self.assertEqual(reader.u8(), 7)
        self.assertEqual(reader.u32(), 123)
        self.assertEqual(reader.f32(), 1.5)
        self.assertEqual(reader.asciiz(), b"name")
        self.assertEqual(reader.u32s(2), (4, 5))
        self.assertEqual(reader.remaining, 0)

    def test_eof(self):
        reader = binary.BinaryReader(b"\x01\x02\x03")
        with self.assertRaises(binary.BinaryEOFError) as ctx:
            reader.u32("thing")
        self.assertIn("thing", str(ctx.exception))
        self.assertIn("offset 0", str(ctx.exception))
        with self.assertRaises(binary.BinaryEOFError):
            reader.asciiz()
        with self.assertRaises(binary.BinaryEOFError):
            reader.take(2 ** 40)


if __name__ == "__main__":
    unittest.main()

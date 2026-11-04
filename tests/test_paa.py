"""PAA reader and picture cache (task #53, D29). The PAA files here are built by the tests; no Arma 3 bytes are stored.

The Samples profile decodes every .paa under the Arma 3 Samples folder (skipped when it is absent).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import random
import shutil
import struct
import tempfile
import time
import unittest
import zlib

from a3ob_core import paa, png
from a3ob_houdini import paa_cache, settings
from tests import samples_support as ss

LZO_ABCDABCD = bytes([21]) + b"abcd" + bytes([108, 0]) + bytes([17, 0, 0])  # 4 literals, copy 4 back (4 bytes), end


def block_dxt1(v0, v1, codes):
    table = sum(c << (2 * i) for i, c in enumerate(codes))
    return struct.pack("<HHI", v0, v1, table)


def block_dxt5(a0, a1, acodes, v0, v1, codes):
    bits = sum(c << (3 * i) for i, c in enumerate(acodes))
    return bytes([a0, a1]) + bits.to_bytes(6, "little") + block_dxt1(v0, v1, codes)


def make_paa(type_id, width, height, payload, lzo=False, taggs=(), extra_mips=()):
    out = struct.pack("<H", type_id)
    for name, data in taggs:
        out += b"GGAT" + name.encode("ascii")[::-1] + struct.pack("<I", len(data)) + data
    out += struct.pack("<H", 0)
    for w, h, data, flag in [(width, height, payload, lzo)] + list(extra_mips):
        out += struct.pack("<HH", w | (0x8000 if flag else 0), h) + len(data).to_bytes(3, "little") + data
    return out + b"\x00" * 6  # the 0 x 0 mipmap that ends the list, then the end-of-file ushort


def pixels(rgba, width, x, y):
    return tuple(rgba[(y * width + x) * 4:(y * width + x) * 4 + 4])


class TestDecode(unittest.TestCase):
    def test_dxt1_pixels_and_row_order(self):
        # Two blocks side by side, 8 x 4. Left: codes 0 (red), 1 (blue), 2 (2/3 mix), 3 (1/3 mix) by row. Right: 3-colour.
        left = block_dxt1(0xF800, 0x001F, [0] * 4 + [1] * 4 + [2] * 4 + [3] * 4)
        right = block_dxt1(0x001F, 0xF800, [2] * 8 + [3] * 8)  # v0 < v1: half mix, and code 3 is transparent black
        width, height, rgba = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 8, 4, left + right))
        self.assertEqual((width, height, len(rgba)), (8, 4, 8 * 4 * 4))
        self.assertEqual(pixels(rgba, 8, 0, 0), (255, 0, 0, 255))
        self.assertEqual(pixels(rgba, 8, 3, 1), (0, 0, 255, 255))
        self.assertEqual(pixels(rgba, 8, 1, 2), (170, 0, 85, 255))
        self.assertEqual(pixels(rgba, 8, 2, 3), (85, 0, 170, 255))
        self.assertEqual(pixels(rgba, 8, 4, 0), (128, 0, 128, 255))
        self.assertEqual(pixels(rgba, 8, 7, 3), (0, 0, 0, 0))

    def test_dxt5_pixels_and_block_order(self):
        # 4 x 8: the upper block is red with alpha codes 0 and 1, the lower is green with alpha 6 of the 8-step table.
        upper = block_dxt5(255, 0, [0, 1] * 8, 0xF800, 0xF800, [0] * 16)
        lower = block_dxt5(255, 0, [2] * 16, 0x07E0, 0x07E0, [0] * 16)
        _, _, rgba = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT5, 4, 8, upper + lower))
        self.assertEqual(pixels(rgba, 4, 0, 0), (255, 0, 0, 255))
        self.assertEqual(pixels(rgba, 4, 1, 0), (255, 0, 0, 0))
        self.assertEqual(pixels(rgba, 4, 3, 7), (0, 255, 0, 219))  # (6 * 255 + 0 + 3) // 7
        # The 5-step alpha table (a0 <= a1) ends with 0 and 255.
        five = block_dxt5(10, 200, [6, 7] * 8, 0, 0, [0] * 16)
        _, _, rgba = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT5, 4, 4, five))
        self.assertEqual(pixels(rgba, 4, 0, 0)[3], 0)
        self.assertEqual(pixels(rgba, 4, 1, 0)[3], 255)

    @unittest.skipIf(paa._np is None, "numpy is not available")
    def test_numpy_and_plain_python_agree(self):
        rng = random.Random(5)
        for block_bytes, decode in ((8, paa.decode_dxt1), (16, paa.decode_dxt5)):
            data = bytes(rng.randrange(256) for _ in range(16 * 16 * block_bytes))  # 64 x 64 pixels
            self.assertEqual(decode(data, 64, 64, use_numpy=True), decode(data, 64, 64, use_numpy=False))

    def test_largest_mipmap_is_used_and_taggs_are_read(self):
        big = block_dxt1(0xF800, 0xF800, [0] * 16) * 4  # 8 x 8
        small = block_dxt1(0x001F, 0x001F, [0] * 16)
        data = make_paa(paa.TYPE_DXT1, 8, 8, big, taggs=[("AVGC", b"\x01\x02\x03\x04")], extra_mips=[(4, 4, small, False)])
        parsed = paa.read_paa(data)
        self.assertEqual([(m.width, m.height) for m in parsed.mips], [(8, 8), (4, 4)])
        self.assertEqual(parsed.tagg("AVGC").data, b"\x01\x02\x03\x04")
        width, _, rgba = paa.decode_paa_bytes(data)
        self.assertEqual((width, pixels(rgba, 8, 0, 0)), (8, (255, 0, 0, 255)))

    def test_lzo_stream(self):
        self.assertEqual(bytes(paa.lzo1x_decompress(LZO_ABCDABCD, 8)), b"abcdabcd")
        with self.assertRaisesRegex(paa.PaaError, "bytes"):
            paa.lzo1x_decompress(LZO_ABCDABCD, 9)
        with self.assertRaisesRegex(paa.PaaError, "overrun"):
            paa.lzo1x_decompress(LZO_ABCDABCD, 6)
        with self.assertRaisesRegex(paa.PaaError, "ends early"):
            paa.lzo1x_decompress(LZO_ABCDABCD[:-3], 8)

    def test_lzo_compressed_mipmap(self):
        block = block_dxt1(0xF800, 0x001F, [0, 1, 2, 3] * 4)  # 8 bytes
        stream = bytes([17 + 8]) + block + bytes([17, 0, 0])  # one literal run of 8 bytes, then the end marker
        plain = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 4, 4, block))
        packed = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 4, 4, stream, lzo=True))
        self.assertEqual(plain, packed)
        # A match that repeats: 4 literals "abcd" then a copy gives 8 bytes, the size of one DXT1 block.
        _, _, rgba = paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 4, 4, LZO_ABCDABCD, lzo=True))
        self.assertEqual(len(rgba), 64)


class TestErrors(unittest.TestCase):
    def test_truncated(self):
        good = make_paa(paa.TYPE_DXT1, 4, 4, block_dxt1(0, 0, [0] * 16), taggs=[("AVGC", b"abcd")])
        for cut in (0, 1, 3, 9, 14, 20, 25, len(good) - 6):
            with self.assertRaisesRegex(paa.PaaError, "truncated"):
                paa.decode_paa_bytes(good[:cut])

    def test_unknown_and_unsupported_types(self):
        with self.assertRaisesRegex(paa.PaaError, "unknown format type 0x1234"):
            paa.decode_paa_bytes(struct.pack("<H", 0x1234) + b"\x00" * 20)
        data = make_paa(0x4444, 4, 4, b"\x00" * 32)
        with self.assertRaisesRegex(paa.PaaError, "ARGB4444 cannot be decoded"):
            paa.decode_paa_bytes(data)
        with self.assertRaisesRegex(paa.PaaError, "palettes"):
            paa.decode_paa_bytes(struct.pack("<HH", paa.TYPE_DXT1, 1) + b"\x00" * 20)

    def test_bad_size_and_missing_file(self):
        with self.assertRaisesRegex(paa.PaaError, "multiple of 4"):
            paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 6, 4, b"\x00" * 16))
        with self.assertRaisesRegex(paa.PaaError, "needed"):
            paa.decode_paa_bytes(make_paa(paa.TYPE_DXT1, 8, 8, b"\x00" * 8))
        with self.assertRaisesRegex(paa.PaaError, "cannot be read"):
            paa.decode_paa(os.path.join(tempfile.gettempdir(), "a3ob_no_such_file.paa"))


class TestPng(unittest.TestCase):
    def test_png_round_trip_by_hand(self):
        raw = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        data = png.encode_png(2, 1, raw)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        start = data.index(b"IDAT")
        length = struct.unpack(">I", data[start - 4:start])[0]
        self.assertEqual(zlib.decompress(data[start + 4:start + 4 + length]), b"\x00" + raw)
        with self.assertRaises(ValueError):
            png.encode_png(2, 2, raw)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="a3ob_paa_test_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.cache = os.path.join(self.folder, "cache")
        self.source = os.path.join(self.folder, "tex_co.paa")
        self.write(0xF800)

    def write(self, color, mtime=None):
        with open(self.source, "wb") as file:
            file.write(make_paa(paa.TYPE_DXT1, 4, 4, block_dxt1(color, color, [0] * 16)))
        if mtime:
            os.utime(self.source, (mtime, mtime))

    def test_reuse_and_invalidate(self):
        first = paa_cache.cached_image(self.source, self.cache)
        self.assertTrue(first.startswith(self.cache) and first.endswith(".png") and os.path.basename(first).startswith("tex_co_"))
        stamp = os.stat(first).st_mtime_ns
        self.assertEqual(paa_cache.cached_image(self.source, self.cache), first)
        self.assertEqual(os.stat(first).st_mtime_ns, stamp)  # not written again
        self.write(0x001F, mtime=os.stat(self.source).st_mtime + 10)  # new content and a new modification time
        second = paa_cache.cached_image(self.source, self.cache)
        self.assertNotEqual(second, first)
        self.assertTrue(os.path.isfile(second))
        with open(second, "rb") as file:
            self.assertIn(b"IHDR", file.read(40))

    def test_setting_and_default_folder(self):
        saved = settings.Settings(paa_cache_dir=self.cache)
        path = os.path.join(self.folder, "settings.json")
        settings.save(saved, path)
        loaded, problems = settings.load(path)
        self.assertEqual((loaded.paa_cache_dir, problems), (self.cache, []))
        self.assertEqual(paa_cache.cache_dir(self.cache), os.path.abspath(self.cache))
        old = os.environ.get("HOUDINI_TEMP_DIR")
        os.environ["HOUDINI_TEMP_DIR"] = self.folder
        try:
            self.assertEqual(paa_cache.default_cache_dir(), os.path.join(self.folder, "a3ob_paa_cache"))
        finally:
            if old is None:
                del os.environ["HOUDINI_TEMP_DIR"]
            else:
                os.environ["HOUDINI_TEMP_DIR"] = old

    def test_bad_source_is_an_error(self):
        with self.assertRaises(paa.PaaError):
            paa_cache.cached_image(os.path.join(self.folder, "missing.paa"), self.cache)
        bad = os.path.join(self.folder, "bad.paa")
        with open(bad, "wb") as file:
            file.write(b"\x01\x02")
        with self.assertRaises(paa.PaaError):
            paa_cache.cached_image(bad, self.cache)


@unittest.skipUnless(ss.AVAILABLE, ss.SKIP_REASON)
class TestSamplesPaa(unittest.TestCase):
    def test_every_paa_decodes(self):
        files = []
        for folder, _, names in os.walk(ss.ROOT):
            files += [os.path.join(folder, n) for n in names if n.lower().endswith(".paa")]
        if not files:
            self.skipTest("the Samples folder holds no .paa files")
        started = time.perf_counter()
        pixels_total = 0
        for name in sorted(files):
            width, height, rgba = paa.decode_paa(name)
            self.assertEqual(len(rgba), width * height * 4, name)
            pixels_total += width * height
        print("\nSamples .paa: %d files, %d pixels, %.2f s" % (len(files), pixels_total, time.perf_counter() - started))


if __name__ == "__main__":
    unittest.main()

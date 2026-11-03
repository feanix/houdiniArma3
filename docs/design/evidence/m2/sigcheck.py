# M2 revision check S6: LOD signature storage rule (raw bits + decoded view), with M1's decode/encode.
# Run with system CPython 3.12: PYTHONPATH=E:/Projects/houdiniArma3/python python -I sigcheck.py <p3d>...
# (-I ignores PYTHONPATH, so the script inserts python/ itself.)
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "python"))
from a3ob_core.p3d import P3DModel  # noqa: E402
from a3ob_core.p3d.lod_types import LodType, decode_signature, encode_signature, f32  # noqa: E402


def bits(x):
    return struct.unpack("<i", struct.pack("<f", x))[0]


def from_bits(i):
    return struct.unpack("<f", struct.pack("<i", i))[0]


def import_lod(sig):
    """What the importer stores: (a3ob_lod_signature, a3ob_lod_type, a3ob_lod_resolution as float32)."""
    t, r = decode_signature(sig)
    return bits(sig), int(t), f32(r)


def export_sig(stored_bits, t_h, r_h):
    """The spec's export rule. Returns (signature, path)."""
    sig = from_bits(stored_bits)
    t, r = decode_signature(sig)
    if int(t) == t_h and bits(f32(r)) == bits(r_h):
        return sig, "stored"
    return encode_signature(LodType(t_h), r_h), "encoded"


def roundtrip(sig):
    b, t, r = import_lod(sig)
    out, path = export_sig(b, t, r)
    return bits(out) == bits(sig), path


sigs = []
for p in sys.argv[1:]:
    for lod in P3DModel.read_file(p).lods:
        sigs.append(lod.resolution_signature)
ok = [roundtrip(s) for s in sigs]
print("S6 fixture LODs:", len(sigs), "L0:", sum(1 for o, _ in ok if o))

near = [f32(1e13 * (1 + 3e-7)), f32(1e15 * (1 - 5e-7))]
for s in near:
    t, r = decode_signature(s)
    try:
        encode_signature(LodType.UNKNOWN, s)
        unk = "ok"
    except ValueError as ex:
        unk = "raises"
    print("S6 near-miss", repr(s), "decodes", t.name, r, "encode(type)==sig:", bits(encode_signature(t, r)) == bits(s),
          "encode(UNKNOWN, sig):", unk, "-> new rule:", roundtrip(s))

rng = random.Random(257)
fail = 0
paths = {}
enc_differs = 0
n = 200000
for _ in range(n):
    b = rng.getrandbits(32) - (1 << 31)
    s = from_bits(b)
    if s != s:  # NaN: compare bits
        pass
    good, path = roundtrip(s)
    paths[path] = paths.get(path, 0) + 1
    if not good:
        fail += 1
    t, r = decode_signature(s)
    try:
        if bits(encode_signature(t, f32(r))) != bits(s):
            enc_differs += 1
    except (ValueError, OverflowError):
        enc_differs += 1
print("S6 random f32 bit patterns:", n, "round-trip failures with the new rule:", fail, "paths:", paths)
print("S6 of those, encode_signature(decoded type, f32(decoded res)) would NOT give the source bits:", enc_differs)

# Edits take the encode path
b, t, r = import_lod(encode_signature(LodType.VISUAL, 1.0))
print("S6 edit Resolution 1 -> 2:", export_sig(b, t, f32(2.0)), "| type Visual -> Geometry:",
      export_sig(b, int(LodType.GEOMETRY), 0.0))

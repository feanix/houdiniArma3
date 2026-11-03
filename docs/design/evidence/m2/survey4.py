"""Survey 4 (M2 spec): face UVs in LODs without a #UVSet# TAGG; f32 round trip of selection weight bytes."""
import struct, sys
def rd(f, fmt): return struct.unpack(fmt, f.read(struct.calcsize(fmt)))
def asciiz(f):
    o = bytearray()
    while (c := f.read(1)) != b"\0": o += c
    return o
for path in sys.argv[1:]:
    f = open(path, "rb"); f.read(4); _, nlod = rd(f, "<II")
    for li in range(nlod):
        f.read(12); nv, nn, nf, _ = rd(f, "<IIII"); f.read(16 * nv + 12 * nn)
        nz = 0
        for _ in range(nf):
            n = rd(f, "<I")[0]; c = [rd(f, "<IIff") for _ in range(4)][:n]; rd(f, "<I"); asciiz(f); asciiz(f)
            nz += sum(1 for x in c if x[2] or x[3])
        f.read(4); names = []
        while True:
            rd(f, "<B"); name = asciiz(f).decode(); ln = rd(f, "<I")[0]; f.read(ln)
            if name == "#EndOfFile#": break
            names.append(name)
        res = rd(f, "<f")[0]
        if "#UVSet#" not in names and nf:
            print(path.split("/")[-1], "LOD %d %.3e: no #UVSet# TAGG, face corners with nonzero UV: %d" % (li, res, nz))
f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
bad = [b for b in range(1, 256) if min(255, max(1, round(255 - 254 * f32((255 - b) / 254)))) != b]
print("weight bytes 1..255 failing decode->f32->encode:", bad)

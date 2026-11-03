"""Survey 3 (M2 spec): sharp-edge pairs vs real polygon edges; proxy vertex membership; mass length."""
import struct, sys
def rd(f, fmt): return struct.unpack(fmt, f.read(struct.calcsize(fmt)))
def asciiz(f):
    o = bytearray()
    while (c := f.read(1)) != b"\0": o += c
    return o.decode("ascii", "replace")
for path in sys.argv[1:]:
    f = open(path, "rb"); f.read(4); _, nlod = rd(f, "<II")
    print(path)
    for li in range(nlod):
        f.read(12); nv, nn, nf, _ = rd(f, "<IIII")
        f.read(16 * nv); f.read(12 * nn)
        faces = []
        for _ in range(nf):
            n = rd(f, "<I")[0]; c = [rd(f, "<IIff") for _ in range(4)][:n]; rd(f, "<I"); asciiz(f); asciiz(f)
            faces.append([x[0] for x in c])
        f.read(4); taggs = []
        while True:
            rd(f, "<B"); name = asciiz(f); ln = rd(f, "<I")[0]; d = f.read(ln)
            if name == "#EndOfFile#": break
            taggs.append((name, d))
        res = rd(f, "<f")[0]
        edges = {}
        for fi, vs in enumerate(faces):
            for i in range(len(vs)):
                a, b = vs[i], vs[(i + 1) % len(vs)]
                edges.setdefault(frozenset((a, b)), []).append(fi)
        for name, d in taggs:
            if name == "#SharpEdges#" and d:
                pairs = [struct.unpack_from("<II", d, i) for i in range(0, len(d), 8)]
                real = sum(1 for p in pairs if frozenset(p) in edges)
                degen = sum(1 for p in pairs if p[0] == p[1])
                dup = len(pairs) - len({frozenset(p) for p in pairs})
                # direction: does pair (a,b) match an edge traversal a->b in some face (file order)?
                fwd = 0
                for a, b in pairs:
                    for fi in edges.get(frozenset((a, b)), []):
                        vs = faces[fi]; i = vs.index(a)
                        if vs[(i + 1) % len(vs)] == b: fwd += 1; break
                print("  LOD %d %.3e sharp pairs=%d real-edges=%d degenerate=%d duplicates=%d matches-a->b-in-a-face=%d" % (li, res, len(pairs), real, degen, dup, fwd))
            if name == "#Mass#":
                print("  LOD %d mass tagg len=%d nv*4=%d" % (li, len(d), nv * 4))
        sels = [(n, d) for n, d in taggs if not n.startswith("#")]
        for n, d in sels:
            if n.startswith("proxy:"):
                members = [i for i, b in enumerate(d[:nv]) if b]
                others = sorted({(m, d2[i]) for i in members for m, d2 in sels if m != n and d2[i]})
                shared = [fi for fi, vs in enumerate(faces) if set(vs) & set(members) and not set(vs) <= set(members)]
                print("  LOD %d %s -> other selections on its verts: %s; faces sharing its verts: %d" % (li, n.split("\\")[-1], others[:6], len(shared)))
                break

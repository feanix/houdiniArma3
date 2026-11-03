"""Second survey: byte-level facts for lossless round trip (M2 spec). Independent code."""
import struct, sys, collections, math

def rd(f, fmt):
    return struct.unpack(fmt, f.read(struct.calcsize(fmt)))

def asciiz(f):
    out = bytearray()
    while True:
        c = f.read(1)
        if c == b"\0":
            return out.decode("ascii", "replace")
        out += c

def survey(path):
    f = open(path, "rb")
    f.read(4); ver, nlod = rd(f, "<II")
    print(path)
    for li in range(nlod):
        f.read(4); rd(f, "<II")
        nv, nn, nf, lflags = rd(f, "<IIII")
        verts = [rd(f, "<fffI") for _ in range(nv)]
        norms = [rd(f, "<fff") for _ in range(nn)]
        faces = []
        pad_nonzero = 0
        for _ in range(nf):
            n = rd(f, "<I")[0]
            corners = [rd(f, "<IIff") for _ in range(4)]
            if n == 3 and corners[3] != (0, 0, 0.0, 0.0):
                pad_nonzero += 1
            fflag = rd(f, "<I")[0]
            tex = asciiz(f); mat = asciiz(f)
            faces.append((corners[:n], fflag, tex, mat))
        f.read(4)
        order = []
        taggs = {}
        inactive = 0
        sels = []
        while True:
            active = rd(f, "<B")[0]
            name = asciiz(f); ln = rd(f, "<I")[0]; data = f.read(ln)
            if name == "#EndOfFile#":
                break
            if not active:
                inactive += 1
            kind = name if name.startswith("#") else "sel"
            order.append(kind)
            taggs.setdefault(name, []).append(data)
            if kind == "sel":
                sels.append((name, data))
        res = rd(f, "<f")[0]
        # compress order
        comp = []
        for k in order:
            if comp and comp[-1][0] == k:
                comp[-1][1] += 1
            else:
                comp.append([k, 1])
        print("LOD %d %.3e TAGG order: %s inactive=%d tri-pad-nonzero=%d" % (li, res, comp, inactive, pad_nonzero))
        # normals table: first-appearance rebuild equal?
        seen = {}
        rebuilt = []
        for corners, *_ in faces:
            for c in corners:
                v = norms[c[1]]
                if v not in seen:
                    seen[v] = len(rebuilt); rebuilt.append(v)
        dup = nn - len(set(norms))
        print("   normals: nn=%d unique=%d dupvalues=%d rebuilt_first_appearance_equal=%s" % (nn, len(set(norms)), dup, rebuilt == norms))
        lens = [math.sqrt(sum(x * x for x in v)) for v in norms]
        if lens:
            print("   normal length min/max %.6f %.6f" % (min(lens), max(lens)))
        # winding vs normal: sign of dot(cross(e1,e2), stored normal) in file coords
        pos = neg = 0
        for corners, *_ in faces[:2000]:
            p = [verts[c[0]][:3] for c in corners[:3]]
            e1 = [p[1][i] - p[0][i] for i in range(3)]
            e2 = [p[2][i] - p[0][i] for i in range(3)]
            cr = (e1[1]*e2[2]-e1[2]*e2[1], e1[2]*e2[0]-e1[0]*e2[2], e1[0]*e2[1]-e1[1]*e2[0])
            nsum = [sum(norms[c[1]][i] for c in corners) for i in range(3)]
            d = sum(cr[i] * nsum[i] for i in range(3))
            if d > 0: pos += 1
            elif d < 0: neg += 1
        print("   dot(cross(v1-v0,v2-v0), stored normal) >0: %d  <0: %d" % (pos, neg))
        # uvset 0 tagg vs face uvs
        if "#UVSet#" in taggs:
            for data in taggs["#UVSet#"]:
                sid = struct.unpack_from("<I", data)[0]
                vals = struct.unpack_from("<%df" % ((len(data) - 4) // 4), data, 4)
                if sid == 0:
                    face_uv = [x for corners, *_ in faces for c in corners for x in c[2:4]]
                    print("   uvset0 tagg == face uv: %s (n=%d)" % (list(vals) == face_uv, len(vals) // 2))
                vs = vals[1::2]
                if vs:
                    print("   uv v range %.4f..%.4f, min |v|>0: %g" % (min(vs), max(vs), min((abs(v) for v in vs if v), default=0)))
        # face byte mismatches detail
        mism = collections.Counter()
        for name, data in sels:
            vb = data[:nv]; fb = data[nv:nv+nf]
            for fi, (corners, *_ ) in enumerate(faces):
                derived = all(vb[c[0]] for c in corners)
                if bool(fb[fi]) != derived:
                    mism[(name, "face=1,derived=0" if fb[fi] else "face=0,derived=1")] += 1
        if mism:
            print("   face-byte mismatches:", mism.most_common(6))
        # proxy triangles
        for name, data in sels:
            if name.startswith("proxy:"):
                members = [i for i, b in enumerate(data[:nv]) if b]
                fmem = [i for i, b in enumerate(data[nv:nv+nf]) if b]
                if li in (0, 5):
                    print("   proxy %s verts=%s faces=%s flags=%s" % (name, members, fmem, [hex(verts[i][3]) for i in members]))
                    if li == 5:
                        print("      tex/mat", [(faces[i][2], faces[i][3]) for i in fmem], "coords", [tuple(round(x, 4) for x in verts[i][:3]) for i in members])
                break
        # memory-point hint: directions
        if li == 5 or (len(faces) == 0 and nv):
            for name, data in sels:
                if name in ("lefteye", "righteye", "lfoot", "rfoot", "pilot", "head", "kneeleft", "kneeright", "eye", "pelvis", "footstepleft", "footsteprright", "wristleft", "wristright"):
                    m = [i for i, b in enumerate(data[:nv]) if b]
                    print("   %s: %s" % (name, [tuple(round(x, 3) for x in verts[i][:3]) for i in m]))
    print()

for p in sys.argv[1:]:
    survey(p)

"""Independent minimal MLOD P3D survey (written for the M2 spec; not A3OB code).
Usage: python survey_p3d.py file.p3d
Reports per-LOD facts needed by docs/design/houdini-data-model.md."""
import struct, sys, collections

def rd(f, fmt):
    s = struct.calcsize(fmt)
    return struct.unpack(fmt, f.read(s))

def asciiz(f):
    out = bytearray()
    while True:
        c = f.read(1)
        if not c:
            raise EOFError
        if c == b"\0":
            return out.decode("ascii", "replace")
        out += c

def survey(path):
    f = open(path, "rb")
    assert f.read(4) == b"MLOD"
    ver, nlod = rd(f, "<II")
    print(path, "version", ver, "lods", nlod)
    for li in range(nlod):
        sig = f.read(4); vmaj, vmin = rd(f, "<II")
        nv, nn, nf, lflags = rd(f, "<IIII")
        verts = [rd(f, "<fffI") for _ in range(nv)]
        norms = [rd(f, "<fff") for _ in range(nn)]
        faces = []
        sides = collections.Counter()
        texmat = []
        for _ in range(nf):
            n = rd(f, "<I")[0]
            sides[n] += 1
            corners = [rd(f, "<IIff") for _ in range(4)]
            fflag = rd(f, "<I")[0]
            tex = asciiz(f); mat = asciiz(f)
            faces.append((n, corners[:n], fflag))
            texmat.append((tex, mat))
        assert f.read(4) == b"TAGG"
        taggs = []
        while True:
            active = rd(f, "<B")[0]
            name = asciiz(f)
            ln = rd(f, "<I")[0]
            data = f.read(ln)
            if name == "#EndOfFile#":
                break
            taggs.append((active, name, ln, data))
        res = rd(f, "<f")[0]
        vflags = collections.Counter(v[3] for v in verts)
        fflags = collections.Counter(x[2] for x in faces)
        # sections: runs of identical (tex, mat)
        runs = 1 + sum(1 for a, b in zip(texmat, texmat[1:]) if a != b) if texmat else 0
        uniq = len(set(texmat))
        # normal sharing
        norm_refs = collections.Counter(c[1] for x in faces for c in x[1])
        print("LOD %d res=%r (%s) hdr=%s v%d.%d flags=0x%08x nv=%d nn=%d nf=%d sides=%s" % (
            li, res, "%.3e" % res, sig, vmaj, vmin, lflags, nv, nn, nf, dict(sides)))
        print("   vflags", {hex(k): c for k, c in vflags.most_common(5)}, "fflags", {hex(k): c for k, c in fflags.most_common(5)})
        print("   tex/mat unique=%d runs(sections in order)=%d" % (uniq, runs), "normals referenced=%d/%d" % (len(norm_refs), nn))
        # padding corner of tris nonzero?
        tagnames = collections.Counter()
        nsel = 0; nproxy = 0; face_mismatch = 0; face_nonbin = 0; vert_vals = collections.Counter(); sel_names = []
        uvset_ids = []
        for active, name, ln, data in taggs:
            if name.startswith("#") and name.endswith("#"):
                tagnames[name] += 1
                if name == "#UVSet#":
                    uvset_ids.append(struct.unpack_from("<I", data)[0])
                if name == "#Property#":
                    k = data[:64].split(b"\0")[0]; v = data[64:].split(b"\0")[0]
                    print("   property", k, v)
                if name == "#SharpEdges#":
                    print("   sharp edges pairs", ln // 8)
                if name not in ("#UVSet#", "#Property#", "#SharpEdges#", "#Mass#"):
                    print("   other tagg", name, "len", ln, "active", active)
                continue
            nsel += 1
            if name.startswith("proxy:"):
                nproxy += 1
            sel_names.append(name)
            vb = data[:nv]; fb = data[nv:nv + nf]
            assert ln == nv + nf, (name, ln, nv, nf)
            for b in vb:
                if b: vert_vals[b] += 1
            member = [b != 0 for b in vb]
            for fi, x in enumerate(faces):
                derived = all(member[c[0]] for c in x[1])
                if bool(fb[fi]) != derived:
                    face_mismatch += 1
                if fb[fi] not in (0, 1):
                    face_nonbin += 1
        print("   taggs", dict(tagnames), "uvset ids", uvset_ids)
        print("   selections=%d proxies=%d vertex-byte histogram(top)=%s face-bytes!=derived=%d face-bytes-not-0/1=%d" % (
            nsel, nproxy, vert_vals.most_common(4), face_mismatch, face_nonbin))
        odd = [n for n in sel_names if not all(ch.isalnum() or ch == "_" for ch in n) or n[:1].isdigit()]
        print("   names illegal as identifiers (%d):" % len(odd), odd[:8])
        print("   sample names:", sel_names[:12])
    rest = f.read()
    print("trailing bytes after last LOD:", len(rest), rest[:64])

for p in sys.argv[1:]:
    survey(p)

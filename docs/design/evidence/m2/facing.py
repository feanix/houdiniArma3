"""Facing check for sample_1_character LOD 0: compare foot vertex z-extent vs lower-leg (ankle) z centre."""
import struct, sys
def rd(f, fmt): return struct.unpack(fmt, f.read(struct.calcsize(fmt)))
def asciiz(f):
    o=bytearray()
    while (c:=f.read(1))!=b"\0": o+=c
    return o.decode()
f=open(sys.argv[1],"rb"); f.read(4); rd(f,"<II")
f.read(4); rd(f,"<II"); nv,nn,nf,_=rd(f,"<IIII")
V=[rd(f,"<fffI") for _ in range(nv)]; f.read(12*nn)
for _ in range(nf):
    n=rd(f,"<I")[0]; f.read(64); rd(f,"<I"); asciiz(f); asciiz(f)
f.read(4); sels={}
while True:
    a=rd(f,"<B")[0]; name=asciiz(f); ln=rd(f,"<I")[0]; d=f.read(ln)
    if name=="#EndOfFile#": break
    if not name.startswith("#"): sels[name]=[i for i,b in enumerate(d[:nv]) if b==1]
for s in ("leftfoot","rightfoot","leftleglower","head"):
    pts=[V[i] for i in sels.get(s,[])]
    if not pts: print(s,"none"); continue
    zs=[p[2] for p in pts]; xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    print("%-12s n=%4d x[%.3f,%.3f] y[%.3f,%.3f] z[%.3f,%.3f]"%(s,len(pts),min(xs),max(xs),min(ys),max(ys),min(zs),max(zs)))
# lowest 5% of foot points: z spread (toe vs heel)
pts=sorted((V[i] for i in sels["leftfoot"]), key=lambda p:p[1])[:40]
print("lowest leftfoot pts z range %.3f..%.3f mean %.3f"%(min(p[2] for p in pts),max(p[2] for p in pts),sum(p[2] for p in pts)/len(pts)))

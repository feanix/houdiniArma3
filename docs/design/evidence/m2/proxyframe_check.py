# M2 revision 3 check (X1): the spec's Houdini proxy frame (section 13.4) against M1's proxy_frame (file space).
# Pure Python, no hou. Run: python -I docs/design/evidence/m2/proxyframe_check.py (finds python/ relative to this file)
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "python"))
from a3ob_core.p3d.proxy import find_axis_vertices, proxy_frame  # noqa: E402


def sub(a, b): return tuple(x - y for x, y in zip(a, b))
def dot(a, b): return sum(x * y for x, y in zip(a, b))
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def norm(a):
    n = math.sqrt(dot(a, a))
    return tuple(x / n for x in a)
def neg(a): return tuple(-x for x in a)
def to_h(p): return (p[0], p[1], -p[2])  # the D-AXIS map M = diag(1, 1, -1)
def det3(x, y, z): return dot(x, cross(y, z))


def spec_frame(c, a, b):
    """Section 13.4 formula, in Houdini space. c, a, b = angle-sorted points (largest, middle, smallest angle)."""
    z = neg(norm(sub(a, c)))
    yp = norm(sub(b, c))
    x = norm(cross(yp, z))
    y = norm(cross(z, x))
    return x, y, z, c


def close(u, v, eps=1e-9):
    return all(abs(p - q) < eps for p, q in zip(u, v))


rng = random.Random(3)
worst = 0.0
bad_det = 0
mism = 0
n = 5000
for _ in range(n):
    pts = [tuple(rng.uniform(-5, 5) for _ in range(3)) for _ in range(3)]
    f = proxy_frame(*pts)  # M1, file space
    o, s, l = find_axis_vertices(*pts)  # M1's angle sort, file space
    # spec frame from the Houdini-space points, sorted the same way (find_axis_vertices is the shared sort)
    sx, sy, sz, sc = spec_frame(to_h(o), to_h(s), to_h(l))
    # the claimed relation: X_h = M f.x, Y_h = M f.y, Z_h = -M f.z, C = M f.origin
    ok = (close(sx, to_h(f.x_axis)) and close(sy, to_h(f.y_axis)) and close(sz, neg(to_h(f.z_axis)))
          and close(sc, to_h(f.origin)))
    mism += not ok
    # M1's frame mapped by M is left-handed (det -1); the spec frame is right-handed (det +1)
    mapped = det3(to_h(f.x_axis), to_h(f.y_axis), to_h(f.z_axis))
    spec = det3(sx, sy, sz)
    if not (abs(mapped + 1) < 1e-9 and abs(spec - 1) < 1e-9):
        bad_det += 1
print("X1 random triangles:", n)
print("X1 spec frame == (M x, M y, -M z, M origin) of M1's frame for all:", mism == 0, "(mismatches %d)" % mism)
print("X1 det of M1's mapped frame is -1 and det of the spec frame is +1 for all:", bad_det == 0)

# canonical A3OB triangle in Blender (0,0,0),(0,0,2),(0,1,0) = file (x, z_b, y_b) swap -> (0,0,0),(0,2,0),(0,0,1)
f = proxy_frame((0, 0, 0), (0, 2, 0), (0, 0, 1))
print("X1 canonical triangle, M1 file-space frame:", f.x_axis, f.y_axis, f.z_axis, "origin", f.origin)
o, s, l = find_axis_vertices((0, 0, 0), (0, 2, 0), (0, 0, 1))
print("X1 canonical triangle, spec Houdini frame:", spec_frame(to_h(o), to_h(s), to_h(l))[:3])

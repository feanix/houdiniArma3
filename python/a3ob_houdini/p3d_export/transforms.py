# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Small matrix helpers for packed transforms (spec 3.2 and 13.5), in plain Python.

A transform is 16 numbers, row-major, row-vector convention (``p_world = p_local * M``, translation in row 3), which is
what ``hou.Matrix4.asTuple()`` gives (spec F9).
"""

from typing import Optional, Sequence, Tuple

__all__ = ["IDENTITY", "det3", "is_identity", "transform_point", "normal_matrix", "transform_normal", "map_axes"]

Vec = Tuple[float, float, float]

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _rotation(m: Sequence[float]):
    return ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10]))


def det3(m: Sequence[float]) -> float:
    """Determinant of the 3x3 part. A negative value means the transform mirrors."""
    (a, b, c), (d, e, f), (g, h, i) = _rotation(m)
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def is_identity(m: Sequence[float], tolerance: float = 1e-9) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(m, IDENTITY))


def transform_point(m: Sequence[float], p: Sequence[float]) -> Vec:
    """``p * M`` for a row vector."""
    return (p[0] * m[0] + p[1] * m[4] + p[2] * m[8] + m[12],
            p[0] * m[1] + p[1] * m[5] + p[2] * m[9] + m[13],
            p[0] * m[2] + p[1] * m[6] + p[2] * m[10] + m[14])


def normal_matrix(m: Sequence[float]) -> Optional[Tuple[Tuple[float, ...], ...]]:
    """The matrix that carries normals for a point matrix: ``(R^-1)^T``, as rows for ``n * N``.

    For a rotation it is the rotation itself. None if the 3x3 part is singular."""
    (a, b, c), (d, e, f), (g, h, i) = _rotation(m)
    det = det3(m)
    if det == 0.0:
        return None
    # Row-vector convention: n' = n * (R^-1)^T. The inverse is adj(R) / det, so (R^-1)^T is the cofactor matrix / det.
    cof = (
        (e * i - f * h, f * g - d * i, d * h - e * g),
        (c * h - b * i, a * i - c * g, b * g - a * h),
        (b * f - c * e, c * d - a * f, a * e - b * d),
    )
    return tuple(tuple(value / det for value in row) for row in cof)


def transform_normal(n_matrix, n: Sequence[float]) -> Vec:
    """``n * N`` for a normal. With no matrix (singular transform) the normal passes unchanged."""
    if n_matrix is None:
        return (n[0], n[1], n[2])
    return tuple(n[0] * n_matrix[0][k] + n[1] * n_matrix[1][k] + n[2] * n_matrix[2][k] for k in range(3))


def map_axes(v: Sequence[float]) -> Vec:
    """Spec 4.3: the axis map ``diag(1, 1, -1)``. It is its own inverse, so it serves both directions."""
    return (v[0], v[1], -v[2])

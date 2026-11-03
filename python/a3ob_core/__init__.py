# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
# A3OB: MrClock (present add-on), Hans-Joerg "Alwarren" Frieden (original ArmaToolbox).
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.
"""Pure-Python core of the houdiniArma3 port: file formats and algorithms.

This package must never import ``hou``, ``bpy`` or any third-party module (no numpy). It runs on plain CPython 3.12+
and on hython 3.13, so it can be tested outside Houdini.

Subpackages and modules:

- ``a3ob_core.binary``: little-endian readers and writers for the BI generic data types.
- ``a3ob_core.p3d``: lossless reader/writer and semantic API for P3D MLOD files.
"""

__all__ = ["binary", "p3d"]

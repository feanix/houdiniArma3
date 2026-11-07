"""Print a rig digest of a Houdini scene: figures beside their reference values, no verdicts (docs/design/digests.md).

    hython scripts/a3ob_digest.py <digest> <scene.hip*> <node path> [options]

Digests: skeleton, skeleton-compare A B, deform-compare A B, weights, landmarks, feet N [N ...], wrap-distance. ``--help`` lists the options. The scene is
loaded read-only and never saved. Exit code 0 answered, 1 could not answer, 2 the question was written wrongly.

Run it with Houdini's own hython, without HOUDINI_PACKAGE_SKIP (the nodes of a rig scene need KineFX).

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "python"))  # this checkout's readers, not another copy


def main(argv):
    try:
        import hou  # noqa: F401
    except ImportError:
        print("digest: run this with Houdini's hython (it needs the hou module)", file=sys.stderr)
        return 1
    from a3ob_houdini import digest
    return digest.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

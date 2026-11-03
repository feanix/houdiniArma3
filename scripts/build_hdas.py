"""Generate the HDA text files of every A3OB node from its parameter table (D13).

    python scripts/build_hdas.py            write houdini/otls/<name>.hda/ for every node
    python scripts/build_hdas.py --check    change nothing; exit 1 if a checked-in file differs

The parameter tables are in python/a3ob_houdini/nodes/<kind>.py. The layout and the file contents come from
python/a3ob_houdini/hda_source.py. Do not edit the generated files by hand, and never save these assets from the
Houdini GUI (D13). Standard library only.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTLS_DIR = os.path.join(REPO_ROOT, "houdini", "otls")


def main(argv):
    sys.path.insert(0, os.path.join(REPO_ROOT, "python"))
    from a3ob_houdini import hda_source, nodes

    check = "--check" in argv
    failed = False
    for kind, spec in nodes.all_specs().items():
        if check:
            bad = hda_source.differences(spec, OTLS_DIR)
            for relative in bad:
                print("differs: %s/%s" % (hda_source.hda_folder_name(spec), relative))
            failed = failed or bool(bad)
        else:
            for path in hda_source.write(spec, OTLS_DIR):
                print("wrote %s" % os.path.relpath(path, REPO_ROOT))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

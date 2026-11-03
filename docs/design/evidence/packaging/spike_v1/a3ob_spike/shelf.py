"""What a shelf tool script would call to create the fallback node."""
import hou

from . import nodes

STABLE_CODE = "import a3ob_spike.entry as e\ne.cook(hou.pwd())\n"
# Naive wrapper: imports the logic function by its module-level name.
NAIVE_CODE = "from a3ob_spike.nodes import cook\ncook(hou.pwd())\n"


def create_python_sop(parent, name, naive=False):
    """Create a Python SOP with spare parameters; returns the node."""
    node = parent.createNode("python", name)
    node.parm("python").set(NAIVE_CODE if naive else STABLE_CODE)
    nodes.add_spare_parms(node)
    return node

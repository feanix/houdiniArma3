# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""New P3D without a dialog (ux-tools.md section 7.5): builds the starter network of D10.

    res1_mesh (Box) -------------------> res1_props (LOD Properties: Resolution 1) ------+
    geometry_mesh (Box) -> geometry_components (Group) -> geometry_mass (Mass, 100 kg)
        -> geometry_props (Geometry) ------------------------------------------------------+--> assemble --> export
    memory_points (Add, one point) -> memory_group (Group "new_point") -> memory_props (Memory) --+

The network passes the checks of spec 17.1 as it stands, so Save to Disk works on the first try. ``new_p3d_dialog`` is
the thin ``hou.ui`` layer on top. Needs ``hou``.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from . import selection_browser
from .errors import UserError
from .messages import format_problem

__all__ = ["TOOL", "StarterNetwork", "build_starter", "check_name"]

TOOL = "New P3D"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Node names, in the order of the diagram above.
NODE_NAMES = ("res1_mesh", "res1_props", "geometry_mesh", "geometry_components", "geometry_mass", "geometry_props",
              "memory_points", "memory_group", "memory_props", "assemble", "export")

_NOTE = ("Replace res1_mesh, geometry_mesh and memory_points with your own model, geometry and memory points. "
         "Everything else is set up: the LOD Properties nodes name the LODs, Assemble LODs makes the P3D geometry, "
         "and P3D Export writes it when you press Save to Disk.")

_COLUMN = 3.0  # network editor units between columns
_ROW = 1.2


@dataclass
class StarterNetwork:
    """What ``build_starter`` made: the network node and the nodes by the names above."""

    network: object
    nodes: Dict[str, object] = field(default_factory=dict)
    note: Optional[object] = None


def check_name(name: str) -> str:
    """The object name, or a ``UserError``. Houdini node names are letters, digits and underscores."""
    name = name.strip()
    if not _NAME.match(name):
        raise UserError(format_problem(TOOL, "the name %r cannot be a node name" % name,
                                       "Use letters, digits and underscores, and do not start with a digit"))
    return name


def _sop_network(parent):
    import hou
    if parent.childTypeCategory() != hou.sopNodeTypeCategory():
        raise UserError(format_problem(TOOL, "%s is not a SOP network" % parent.path(),
                                       "Choose a Geometry object, or leave it empty to make a new one"))
    return parent


def build_starter(parent=None, name: str = "new_p3d"):
    """Build the starter network in ``parent`` (a Geometry object or another SOP network), or in a new Geometry
    object called ``name`` under ``/obj`` when ``parent`` is None. Returns a ``StarterNetwork``.

    Nodes are created with the names above; if ``parent`` already uses a name, Houdini adds a number, so use the
    returned nodes, not the names. Raises ``UserError`` for a bad name or a parent that is not a SOP network."""
    import hou
    if parent is None:
        network = hou.node("/obj").createNode("geo", check_name(name))
    else:
        network = _sop_network(parent)
        name = network.name()
    made = StarterNetwork(network)

    def add(type_name: str, key: str, column: float, row: float):
        node = network.createNode(type_name, key)
        node.setPosition(hou.Vector2(column * _COLUMN, -row * _ROW))
        made.nodes[key] = node
        return node

    def group(source, key: str, selection: str, column: float, row: float):
        node = selection_browser.create_selection(source, selection)  # all points; the node is wired to ``source``
        node.setName(key, unique_name=True)
        node.setPosition(hou.Vector2(column * _COLUMN, -row * _ROW))
        made.nodes[key] = node
        return node

    def chain(node, source):
        node.setFirstInput(source)
        return node

    res1_mesh = add("box", "res1_mesh", 0, 0)
    _set_box(res1_mesh)
    res1_props = chain(add("a3ob_lod_properties", "res1_props", 1, 0), res1_mesh)
    res1_props.parm("lodtype").set("visual")
    res1_props.parm("resolution").set(1.0)

    geometry_mesh = add("box", "geometry_mesh", 0, 3)
    _set_box(geometry_mesh)
    geometry_components = group(geometry_mesh, "geometry_components", "component01", 1, 3)
    geometry_mass = chain(add("a3ob_mass", "geometry_mass", 2, 3), geometry_components)
    geometry_mass.parm("mode").set("total")
    geometry_mass.parm("value").set(100.0)
    geometry_props = chain(add("a3ob_lod_properties", "geometry_props", 3, 3), geometry_mass)
    geometry_props.parm("lodtype").set("geometry")

    memory_points = add("add", "memory_points", 0, 6)
    memory_points.parm("points").set(1)
    memory_group = group(memory_points, "memory_group", "new_point", 1, 6)
    memory_props = chain(add("a3ob_lod_properties", "memory_props", 3, 6), memory_group)
    memory_props.parm("lodtype").set("memory")

    assemble = add("a3ob_assemble_lods", "assemble", 4, 3)
    for number, props in enumerate((res1_props, geometry_props, memory_props)):
        assemble.setInput(number + 1, props)  # input 1 is the optional base: left empty

    export = chain(add("a3ob_p3d_export", "export", 5, 3), assemble)
    export.parm("file").set("$HIP/%s.p3d" % name)
    export.setDisplayFlag(True)
    export.setRenderFlag(True)

    made.note = network.createStickyNote()
    made.note.setText(_NOTE)
    made.note.setPosition(hou.Vector2(0, 1.5 * _ROW))
    made.note.setSize(hou.Vector2(4 * _COLUMN, 1.2))
    return made


def _set_box(box) -> None:
    """A box of size 1 that sits on the ground."""
    box.parmTuple("size").set((1.0, 1.0, 1.0))
    box.parm("ty").set(0.5)


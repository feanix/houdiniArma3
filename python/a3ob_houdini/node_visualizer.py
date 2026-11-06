# SPDX-License-Identifier: GPL-3.0-or-later
#
# houdiniArma3: a Houdini port of Arma 3 Object Builder (A3OB).
"""Node visualizers (viewport colouring that does not change the geometry), shared by the nodes that need one.

Task #32 research, Houdini 22.0.466:

* ``hou.viewportVisualizers`` exists only when the Houdini window is up (``hou.isUIAvailable()``): hython has no such
  attribute, so every function here does nothing without it and returns None or False.
* A node visualizer is made with ``hou.viewportVisualizers.createVisualizer(type, hou.viewportVisualizerCategory.Node,
  node)``. The recipe is the one of ``soputils.toggleVisualizer``: create a ``vis_generic``, then ``setType`` it to
  ``vis_color``, then ``setParm`` its settings, ``setName`` and ``setLabel``.
* ``hou.viewportVisualizers.visualizers(Node, node)`` lists the ones on a node. The docs call them "installed on
  individual nodes"; whether they survive save and reload could not be tried without the window. ``viz.destroy()``
  removes one.

Task #78, Houdini 22.0.466: the colours of a node visualizer draw only when the global Visualization button on the right
display toolbar of the viewport is on. Houdini 22's Python API has no call to read or set that button:
``hou.GeometryViewportSettings``, ``hou.GeometryViewport`` and ``hou.viewportVisualizers`` have none, and
``soputils.toggleVisualizer`` does not touch it (lead check, 2026-11-06). So the nodes cannot switch it on. They say so in a
node message instead (``HINT``), and the Visualize tooltips repeat it."""

try:
    import hou
except ImportError:
    hou = None

__all__ = ["available", "find", "ensure_color_ramp", "set_attrib", "set_range", "remove", "HINT"]

#: The one line the nodes with a Visualize toggle show while it is on (also the end of their tooltips).
HINT = ("The Visualize colours show only when the Visualization button on the right toolbar of the viewport is on.")


def available() -> bool:
    """True in a session that has viewport visualizers (the Houdini window), False in hython and plain Python."""
    return hou is not None and getattr(hou, "viewportVisualizers", None) is not None


def find(node, name: str):
    """The node visualizer of ``node`` called ``name``, or None."""
    if not available():
        return None
    for viz in hou.viewportVisualizers.visualizers(hou.viewportVisualizerCategory.Node, node):
        if viz.name() == name:
            return viz
    return None


def ensure_color_ramp(node, name: str, label: str, attrib: str, stops, low: float, high: float):
    """Make (or update and switch on) a Color visualizer of the point attribute ``attrib`` on ``node``.

    ``stops`` is ``((position, (r, g, b)), ...)`` from 0 to 1; ``low`` and ``high`` are the values at 0 and 1.
    Returns the visualizer, or None when this session has none.
    """
    if not available():
        return None
    viz = find(node, name)
    if viz is None:
        viz = hou.viewportVisualizers.createVisualizer(
            hou.viewportVisualizers.type("vis_generic"), hou.viewportVisualizerCategory.Node, node)
        viz.setType(hou.viewportVisualizers.type("vis_color"))
        viz.setName(name)
        viz.setLabel(label)
        viz.setParm("colortype", "attribramped")
        viz.setParm("attrib", attrib)
        viz.setParm("rangespec", "min-max")
        ramp = hou.Ramp((hou.rampBasis.Linear,) * len(stops), tuple(pos for pos, _ in stops),
                        tuple(color for _, color in stops))
        viz.setParm("colorramp", ramp)
    set_range(viz, low, high)
    viz.setIsActive(True)
    return viz


def set_attrib(viz, attrib: str, stops, low: float, high: float) -> None:
    """Point an existing Color visualizer at another point attribute, with its own ramp and range (Rig Check: the Show menu)."""
    viz.setParm("attrib", attrib)
    viz.setParm("colorramp", hou.Ramp((hou.rampBasis.Linear,) * len(stops), tuple(pos for pos, _ in stops),
                                      tuple(color for _, color in stops)))
    set_range(viz, low, high)


def set_range(viz, low: float, high: float) -> None:
    """Set the values at the two ends of the ramp. Writes only when a number changes."""
    for parm, value in (("minscalar", low), ("maxscalar", high)):
        if abs(viz.evalParmAsFloat(parm) - value) > 1e-9 * max(1.0, abs(value)):
            viz.setParm(parm, value)


def remove(node, name: str) -> bool:
    """Delete the node visualizer ``name`` of ``node``. True when one was deleted."""
    viz = find(node, name)
    if viz is None:
        return False
    viz.destroy()
    return True

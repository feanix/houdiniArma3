"""Stable entry points (same names as v1; the logic behind them changed)."""
from . import nodes


def cook(node):
    nodes.run_cook(node)


def save(kwargs):
    return nodes.save(kwargs)


def upgrade(node):
    """Bring the parameter layout of an older node up to date."""
    return nodes.add_spare_parms(node)

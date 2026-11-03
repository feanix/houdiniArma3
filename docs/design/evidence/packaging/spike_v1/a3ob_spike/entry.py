"""Stable entry points. Wrappers stored in .hip files or HDAs call only these.

Names here must not change once a wrapper has shipped. Everything behind
them (nodes.py) may be renamed freely.
"""
from . import nodes


def cook(node):
    nodes.cook(node)


def save(kwargs):
    return nodes.save(kwargs)

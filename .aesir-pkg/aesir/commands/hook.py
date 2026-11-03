"""hook (Claude Code hook entry: stdin JSON, never creates a db, never fails)."""
from .. import agenthook
from ._common import add

RAW = {"hook"}


def hook(a):
    return agenthook.main(a.db)


def register(sub):
    add(sub, "hook", hook, ("event", {"nargs": "?", "help": "ignored; the event comes from stdin JSON"}))

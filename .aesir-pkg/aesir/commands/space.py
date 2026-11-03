"""space put/get/list/rm."""
import json

from .. import service as S
from ._common import out, line, add, grp, text_arg as _text_arg


def space_put(c, a):
    S.space_put(c, a.actor, a.role, a.kind, a.key, json.loads(_text_arg(a.body)))
    print("stored")


def space_get(c, a):
    out(a, S.space_get(c, a.role, a.kind, a.key), lambda r: print(json.dumps(r["body"], indent=2, ensure_ascii=False)))


def space_list(c, a):
    out(a, S.space_list(c, a.role, a.kind),
        lambda rs: [line(r["kind"], r["key"], json.dumps(r["body"], ensure_ascii=False)) for r in rs])


def space_rm(c, a):
    S.space_rm(c, a.actor, a.role, a.kind, a.key)
    print("removed")


def register(sub):
        sp = grp(sub, "space")
        add(sp, "put", space_put, ("role", {}), ("kind", {}), ("key", {}), ("body", {"help": "JSON; @file reads a file"}))
        add(sp, "get", space_get, ("role", {}), ("kind", {}), ("key", {}))
        add(sp, "list", space_list, ("role", {}), ("--kind", {}))
        add(sp, "rm", space_rm, ("role", {}), ("kind", {}), ("key", {}))

"""Tracks: a cheap domain role = base role + scope globs + context files + gate profile, declared in aesir.toml
as [tracks.<name>]. A task may name one track (stored in tasks.extra, no column). The track's scope applies when
the task states none."""
import json

from . import gates
from .db import event, now, tx
from .service import AesirError, get_task

BASES = ("implementer", "reviewer", "runner", "researcher")


def _strs(name, key, v):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise AesirError(f"track '{name}': {key} must be a list of strings")
    return v


def load(root) -> dict:
    """{name: {name, base, scope, context, gates, model}} from <root>/aesir.toml, validated."""
    try:
        cfg = gates.load_config(root)
    except AesirError:
        return {}
    out = {}
    for name, t in (cfg.get("tracks") or {}).items():
        if not isinstance(t, dict):
            raise AesirError(f"track '{name}' must be a table")
        base = t.get("base")
        if base not in BASES:
            raise AesirError(f"track '{name}': base must be one of {', '.join(BASES)}")
        prof = t.get("gates", "")
        if prof and prof not in (cfg.get("gates") or {}):
            raise AesirError(f"track '{name}': no gate profile '{prof}'")
        out[name] = {"name": name, "base": base, "scope": _strs(name, "scope", t.get("scope", [])),
                     "context": _strs(name, "context", t.get("context", [])), "gates": prof,
                     "model": t.get("model", "")}
    return out


def get(root, name) -> dict:
    ts = load(root)
    if name not in ts:
        raise AesirError(f"no track '{name}' (have: {', '.join(ts) or 'none'})")
    return ts[name]


def task_track(t) -> str:
    try:
        return json.loads(t.get("extra") or "{}").get("track") or ""
    except ValueError:
        return ""


def set_track(conn, actor, tid, name, root):
    """Set (or clear with '') the task's track. The track must exist in aesir.toml."""
    if name:
        get(root, name)
    with tx(conn):
        t = get_task(conn, tid)
        try:
            extra = json.loads(t["extra"] or "{}")
        except ValueError:
            extra = {}
        old = extra.get("track") or ""
        if name:
            extra["track"] = name
        else:
            extra.pop("track", None)
        conn.execute("UPDATE tasks SET extra=?, updated=? WHERE id=?", (json.dumps(extra), now(), int(tid)))
        event(conn, actor, "task", tid, "set", {"track": {"from": old, "to": name}})


def effective_scope(t, root) -> str:
    """The task's own scope, else its track's scope (comma-joined), else ''. A track missing from the config
    contributes nothing."""
    if (t["scope"] or "").strip():
        return t["scope"]
    name = task_track(t)
    if not name:
        return ""
    try:
        return ", ".join(load(root).get(name, {}).get("scope", []))
    except AesirError:
        return ""


def context_lines(track) -> list:
    lines = [f"Track: {track['name']} (base {track['base']})",
             f"Track scope: {', '.join(track['scope']) or '(none)'}",
             "Track context files (read these, nothing else is preloaded): " + (", ".join(track["context"]) or "(none)"),
             f"Gate profile: {track['gates'] or '(default)'}"]
    if track["model"]:
        lines.append(f"Track model: {track['model']}")
    return lines

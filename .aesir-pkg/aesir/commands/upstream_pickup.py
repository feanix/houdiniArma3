"""upstream watch/link/close/status: pick up reports filed with report-upstream and close the loop."""
import json
import os
import sys

from .. import db, upstream, upstream_pickup as P
from ._common import add, grp, line, client_cmd, text_arg

PRE_DB = {"upstream"}  # watch needs no database; link/status open it themselves


def _actor(a):
    return os.environ.get("AESIR_AGENT") or a.actor or os.environ.get("AESIR_ROLE") or "unknown"


def _conn(a):
    return db.connect(db.require_project(a.db), allow_migrate=True if a.allow_migrate else None)


def watch_cmd(a):
    P.watch(db.project_root(), once=a.once, db_arg=a.db)


def link_cmd(a):
    c = _conn(a)
    try:
        url = P.link(c, _actor(a), db.project_root(), a.issue, a.task, a.workaround, post=not a.no_comment)
    finally:
        c.close()
    print(f"linked task #{a.task:03d} to {url}" + ("" if a.no_comment else " (triage comment posted)"))


def reply_cmd(a):
    text = text_arg(a.text)
    print(f"replied on upstream issue #{P.comment(db.project_root(), a.issue, P.reply_body(_actor(a), text))}")


def search_cmd(a):
    rows = P.search(db.project_root(), a.words, kind=a.kind)
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for r in rows:
        line(f"#{r['issue']}", r["state"].lower(), r["task"] or "no task", r["title"], r["url"])
        print(f"    workaround: {r['workaround'] or 'none recorded'}")
    if not rows:
        print("no matching reports")


def retry_cmd(a):
    path = db.db_path(a.db)
    left = len(upstream.load_queue(path))
    if not left:
        print("no queued reports")
        return
    c = _conn(a)
    try:
        n = upstream.retry_queue(c, path)
    finally:
        c.close()
    print(f"filed {n} of {left} queued report(s); {left - n} still pending")


def close_cmd(a):
    print(f"closed upstream issue #{P.close(db.project_root(), _actor(a), a.issue)}")


def fixed_in_cmd(a):
    """Post 'fixed in <tag>' on the linked issues, only once the tag is on origin (D193). Plans once and lists skips."""
    root = db.project_root()
    if not P.tag_on_origin(root, a.tag):
        sys.exit(f"upstream fixed-in: {a.tag} is not on origin; push it first "
                 f"(`git push origin main {a.tag}` after the full gate passes), then rerun")
    skip = [P.issue_number(x) for x in (a.skip or "").split(",") if x.strip()]
    plan = P.plan_release(root, a.tag, a.db, skip)  # one plan (one set of gh views) shared with the announce
    skipped = plan[1]
    done = P.announce_release(root, a.tag, a.db, _actor(a), plan)
    if done:
        print("commented 'fixed in %s' on upstream issue(s): %s" % (a.tag, ", ".join(f"#{n}" for n in done)))
    else:
        print(f"no upstream issues to announce for {a.tag}")
    for line in P.skip_lines(skipped, a.all):
        print(line)


def status_cmd(a):
    c = _conn(a)
    try:
        rows = P.report_status(c, db.project_root())
    finally:
        c.close()
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    released = [r for r in rows if r["fix"] and r["state"] != "CLOSED"]
    for r in rows:
        state = f"fix-released ({r['fix']})" if r in released else r["state"].lower()
        line(f"#{r['issue']}", state, r["title"], r["url"])
    if not rows:
        print("no upstream reports from this project")
    elif released:
        print(client_cmd("fix released: upgrade, verify, then `python -m aesir upstream close <issue>`"))


def register(sub):
    g = grp(sub, "upstream")
    add(g, "watch", watch_cmd, ("--once", {"action": "store_true", "help": "poll once and exit"}))
    add(g, "link", link_cmd, ("issue", {"help": "issue number or url"}), ("task", {"type": int}),
        ("--workaround", {"default": "", "help": "workaround for the triage comment (default: none yet)"}),
        ("--no-comment", {"action": "store_true", "help": "link only; post no triage comment"}))
    add(g, "reply", reply_cmd, ("issue", {"help": "issue number or url"}), ("text", {"help": "@file reads a file"}))
    add(g, "search", search_cmd, ("words", {"nargs": "+", "help": "search reports (open and closed) before filing"}),
        ("--kind", {"choices": upstream.KINDS, "help": "only reports with this label"}))
    add(g, "retry", retry_cmd)
    add(g, "close", close_cmd, ("issue", {"help": "issue number or url"}))
    add(g, "fixed-in", fixed_in_cmd, ("tag", {"help": "vX.Y.Z; must already be pushed to origin"}),
        ("--all", {"action": "store_true", "help": "list every skipped issue, not counts for the routine reasons"}),
        ("--skip", {"default": "", "help": "issue numbers (comma-separated) to leave out; listed as 'skipped by hand'"}))
    add(g, "status", status_cmd)

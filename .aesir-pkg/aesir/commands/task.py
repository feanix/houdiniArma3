"""task new/list/show/set/log/link and reject-approach."""
import sys

from ..invoke import cmd as client_cmd
from .. import db, escalation, evidence, friction, observe, taskorder, tips, tracks, service as S
from .evidence import fmt as fmt_evidence
from ._common import out, out_list, hint, line, add, grp, paging, text_arg as _text_arg


def _scope_notes(given, root):
    """After task new/set --scope (#209, #249): say when the stored comma list differs from the input, and warn per entry
    that matches no tracked path. Never fails the command."""
    norm = S.normalise_scope(given)
    if norm != (given or ""):
        print(f"scope stored as: {norm or '(empty)'}")
    for w in S.scope_warnings(root, given):
        print(w, file=sys.stderr)


def task_new(c, a):
    if a.track:
        tracks.get(db.project_root(), a.track)  # validate before creating anything
    tid = S.new_task(c, a.actor, a.title, _text_arg(a.criteria), a.requester, a.owner, a.scope)
    if a.track:
        tracks.set_track(c, a.actor, tid, a.track, db.project_root())
    if a.ui:
        observe.set_ui(c, a.actor, tid)
    print(f"task {tid} created")
    _scope_notes(a.scope, db.project_root())
    if a.branch:
        S.set_task(c, a.actor, tid, branch=a.branch, repo=db.project_root())
    else:
        print(client_cmd(f"warning: no branch recorded; once the implementer has one run: python -m aesir task set {tid} --branch <branch>")
              + " (gate, evidence and merge look it up)", file=sys.stderr)
    tips.print_for(c, a, "tasks")


def task_list(c, a):
    cmd = "python -m aesir task list" + (f" --status {a.status}" if a.status else "") + (f" --owner {a.owner}" if a.owner else "")
    rows = S.list_tasks(c, a.status, a.owner)
    first = f"python -m aesir task show {rows[0]['id']}" if rows else None
    out_list(a, rows, lambda ts: [line(f"#{t['id']:03d}", f"{t['status']:<17}", f"{t['owner'] or '-':<10}",
                          f"{t['target_release'] or '-':<7}", t["title"]) for t in ts],
             cmd, first, "python -m aesir status")


def _show_task(t):
    for k in ("title", "status", "owner", "requester", "scope", "target_release", "track", "branch", "gate_profile", "attempts",
              "escalations", "wontfix_reason", "created", "updated"):
        if t[k] not in ("", None):
            print(f"{k}: {t[k]}")
    if t.get("base"):
        print(f"base: #{t['base']['id']} [{t['base']['status']}]")
    if t["links"]:
        print("links:", ", ".join(t["links"]))
    if t.get("after"):
        print("after:", ", ".join(f"#{x['id']:03d} [{taskorder.fmt_status(x['status'])}]" for x in t["after"]))
    if t.get("blocks"):
        print("blocks:", ", ".join(f"#{x['id']:03d} [{x['status']}]" for x in t["blocks"]))
    print("\nspec:\n" + t["spec"])
    if t.get("evidence"):
        n_ran = sum(c["state"] == "ran" for c in t["evidence"])
        n_met = sum(c["state"] == "met" for c in t["evidence"])
        print(f"\nevidence per criterion: {n_ran} RAN (exit 0 only), {n_met} MET (expect matched), of {len(t['evidence'])}")
        for c in t["evidence"]:
            print(f"{c['n']}. [{evidence.state_tag(c)}] {c['text'][:90]}")
            for w in c.get("warnings", []):
                print(f"   warning: criterion cites `{w}` but no evidence command for it contains that")
            for e in c["entries"]:
                for ln in fmt_evidence(e):
                    print("   " + ln)
        print("\nevidence logs (read each; [OK]/RAN means the command ran, not that the criterion is met):")
        for ln in evidence.log_lines(t["evidence"]):
            print("  " + ln)
    print("\nlog:")
    for e in t["log"]:
        print(f"- {e['ts']} {e['actor']}: {e['text']}")


def task_show(c, a):
    t = S.show_task(c, a.id)
    t["evidence"] = evidence.status(c, t, evidence.head_of(t, db.project_root()))
    out(a, t, _show_task)
    hint(a, f"python -m aesir task log {a.id} \"<note>\"", f"python -m aesir thread {a.id}", f"python -m aesir task set {a.id} --status <status>")


def task_set(c, a):
    if a.track is not None:
        tracks.set_track(c, a.actor, a.id, a.track, db.project_root())
    ui = True if a.ui else False if a.no_ui else None
    if a.release is not None:  # #228: a placement; the other fields (if any) go through set_task below
        S.set_release(c, a.actor, a.id, a.release)
    if ui is not None:
        observe.set_ui(c, a.actor, a.id, ui)
    if a.deviation_cause:
        S.get_task(c, a.id)
        friction.record_deviation(c, a.actor, a.id, a.deviation_cause, a.deviation_criterion or "", a.reason or "")
    try:
        notes = S.set_task(c, a.actor, a.id, status=a.status, reason=a.reason, owner=a.owner, requester=a.requester,
                   scope=a.scope, title=a.title, spec=_text_arg(a.criteria), branch=a.branch,
                   gate_profile=a.gate_profile, attempts=a.attempts, repo=db.project_root(),
                   force_gate_skip=a.force_gate_skip, force_evidence_skip=a.force_evidence_skip, landed=a.landed,
                   carried_by=a.carried_by, base_task=a.base_task)
    except S.AesirError as e:
        if (a.track is None and ui is None and not a.deviation_cause and a.release is None) or str(e) != "nothing to change":
            raise
        notes = []
    print(f"task {a.id} updated")
    if a.scope is not None:
        _scope_notes(a.scope, db.project_root())
    for n in notes or []:
        print(n)
    if a.status == "verified":
        t = S.get_task(c, a.id)
        print("Read every evidence log before relying on this: [OK]/RAN means the command ran, not that the criterion is met.")
        for ln in evidence.log_lines(evidence.status(c, t, evidence.head_of(t, db.project_root()))):
            print("  " + ln)


def task_split(c, a):
    after = None if a.after is None else (a.id if a.after == "" else S.parse_id(a.after, "T"))
    new = S.split_task(c, a.actor, a.id, a.title, a.branch, _text_arg(a.criteria) if a.criteria else None, after,
                       repo=db.project_root())
    print(f"task {new} created (split from #{a.id})")


def task_reject_approach(c, a):
    escalation.reject_approach(c, a.actor, a.id, a.reason)
    f = escalation.failures(c, a.id)
    print(f"approach rejected; {f['failures']} failures on {f['tier']}")


def task_log(c, a):
    S.log_task(c, a.actor, a.id, a.text)
    print("logged")


def task_backfill_merge_commits(c, a):
    done, left = S.backfill_merge_commits(c, a.actor, db.project_root())
    for tid, sha in done:
        print(f"task {tid}: merge_commit = {sha[:12]}")
    print(f"backfilled {len(done)} task(s)")
    if left:
        print("left without a merge_commit (no passing full gate run on main): " + ", ".join(f"#{i}" for i in left))


def task_link(c, a):
    if a.kind == "after":  # task order (#176): 'task link A after B' = A runs after B
        b = S.parse_id(a.target, "#")
        taskorder.link(c, a.actor, a.id, b)
        print(f"task {a.id} now runs after #{b}")
        return
    S.add_link(c, a.actor, "task", a.id, a.kind, a.target)
    print("linked")


def task_unlink(c, a):
    if a.kind == "upstream":  # #259: drop a wrong/partial upstream issue link
        print(f"task {a.id} no longer linked to upstream issue #{S.unlink_upstream(c, a.actor, a.id, a.target)}")
        return
    taskorder.unlink(c, a.actor, a.id, S.parse_id(a.target, "#"))
    print(f"task {a.id} no longer runs after #{S.parse_id(a.target, '#')}")


def task_ready(c, a):
    rows = taskorder.ready(c)
    out_list(a, rows, lambda ts: [line(f"#{t['id']:03d}", f"{t['owner'] or '-':<10}", t["title"]) for t in ts],
             "python -m aesir task ready", f"python -m aesir task show {rows[0]['id']}" if rows else None)


def task_path(c, a):
    chain = taskorder.path(c, a.goal)
    data = {"goal": a.goal, "chain": [{"id": i, "status": s} for i, s in chain], "unblocked": len(chain) == 1}

    def render(d):
        if d["unblocked"]:
            print(f"#{a.goal:03d} is unblocked: no unfinished predecessors")
        print(" -> ".join(f"#{x['id']:03d} [{taskorder.fmt_status(x['status'])}]" for x in d["chain"]))
    out(a, data, render)


def register(sub):
        t = grp(sub, "task")
        add(t, "new", task_new, ("title", {}), ("--criteria", {"default": "", "help": "acceptance criteria; @file reads a file"}),
            ("--requester", {}), ("--owner", {"default": ""}), ("--scope", {"default": "", "help": "allowed paths, comma- or space-separated (entries cannot contain spaces); stored comma-joined"}),
            ("--track", {"default": "", "help": "track from aesir.toml"}),
            ("--branch", {"help": "the task branch, if it already exists (else set it later with task set N --branch)"}),
            ("--ui", {"action": "store_true", "help": "UI/behaviour task: criteria must cite a digest question or headless scenario to enter in_review"}))
        paging(add(t, "list", task_list, ("--status", {}), ("--owner", {})))
        add(t, "show", task_show, ("id", {"type": int}))
        add(t, "set", task_set, ("id", {"type": int}), ("--status", {}), ("--reason", {}),
            ("--owner", {}), ("--requester", {}), ("--scope", {"help": "allowed paths, comma- or space-separated (entries cannot contain spaces); stored comma-joined"}), ("--title", {}), ("--criteria", {}),
            ("--branch", {}), ("--gate-profile", {}), ("--attempts", {"type": int}),
            ("--track", {"help": "track from aesir.toml; '' clears"}),
            ("--release", {"help": "target release X.Y.Z (requester, advisor or owner); 'none' clears"}),
            ("--ui", {"action": "store_true", "help": "flag as UI/behaviour task"}),
            ("--no-ui", {"action": "store_true", "help": "clear the ui flag"}),
            ("--deviation-cause", {"choices": list(friction.CAUSES),
                                   "help": "record why the work deviated from the spec (lead on accept/bounce; advisor on verify)"}),
            ("--deviation-criterion", {"help": "the acceptance criterion deviated from"}),
            ("--landed", {"help": "with --status merged: docs-only commit <sha> or <base>..<sha> already on main (in_progress/in_review only)"}),
            ("--carried-by", {"help": "with --status merged: task <B> (merged/verified, has a merge_commit) already landed this work (in_progress/in_review/approved)"}),
            ("--base-task", {"help": "task <M> whose branch this task is built on: scope/docs-only/audit diff against M's branch while M is unmerged ('' clears)"}),
            ("--force-gate-skip", {"action": "store_true", "help": "lead only, needs --reason: skip the gate check"}),
            ("--force-evidence-skip", {"action": "store_true", "help": "lead only, needs --reason: skip the per-criterion evidence check"}))
        add(t, "split", task_split, ("id", {"type": int}), ("--title", {"required": True}), ("--branch", {"required": True}),
            ("--criteria", {"help": "acceptance criteria; @file reads a file (default: 'split from #N: <title>')"}),
            ("--after", {"nargs": "?", "const": "", "help": "also record that the new task runs after this task id (bare: after N)"}))
        add(t, "backfill-merge-commits", task_backfill_merge_commits)
        add(t, "log", task_log, ("id", {"type": int}), ("text", {}))
        add(t, "link", task_link, ("id", {"type": int}), ("kind", {"choices": ["decision", "tip", "upstream", "after"]}),
            ("target", {}))

        paging(add(t, "ready", task_ready))
        add(t, "path", task_path, ("goal", {"type": int}))
        add(t, "unlink", task_unlink, ("id", {"type": int}), ("kind", {"choices": ["after", "upstream"]}), ("target", {"help": "task id (after) or issue number/url (upstream)"}))

        add(t, "reject-approach", task_reject_approach, ("id", {"type": int}), ("--reason", {}))

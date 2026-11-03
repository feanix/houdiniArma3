"""report-upstream."""
from .. import db, messages, upstream
from ._common import out, add, grp, client_cmd, text_arg as _text_arg


def report_upstream(c, a):
    r = upstream.report(c, a.actor, a.title, _text_arg(a.body), _text_arg(a.repro), a.log, a.workaround, a.task, a.dry_run, symptom=_text_arg(a.symptom), kind=a.kind, severity=a.severity)

    def show(r):
        print(f"repo: {r['repo']}\ntitle: {r['title']}\nduplicate search: {r['dup_query']}\n\n{r['body']}")
        if r["dry_run"]:
            print("\n(dry run: nothing filed)")
            return
        if r.get("queued"):
            print(f"gh failed: {r['error']}\nreport QUEUED locally ({r['pending']} pending); nothing filed yet. "
                  + client_cmd("Retry: python -m aesir upstream retry (upstream watch also retries)"))
            return
        for d in r["duplicates"]:
            print(f"possible duplicate: #{d['number']} [{d['state']}] {d['title']} {d['url']}")
        print(f"filed: {r['url']}" + (f" (tip T{r['tip']} linked)" if r.get("tip") else ""))
        print(messages.for_caller(f'notify: SendMessage {r["notify"]} "{r["url"]}"'))
    out(a, r, show)


def sync_milestones_cmd(a):
    """`upstream sync-milestones [--apply]` (#228). Part of the PRE_DB `upstream` group, so it opens the db itself."""
    c = db.connect(db.require_project(a.db), allow_migrate=True if a.allow_migrate else None)
    try:
        acts = upstream.sync_milestones(c, db.project_root(), apply=a.apply)
    finally:
        c.close()
    for x in acts:
        print(("done: " if a.apply else "would: ") + x)
    if not acts:
        print("milestones already match the release plan")
    elif not a.apply:
        print("(dry run: nothing written; --apply writes)")


def register(sub):
        add(grp(sub, "upstream"), "sync-milestones", sync_milestones_cmd,
            ("--apply", {"action": "store_true", "help": "write to GitHub (default: list what would change)"}))
        p = add(sub, "report-upstream", report_upstream, ("--title", {"required": True}), ("--body", {"required": True, "help": "@file reads a file"}),
            ("--symptom", {"default": "", "help": "exact error text (searchable by others); @file reads a file"}),
            ("--kind", {"default": "bug", "choices": upstream.KINDS,
                        "help": "label: bug (default), suggestion (for the project), internal (Aesir-internal bug)"}),
            ("--severity", {"choices": upstream.SEVERITIES, "help": "required for bug and internal: " + upstream.SEVERITY_SCALE}),
            ("--repro", {"default": ""}), ("--log", {"help": "log file; last 200 lines are attached"}),
            ("--workaround", {"default": "", "help": "included in the issue; also stored as a linked tip in this db"}),
            ("--task", {"type": int, "help": "also link this task to the issue"}),
            ("--dry-run", {"action": "store_true", "help": "print the issue and duplicate query; call nothing"}))
        orig = p.get_default("func")

        def checked(c, a):
            # Parser-level usage error (exit 2) so the scale is printed where the agent is already looking.
            if a.kind in upstream.SEVERITY_REQUIRED_KINDS and not a.severity:
                p.error(f"--severity is required for --kind {a.kind}. Scale: {upstream.SEVERITY_SCALE}")
            return orig(c, a)
        p.set_defaults(func=checked)

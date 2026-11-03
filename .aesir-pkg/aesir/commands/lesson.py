"""lesson add/list/review/send."""
from .. import lessons, messages
from ._common import out, add, grp, text_arg as _text_arg


def lesson_add(c, a):
    """Record one lesson; the number is echoed so the caller can cite it as evidence elsewhere."""
    n = lessons.add(c, a.actor, a.scope, _text_arg(a.what), _text_arg(a.lesson), a.evidence)
    print(f"lesson L{n} added")


def lesson_list(c, a):
    """Show entries without changing them, so a peek never counts as an owner review."""
    out(a, lessons.list_(c, a.scope, a.actor), lambda rows: [print(lessons.render(b)) for b in rows] or (not rows and print("no lessons")))


def lesson_review(c, a):
    """Print unreviewed lessons grouped by scope for the owner session at project close, then mark them reviewed."""
    def show(g):
        """Human output for review; skips empty scopes so the owner only sees what needs a decision."""
        if not any(g.values()):
            print("no unreviewed lessons")
        for s, rows in g.items():
            if rows:
                print(f"== scope {s} ({len(rows)}) ==")
                for b in rows:
                    print(lessons.render(b))
        print("(marked reviewed)")
    out(a, lessons.review(c, a.actor), show)


def lesson_send(c, a):
    """File all unsent aesir-scope lessons as one upstream report; a failed gh call queues it rather than losing it."""
    r = lessons.send(c, a.actor, a.dry_run)

    def show(r):
        """Human output for send: nothing to send, dry-run preview, queued fallback, or the filed link."""
        if r is None:
            print("no unsent aesir-scope lessons")
        elif r["dry_run"]:
            print(f"repo: {r['repo']}\ntitle: {r['title']}\n\n{r['body']}\n\n(dry run: nothing filed)")
        elif r.get("queued"):
            print(f"gh failed: {r['error']}\nreport QUEUED locally; {r['count']} lesson(s) marked as queued (not resent)")
        else:
            print(f"filed: {r['url']} ({r['count']} lesson(s))")
            print(messages.for_caller(f'notify: SendMessage {r["notify"]} "{r["url"]}"'))
    out(a, r, show)


def register(sub):
        """Wire the lesson subcommands into the CLI parser."""
        g = grp(sub, "lesson")
        add(g, "add", lesson_add, ("what", {"help": "what happened; @file reads a file"}),
            ("--scope", {"required": True, "choices": lessons.SCOPES, "help": "aesir: sent upstream; project: owner review only"}),
            ("--lesson", {"required": True}), ("--evidence", {"default": "", "help": "ids, e.g. 'D133 T33 aesir-reports#54'"}))
        add(g, "list", lesson_list, ("--scope", {"choices": lessons.SCOPES}))
        add(g, "review", lesson_review)
        add(g, "send", lesson_send, ("--dry-run", {"action": "store_true"}))

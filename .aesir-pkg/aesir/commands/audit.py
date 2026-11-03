"""finding add/list/set/baseline/import/digest and audit plan/run/benchmark/show/shard/done/list (task #67, #93). See docs/audit-findings.md."""
import json
import sys
import time
from pathlib import Path

from .. import audit, benchmark, db, rank, service as S
from ..service import AesirError
from ._common import add, grp, hint, line, out, out_list, paging, require_dir

LIST = "python -m aesir finding list"


def _flt(a):
    return dict(file=a.file, severity=a.severity, rule=a.rule, source=a.source, status=a.status,
                since=a.since, area=a.area)


def _flags(a):
    return "".join(f" --{k.replace('_', '-')} {v}" for k, v in _flt(a).items() if v not in (None, ""))


def _row(f):
    tail = f"  -> task {f['task_id']}" if f["task_id"] else ""
    note = f"  note: {f['note']}" if f.get("note") else ""
    return f"F{f['id']} {f['severity']:<6} {f['file']}:{f['line']}  {f['rule']} [{f['source']}] {f['status']}{tail}  {f['message'][:100]}{note}"


def finding_add(c, a):
    fid = audit.add_finding(c, a.actor, a.file, a.line, a.severity, a.rule, a.message, a.source, a.audit)
    print(f"finding {fid} recorded")


def finding_list(c, a):
    rows = audit.list_findings(c, **_flt(a))
    cmd = LIST + _flags(a)
    out_list(a, rows, lambda rs: [print(_row(f)) for f in rs] or (not rs and print("no findings")), cmd,
             "python -m aesir finding digest" + _flags(a), "python -m aesir finding set <id> --status fixed --task <n>")


def finding_digest(c, a):
    rows = audit.digest(c, a.by, a.depth, **_flt(a))
    total = sum(g["count"] for g in rows)
    if not a.json:
        print(f"{total} findings in {len(rows)} groups by {a.by}, worst first")
    out_list(a, rows, lambda gs: [line(f"{g['count']:>5}", f"{g['worst']:<6}", g["key"]) for g in gs],
             "python -m aesir finding digest --by " + a.by + _flags(a),
             f"python -m aesir finding list --{a.by} <group>" + _flags(a))


def finding_set(c, a):
    audit.set_finding(c, a.actor, a.id, a.status, a.task, a.note)
    print(f"finding {a.id} updated")


def finding_rank(c, a):
    root = db.project_root()
    n = rank.load_config(root)["top_n"]
    rows = rank.rank(c, root)
    out_list(a, rows, lambda rs: [print(x) for x in rank.render(rs, n)] if rs else print("nothing to rank"),
             "python -m aesir finding rank", "python -m aesir finding pass <file>")


def finding_pass(c, a):
    root = db.project_root()
    if a.done:
        sha = rank.record_review(c, a.actor, a.file, root)
        print(f"review of {a.file} recorded at {sha[:10]}; its churn is now 0")
        return
    tid, created = rank.start_pass(c, a.actor, root, a.file, a.requester, a.reason)
    print(f"review pass task {tid} " + ("created" if created else "already open; no duplicate created"))
    hint(a, f"python -m aesir task show {tid}")


def finding_baseline(c, a):
    if not a.rule:
        raise AesirError("finding baseline needs --rule <rule> (baseline one newly adopted check at a time)")
    n = audit.baseline(c, a.actor, a.rule, **{k: v for k, v in _flt(a).items() if k != "rule"})
    print(f"{n} open {a.rule} findings baselined as expected (status baselined)")
    hint(a, "python -m aesir finding digest --status baselined --by rule")


def finding_import(c, a):
    lines = sys.stdin.read().splitlines() if a.path == "-" else open(a.path, encoding="utf-8").read().splitlines()
    aid, n = audit.import_findings(c, a.actor, lines, a.source or "", audit_id=a.audit)
    print(f"audit {aid}: {n['new']} new, {n['seen']} seen again, {n['reopened']} reopened")
    hint(a, f"python -m aesir finding digest --since {aid - 1}", f"python -m aesir finding list --since {aid - 1}")


def _pre_row(f):
    """One finding as a display row; shared by the new and the pre-existing lists of audit run --dir."""
    return f"{f['severity']:<6} {f['file']}:{f['line']}  {f['rule']} [{f['source']}]  {f['message'][:100]}"


def audit_run_dir(c, a):
    """audit run --dir D [--task N]: audit a branch checkout against main's findings; only the new ones are shown and
    (with --task) logged on the task. Exit 1 on new findings, 2 when a toolkit failed. Never writes main's findings."""
    d = require_dir(a.dir)
    if a.task is not None:
        S.get_task(c, a.task)
    ms = a.min_severity
    t0 = time.monotonic()
    pre = []
    new, errors, code = audit.new_vs_main(c, db.project_root(), d, pre, S.checkout_diff_base(c, d, a.task))
    floor = 0 if ms in (None, "any") else audit.SEVERITIES.index(ms)
    blocking = [f for f in new if audit.SEVERITIES.index(f["severity"]) >= floor]
    if not errors:
        code = 1 if blocking else 0
    rows = [_pre_row(f) + ("" if f in blocking else "  (below threshold, not blocking)") for f in new]
    head = f"audit of {d}: {len(new)} new findings vs main" + (f", {len(blocking)} at or above {ms}" if floor else "") + (f", {len(errors)} toolkit(s) FAILED" if errors else "") + f" ({time.monotonic() - t0:.1f}s)"
    old = ([f"pre-existing on main (not yet recorded; in files this branch does not change, not failing): {len(pre)}"]
           + ["  " + _pre_row(f) for f in sorted(pre, key=lambda f: (f["file"], f["line"]))]) if pre else []
    fails = [f"{n}: FAILED {e}" for n, e in errors]
    print("\n".join([head] + ["  " + r for r in rows] + ["  " + x for x in fails] + old))
    if a.task is not None:
        S.log_task(c, a.actor, a.task, "\n".join([head] + rows + fails + old))
    if code:
        raise SystemExit(code)


def audit_run(c, a):
    if a.dir:
        return audit_run_dir(c, a)
    if a.task is not None:
        raise AesirError("--task goes with --dir (it records the new findings of a branch audit on the task)")
    r = audit.run_toolkits(c, a.actor, db.project_root())
    if r is None:
        print("no audit toolkits enabled ([audit] toolkits in aesir.toml); nothing run")
        return
    aid, results = r
    failed = [n for n, counts, err in results if err]
    for name, counts, err in results:
        print(f"{name}: " + (f"FAILED {err}" if err else f"{counts['new']} new, {counts['seen']} seen again, "
                                                             f"{counts['reopened']} reopened"))
    if aid:
        hint(a, f"python -m aesir finding digest --since {aid - 1}", "python -m aesir finding digest --by rule")
    if failed:
        raise S.AesirError("toolkit failed: " + ", ".join(failed))


def audit_benchmark(c, a):
    """Score the Rust toolkit against a recorded answer key (manual: needs network and cargo; not in the gate)."""
    try:
        res = benchmark.run(a.name, db.project_root(), a.source)
    except benchmark.Skipped as e:
        print(f"SKIPPED: {e}")
        return
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    out(a, res, lambda r: print("\n".join(benchmark.render(r))))


def audit_plan(c, a):
    aid, skipped = audit.plan(c, a.actor, db.project_root(), a.agents, a.exclude, a.note)
    print(f"audit {aid} planned ({skipped} tracked files skipped: binary or excluded)")
    audit_show(c, a, aid)


def audit_show(c, a, aid=None):
    aid = aid if aid is not None else a.id
    au = audit.get_audit(c, aid)
    shards = [dict(r) for r in c.execute("SELECT n, weight, done, files FROM audit_shards WHERE audit_id=? ORDER BY n", (aid,))]
    nf = c.execute("SELECT COUNT(*) FROM findings WHERE first_audit=?", (aid,)).fetchone()[0]
    data = {"audit": dict(au), "new_findings": nf,
            "shards": [{"n": s["n"], "weight": s["weight"], "done": s["done"], "files": len(json.loads(s["files"]))}
                       for s in shards]}

    def render(d):
        print(f"audit {aid} {au['kind']} {au['ts']} {au['actor']}: {au['note']}  ({nf} findings first seen here)")
        for s in d["shards"]:
            print(f"  shard {s['n']}: {s['files']} files, {s['weight']} lines, " + (f"done {s['done']}" if s["done"] else "open"))
    out(a, data, render)
    if shards:
        hint(a, f"python -m aesir audit shard {aid} 1")


def audit_shard(c, a):
    s = audit.get_shard(c, a.id, a.n)

    def render(d):
        print(f"audit {a.id} shard {a.n}: {len(d['files'])} files, {d['weight']} lines. Checklist:")
        for i, item in enumerate(audit.CHECKLIST, 1):
            print(f"  [ ] {i}. {item.replace('<audit>', str(a.id)).replace('<shard>', str(a.n))}")
        print("Files:")
        for f in d["files"]:
            print("  " + f)
    out(a, {**s, "checklist": list(audit.CHECKLIST)}, render)


def audit_done(c, a):
    audit.mark_shard_done(c, a.actor, a.id, a.n)
    print(f"audit {a.id} shard {a.n} done")


def audit_list(c, a):
    rows = [dict(r) for r in c.execute(
        "SELECT a.*, (SELECT COUNT(*) FROM findings f WHERE f.first_audit=a.id) AS new_findings "
        "FROM audits a ORDER BY a.id DESC")]
    out_list(a, rows, lambda rs: [line(f"A{r['id']}", r["kind"], r["ts"], f"{r['new_findings']} new", r["note"]) for r in rs]
             or (not rs and print("no audits")), "python -m aesir audit list", "python -m aesir audit show <id>")


def _filters(p):
    for flag, kw in (("--file", {}), ("--area", {"help": "path prefix (a directory)"}),
                     ("--severity", {"choices": list(audit.SEVERITIES), "help": "this severity and worse"}),
                     ("--rule", {}), ("--source", {"help": "toolkit name"}),
                     ("--status", {"choices": list(audit.STATUSES)}),
                     ("--since", {"type": int, "help": "only findings first seen after this audit id"})):
        p.add_argument(flag, **{"default": None, **kw})
    return p


def register(sub):
    fp = grp(sub, "finding")
    add(fp, "add", finding_add, ("file", {}), ("line", {"type": int}),
        ("--severity", {"required": True, "choices": list(audit.SEVERITIES)}), ("--rule", {"required": True}),
        ("--message", {"default": ""}), ("--source", {"default": "manual"}), ("--audit", {"type": int}))
    paging(_filters(add(fp, "list", finding_list)))
    paging(_filters(add(fp, "digest", finding_digest,
                            ("--by", {"default": "area", "choices": ["area", "severity", "file", "rule", "source"]}),
                            ("--depth", {"type": int, "default": 1, "help": "path segments per area"}))))
    add(fp, "set", finding_set, ("id", {"type": int}), ("--status", {"choices": list(audit.STATUSES)}),
        ("--task", {"type": int, "help": "linked fix task"}),
        ("--note", {"help": "triage reason (e.g. why wontfix); shown by finding list"}))
    paging(add(fp, "rank", finding_rank))
    add(fp, "pass", finding_pass, ("file", {}),
        ("--requester", {"help": "default advisor"}),
        ("--reason", {"help": "needed once top_n passes were already started since the last release"}),
        ("--done", {"action": "store_true", "help": "record the file's review now (commit and date) instead of creating a task"}))
    _filters(add(fp, "baseline", finding_baseline))
    add(fp, "import", finding_import, ("path", {"help": "JSON-lines file, or - for stdin"}),
        ("--source", {"help": "toolkit name for lines without one"}), ("--audit", {"type": int}))
    ap = grp(sub, "audit")
    add(ap, "run", audit_run,
        ("--dir", {"help": "audit this checkout (e.g. an unmerged task worktree) with main's config; report only findings new vs main, import nothing"}),
        ("--task", {"type": int, "help": "with --dir: log the new findings on this task"}),
        ("--min-severity", {"choices": ["any", *audit.SEVERITIES], "help": "with --dir: only new findings at or above this fail (exit 1); the rest are listed"}))
    add(ap, "benchmark", audit_benchmark, ("name", {"help": "benchmark under tests/benchmarks, e.g. tetris-054"}),
        ("--source", {"help": "score this checkout instead of cloning the keyed tag"}),
        ("--out", {"help": "also write the result as JSON to this file"}))
    add(ap, "plan", audit_plan, ("--agents", {"type": int, "default": 4}),
        ("--exclude", {"action": "append", "default": [], "help": "glob; repeatable"}), ("--note", {"default": ""}))
    add(ap, "show", audit_show, ("id", {"type": int}))
    add(ap, "shard", audit_shard, ("id", {"type": int}), ("n", {"type": int}))
    add(ap, "done", audit_done, ("id", {"type": int}), ("n", {"type": int}))
    paging(add(ap, "list", audit_list))

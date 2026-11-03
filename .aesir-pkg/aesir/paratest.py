"""Parallel unittest runner (task #99), stdlib only. Usable by any project's gate:
    python -m aesir.paratest [-s tests] [-j N]
Splits the discovered suite into units (one TestCase class each), runs each unit in its own worker process with its own
temp dir, and prints one unittest-shaped summary: counts first, then failing test names with tracebacks, then OK/FAILED.
Exit 1 if any worker failed or crashed, or if the units ran fewer tests than discovery found.
Workers wanted: -j, else aesir.toml [tests] workers, else cpu count - 2 (min 1); the machine worker budget
(aesir/budget.py, [machine] workers) grants up to that many, fewer while other gates hold workers. A TestCase class with `serial = True` runs
alone after the parallel phase (for tests that cannot share the machine with other tests)."""
import argparse
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from . import proc

CRASH_TAIL = 2000


def _tests(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _tests(t)
        else:
            yield t


def list_units(start, top):
    """{unit name: {"n": test count, "serial": bool}} for every TestCase class discovered under `start`."""
    sys.path.insert(0, top)
    units = {}
    for t in _tests(unittest.defaultTestLoader.discover(start, top_level_dir=top)):
        cls = type(t)
        name = f"{cls.__module__}.{cls.__qualname__}"
        u = units.setdefault(name, {"n": 0, "ids": set(), "serial": bool(getattr(cls, "serial", False))})
        u["n"] += 1
        u["ids"].add(t.id())
    for u in units.values():  # a base class imported into other test modules is collected (and run) once per module
        u["reps"] = u["n"] // len(u["ids"])
        del u["ids"]
    return units


def run_unit(unit, top, out_path, reps=1):
    """Worker body: run one unit, write its result as JSON (never touches stdout, which tests may fill)."""
    sys.path.insert(0, top)
    stream = io.StringIO()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(unit) for _ in range(reps))
    res = unittest.TextTestRunner(stream=stream, verbosity=0, buffer=True).run(suite)
    bad = [(kind, t.id(), tb) for kind, items in (("FAIL", res.failures), ("ERROR", res.errors)) for t, tb in items]
    bad += [("UNEXPECTED SUCCESS", t.id(), "") for t in res.unexpectedSuccesses]
    Path(out_path).write_text(json.dumps({
        "run": res.testsRun, "skipped": [[t.id(), why] for t, why in res.skipped], "bad": bad}), encoding="utf-8")


def worker_count(arg, cwd):
    if arg:
        return max(1, arg)
    try:
        cfg = tomllib.loads((Path(cwd) / "aesir.toml").read_text(encoding="utf-8"))
        n = cfg.get("tests", {}).get("workers")
    except (OSError, tomllib.TOMLDecodeError):
        n = None
    return max(1, int(n)) if n else max(1, (os.cpu_count() or 4) - 2)


def _run_in_worker(unit, reps, slot_tmp, top, real_temp):
    """Spawn one worker for `unit` with TEMP/TMP/TMPDIR in its slot's own dir; returns (result dict or None, output)."""
    out = Path(slot_tmp, "result.json")
    out.unlink(missing_ok=True)
    env = {**os.environ, "TEMP": slot_tmp, "TMP": slot_tmp, "TMPDIR": slot_tmp,
           "AESIR_REAL_TEMP": real_temp}  # the machine temp, which tests/sandbox.py guards (#111)
    p = proc.run([sys.executable, "-m", "aesir.paratest", "--worker", unit, "--top", top, "--out", str(out),
                        "--reps", str(reps)],
                       cwd=top, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return json.loads(out.read_text(encoding="utf-8")), ""
    except (OSError, ValueError):
        return None, f"worker exit {p.returncode}\n{(p.stdout + p.stderr)[-CRASH_TAIL:]}"


def run_all(start, top, jobs):
    """Run the whole suite; returns (ok, summary text)."""
    t0 = time.monotonic()
    real_temp = os.environ.get("AESIR_REAL_TEMP") or tempfile.gettempdir()  # before discovery: importing tests installs the sandbox, which strips AESIR_*
    units = list_units(start, top)
    want = sum(u["n"] for u in units.values())
    results, crashes = {}, {}
    lock = threading.Lock()
    base = tempfile.mkdtemp(prefix="aesir-para-")

    def drain(q, slot):
        tmp = Path(base, f"w{slot}")
        tmp.mkdir(exist_ok=True)
        while True:
            try:
                unit = q.get_nowait()
            except queue.Empty:
                return
            res, crash = _run_in_worker(unit, units[unit]["reps"], str(tmp), top, real_temp)
            with lock:
                if res is None:
                    crashes[unit] = crash
                else:
                    results[unit] = res

    try:
        for serial in (False, True):
            order = sorted((k for k, u in units.items() if u["serial"] == serial), key=lambda k: -units[k]["n"])
            q = queue.Queue()
            for k in order:
                q.put(k)
            ts = [threading.Thread(target=drain, args=(q, i)) for i in range(1 if serial else jobs)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    ran = sum(r["run"] for r in results.values())
    skipped = [s for r in results.values() for s in r["skipped"]]
    bad = [b for r in results.values() for b in r["bad"]]
    lines = []
    for kind, tid, tb in sorted(bad, key=lambda b: b[1]):
        lines.append(f"{'=' * 70}\n{kind}: {tid}\n{'-' * 70}\n{tb.rstrip()}\n")
    for unit, text in sorted(crashes.items()):
        lines.append(f"{'=' * 70}\nCRASH: worker for {unit}\n{'-' * 70}\n{text.rstrip()}\n")
    for tid, why in sorted(skipped):  # named so a serial-vs-parallel skip comparison is a diff, not a count
        lines.append(f"skipped: {tid} ({why})\n")
    if ran != want and not crashes:  # a unit that ran fewer tests than discovery listed: coverage lost
        lines.append(f"COVERAGE: discovered {want} tests, ran {ran}\n")
    ok = not bad and not crashes and ran == want
    head = f"{jobs} workers, {len(units)} units, discovered {want} tests"
    tail = f"Ran {ran} tests in {time.monotonic() - t0:.1f}s\n\n"
    tail += "OK" if ok else f"FAILED (failures={len(bad)}, crashed units={len(crashes)})"
    if skipped:
        tail += f" (skipped={len(skipped)})"
    return ok, head + "\n" + "".join(lines) + tail + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m aesir.paratest")
    ap.add_argument("-s", "--start", default="tests")
    ap.add_argument("-j", "--jobs", type=int, default=0)
    ap.add_argument("--top", default=os.getcwd())
    ap.add_argument("--worker")
    ap.add_argument("--out")
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args(argv)
    top = str(Path(a.top).resolve())
    if a.worker:
        run_unit(a.worker, top, a.out, a.reps)
        return 0
    from . import budget  # the machine-wide worker budget (#104): what is leased, not cpu-2, bounds the processes
    with budget.lease(worker_count(a.jobs, top), root=top, say=lambda m: print(m, flush=True)) as n:
        ok, text = run_all(a.start, top, n)
    sys.stdout.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

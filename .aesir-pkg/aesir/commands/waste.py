"""waste: tokens per verified task, rework, re-read, duplication and per-agent/role overhead (task #35)."""
from .. import db, metrics
from ._common import out, add


def waste_cmd(c, a):
    cfg = metrics.load_config(db.project_root())
    out(a, metrics.waste_report(c, metrics.norm_since(a.since), cfg),
        lambda d: metrics.render_waste(d, agents=a.agents))


def register(sub):
    add(sub, "waste", waste_cmd, ("--since", {"help": "YYYY-MM-DD or full timestamp (UTC)"}),
        ("--agents", {"action": "store_true", "help": "also list every agent's overhead"}))

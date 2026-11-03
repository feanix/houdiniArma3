"""metrics and stage-report."""
from .. import db, metrics
from ._common import out, add


def metrics_cmd(c, a):
    out(a, metrics.metrics(c, metrics.norm_since(a.since), metrics.load_config(db.project_root())), metrics.render_metrics)


def stage_report_cmd(c, a):
    out(a, metrics.stage_report(c, metrics.norm_since(a.since), metrics.load_config(db.project_root())), metrics.render_stage)


def register(sub):
        add(sub, "metrics", metrics_cmd, ("--since", {"help": "YYYY-MM-DD or full timestamp (UTC)"}))
        add(sub, "stage-report", stage_report_cmd, ("--since", {"help": "YYYY-MM-DD or full timestamp (UTC); default: first event"}))

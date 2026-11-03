"""track list/show."""
from .. import db, tracks
from ._common import out, line, add, grp


def track_list(c, a):
    out(a, list(tracks.load(db.project_root()).values()),
        lambda ts: [line(t["name"], t["base"], t["gates"] or "-", ", ".join(t["scope"])) for t in ts]
        or print("no tracks"))


def track_show(c, a):
    def render(t):
        for k in ("name", "base", "gates", "model"):
            if t[k]:
                print(f"{k}: {t[k]}")
        print("scope:", ", ".join(t["scope"]) or "(none)")
        print("context:", ", ".join(t["context"]) or "(none)")
    out(a, tracks.get(db.project_root(), a.name), render)


def register(sub):
        tr = grp(sub, "track")
        add(tr, "list", track_list)
        add(tr, "show", track_show, ("name", {}))

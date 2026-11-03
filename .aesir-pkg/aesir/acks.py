"""Anomaly ack episodes, the one rule (#190): replayed from the 'anomaly' events, imports nothing above the db.

An episode starts when an anomaly key is first noted and ends on 'clear'. `status ack <key>` records a sig and an
optional `until` event id; the ack holds while the anomaly's current sig equals it and the latest event id is below
`until`. Used by status (reconcile, unacked_stalls) and by messages (the unanswered-ask count in the hook).
"""
import json


def episodes(c, like=None):
    """Per anomaly key: {seen, escalated, ack: {sig, until, actor, ts} | None} of the open episode. `like`: SQL LIKE
    filter on the key (e.g. 'unanswered_ask:%')."""
    sql, args = "SELECT ts, actor, entity_id, action, payload FROM events WHERE entity='anomaly'", ()
    if like:
        sql, args = sql + " AND entity_id LIKE ?", (like,)
    st = {}
    for r in c.execute(sql + " ORDER BY id", args):
        k, a = r["entity_id"], r["action"]
        if a == "clear":
            st.pop(k, None)
            continue
        e = st.setdefault(k, {"seen": r["ts"], "escalated": False, "ack": None})
        if a == "escalate":
            e["escalated"] = True
        elif a == "ack":
            p = json.loads(r["payload"] or "{}")
            e["ack"] = {"sig": p.get("sig"), "until": p.get("until"), "actor": r["actor"], "ts": r["ts"]}
    return st


def last_event_id(c):
    return c.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def holds(ack, ident, last):
    """Does `ack` (an episode's ack dict or None) cover an anomaly whose current sig is `ident`, at event id `last`?"""
    return bool(ack and ack["sig"] == ident and (not ack["until"] or last < ack["until"]))

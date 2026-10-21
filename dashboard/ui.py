"""Shared dashboard helpers. The dashboard owns NO authoritative data: everything is read from
the stations over HTTP on every rerun, and every write is an explicit button/form action."""
import os

import pandas as pd
import streamlit as st

from dcn.client import NODE_LABELS, Cluster
from dcn.config import parse_peers
from dcn.controllers import DockerSocketController

DEFAULT_STATIONS = ("alpha=http://localhost:8001,bravo=http://localhost:8002,"
                    "charlie=http://localhost:8003,delta=http://localhost:8004")
SEV = {"low": "🟢 low", "medium": "🟡 medium", "high": "🟠 high", "critical": "🔴 critical"}
PRI = {"low": "🟢 low", "medium": "🟡 medium", "high": "🟠 high", "urgent": "🔴 urgent"}
STATUS_ICON = {"pending": "⏳ pending", "acknowledged": "👀 acknowledged",
               "fulfilled": "✅ fulfilled", "cancelled": "🚫 cancelled"}

CSS = """
<style>
.block-container {padding-top: 1.6rem; max-width: 1400px;}
.card {border:1px solid rgba(128,128,128,.35); border-radius:12px; padding:14px 16px; height:100%;}
.card h4 {margin:0 0 2px 0;}
.pill {display:inline-block; padding:1px 10px; border-radius:999px; font-size:.78rem; font-weight:600;}
.on {background:#1b873f22; color:#1b873f; border:1px solid #1b873f66;}
.off {background:#c0392b22; color:#c0392b; border:1px solid #c0392b66;}
.muted {opacity:.7; font-size:.85rem;}
.small {font-size:.85rem;}
</style>
"""


@st.cache_resource
def get_cluster() -> Cluster:
    return Cluster(parse_peers(os.environ.get("STATIONS", DEFAULT_STATIONS)))


@st.cache_resource
def get_controller():
    """Docker control is only available when the Docker socket is mounted into the dashboard."""
    return DockerSocketController() if DockerSocketController.available() else None


def label(node):
    return NODE_LABELS.get(node, (node.title(), ""))


def station_title(node):
    n, z = label(node)
    return f"{n} — {z}"


def take_snapshot(c: Cluster):
    status = c.status_all()
    matrix, online = c.presence_matrix()
    return {"status": status, "matrix": matrix, "online": online, "blocked": c.blocked_pairs()}


def flash(kind, msg):
    st.session_state.setdefault("_flash", []).append((kind, msg))


def show_flash():
    for kind, msg in st.session_state.pop("_flash", []):
        {"ok": st.success, "err": st.error, "warn": st.warning}.get(kind, st.info)(msg)


def explain(title, body):
    with st.expander(f"💡 What this demonstrates — {title}", expanded=False):
        st.markdown(body)


def pill(online):
    return f'<span class="pill {"on" if online else "off"}">{"ONLINE" if online else "OFFLINE"}</span>'


def short_ts(ts):
    return ts[11:23] if ts else "—"


def record_rows(snap, node, kind):
    """Rows as seen by ONE station's own database (local vs replicated)."""
    rows = []
    for rid, per in snap["matrix"].items():
        rec = per.get(node)
        if not rec or rec["deleted"] or rec["kind"] != kind:
            continue
        holders = [n for n, r in per.items() if r and r["digest"] == rec["digest"]]
        local = rec["origin"] == node
        p = rec["payload"]
        base = {"ID": rid,
                "Storage at this station": "💾 LOCAL (created here)" if local
                else f"🔁 REPLICATED from {rec['received_from'] or rec['origin']}",
                "Origin": rec["origin"], "Version": rec["version"], "Last writer": rec["writer"],
                "Updated": short_ts(rec["updated_at"]), "Created": short_ts(rec["created_at"]),
                "Same copy on": f"{len(holders)}/{len(snap['matrix'][rid])} stations"}
        if kind == "incident":
            base.update({"Title": p["title"], "Category": p["category"], "Severity": SEV[p["severity"]],
                         "Location": p["location"], "Reporting station": p["reporting_station"],
                         "Status": p["status"]})
        else:
            base.update({"Resource": p["resource_type"], "Qty": p["quantity"],
                         "Priority": PRI[p["priority"]], "Status": STATUS_ICON[p["status"]],
                         "Requesting station": p["requesting_station"]})
        rows.append(base)
    first = ["ID", "Title", "Resource", "Qty", "Severity", "Priority", "Status",
             "Storage at this station", "Version", "Same copy on"]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    cols = [c for c in first if c in df.columns] + [c for c in df.columns if c not in first]
    return df[cols]


def network_matrix(snap, kind):
    """One row per record, one column per station: ✓ vX / ✗ missing / offline."""
    rows = []
    for rid, per in snap["matrix"].items():
        any_rec = next((r for r in per.values() if r), None)
        if not any_rec or any_rec["kind"] != kind:
            continue
        row = {"ID": rid}
        for n, r in per.items():
            if not snap["online"][n]:
                row[n] = "⚫ offline"
            elif r is None:
                row[n] = "✗ missing"
            else:
                row[n] = f"✓ v{r['version']}{' 🪦' if r['deleted'] else ''}"
        rows.append(row)
    return pd.DataFrame(rows)


def events_frame(c: Cluster, limit=300):
    ev = c.events_all(limit)
    rows = []
    for n, es in ev.items():
        for e in es or []:
            rows.append({"time": e["ts"], "station": n, "role": e["role"], "peer": e["peer"],
                         "outcome": e["outcome"], "sent": e["sent"], "received": e["received"],
                         "duplicates": e["duplicates"], "stale": e["stale"],
                         "conflicts": e["conflicts"], "invalid": e["invalid"],
                         "ms": e["duration_ms"], "detail": e["detail"]})
    df = pd.DataFrame(rows)
    return df.sort_values("time", ascending=False) if not df.empty else df


def tabs(names, key):
    """Tab-like selector that keeps its selection across reruns.

    st.tabs resets to the first tab whenever elements above it change (e.g. the partition banner
    appears after a button click); a keyed radio stored in session_state does not.
    """
    return st.radio("section", names, horizontal=True, key=key, label_visibility="collapsed")

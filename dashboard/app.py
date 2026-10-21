"""Streamlit dashboard: control panel + observability for the station network.

Run:  streamlit run dashboard/app.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st  # noqa: E402

from dashboard import ui  # noqa: E402
from dashboard.views import (architecture, concepts, incidents, network,  # noqa: E402
                             overview, resources, sync_explorer)

st.set_page_config(page_title="Disaster-Resilient Incident Coordination Network",
                   page_icon="🛰️", layout="wide")
st.markdown(ui.CSS, unsafe_allow_html=True)

PAGES = {
    "1 · System Overview": overview,
    "2 · Incident Management": incidents,
    "3 · Resource Coordination": resources,
    "4 · Network Simulator": network,
    "5 · Synchronization Explorer": sync_explorer,
    "6 · Distributed Systems Concepts": concepts,
    "7 · Architecture & Experiments": architecture,
}

cluster, controller = ui.get_cluster(), ui.get_controller()

with st.sidebar:
    st.title("🛰️ DCN")
    st.caption("Disaster-Resilient Distributed Incident Coordination Network")
    page = st.radio("Navigate", list(PAGES), label_visibility="collapsed")
    st.divider()
    st.button("🔄 Refresh view", use_container_width=True)
    st.caption("The dashboard only *reads* station state on refresh. Writes and sync happen "
               "only when you press an action button.")
    with st.expander("⚠️ Reset demo data"):
        sure = st.checkbox("Delete ALL records, logs and partitions on every station")
        if st.button("Reset all stations", disabled=not sure):
            cluster.heal_all()
            res = cluster.reset_all()
            ui.flash("ok" if all(res.values()) else "warn",
                     "Stations reset to a clean state." if all(res.values())
                     else f"Reset failed on: {[n for n, v in res.items() if not v]} (offline?)")
            st.rerun()
    st.divider()
    st.caption("Stations (from `STATIONS` env):")
    for n, u in cluster.urls.items():
        st.caption(f"• **{n}** → `{u}`")

st.title(page.split("· ", 1)[1])
ui.show_flash()
snap = ui.take_snapshot(cluster)
if snap["blocked"]:
    st.warning("🔌 **Network partition active:** " + ", ".join(f"{a} ✕ {b}" for a, b in sorted(snap["blocked"]))
               + " — stations on both sides are still running and accepting local writes.")
_down = [n for n, s in snap["status"].items() if not s["online"]]
if _down:
    st.error("💥 **Station(s) unavailable:** " + ", ".join(_down) + " — crashed/stopped (not a partition). "
             "Their SQLite volumes are intact.")
PAGES[page].render(cluster, snap, controller)

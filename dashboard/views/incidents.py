import streamlit as st

from dashboard import ui
from dcn import schemas


def render(c, snap, controller):
    online = [n for n in c.nodes if snap["online"][n]]
    if not online:
        st.error("No station is reachable.")
        return

    st.subheader("Report a new incident")
    with st.form("incident_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        station = c1.selectbox("Submit to station", online, format_func=ui.station_title)
        title = c1.text_input("Title", max_chars=120)
        category = c1.selectbox("Category", schemas.CATEGORIES)
        severity = c2.selectbox("Severity", schemas.SEVERITIES, index=2)
        location = c2.text_input("Location / zone", max_chars=120)
        desc = c2.text_area("Description", height=68, max_chars=2000)
        b1, b2 = st.columns(2)
        save = b1.form_submit_button("💾 Save at this station only", use_container_width=True)
        save_sync = b2.form_submit_button("💾 + 🔁 Save and synchronize network", type="primary",
                                          use_container_width=True)
    if save or save_sync:
        st_code, rec = c.create(station, "incident", {
            "title": title, "description": desc, "category": category, "severity": severity,
            "location": location, "reporting_station": station})
        if st_code != 201:
            ui.flash("err", f"Rejected by {station}: {rec.get('details') or rec}")
        else:
            msg = f"Saved **{rec['id']}** to {station}'s local SQLite database (version 1)."
            if save_sync:
                s = c.sync_network()
                ok = sum(1 for r in s["results"] if r["outcome"] == "success")
                bad = len(s["results"]) - ok
                msg += f" Synchronized: {ok} exchanges ok, {bad} failed/skipped, {s['duration_ms']} ms; " \
                       f"converged = {s['convergence']['converged']}."
            else:
                msg += " It is **not replicated** yet — other stations don't have it until a sync."
            ui.flash("ok", msg)
        st.rerun()

    ui.explain("local-first writes",
               "Submitting to a station writes to **that station's own SQLite file first**. Other stations only "
               "get it when a synchronization exchange happens. Use *Save at this station only*, then look at the "
               "'Same copy on' column to see the record is held by 1/4 stations.")

    st.divider()
    TABS = ["📍 View from one station", "🌐 Network-wide view", "✏️ Update / delete"]
    sel = ui.tabs(TABS, "inc_tab")
    if sel == TABS[0]:
        node = st.selectbox("Station database to inspect", online, format_func=ui.station_title, key="inc_view")
        df = ui.record_rows(snap, node, "incident")
        if df.empty:
            st.info(f"{node} holds no incidents.")
        else:
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.caption("💾 LOCAL = created at this station · 🔁 REPLICATED = arrived via synchronization. "
                       "'Same copy on' counts stations holding the identical version.")
    if sel == TABS[1]:
        df = ui.network_matrix(snap, "incident")
        if df.empty:
            st.info("No incidents anywhere.")
        else:
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.caption("✓ vN = station holds version N · ✗ missing = not replicated yet · ⚫ offline = can't be asked.")
        if st.button("🔁 Synchronize network now", key="inc_sync"):
            s = c.sync_network()
            ui.flash("ok" if s["convergence"]["converged"] else "warn",
                     f"Sync finished in {s['duration_ms']} ms ({s['rounds']} round(s)); "
                     f"converged = {s['convergence']['converged']}.")
            st.rerun()
    if sel == TABS[2]:
        ids = [i for i, per in snap["matrix"].items()
               if any(r and r["kind"] == "incident" and not r["deleted"] for r in per.values())]
        if not ids:
            st.info("Nothing to update yet.")
            return
        c1, c2, c3 = st.columns(3)
        rid = c1.selectbox("Incident", ids, key="inc_upd_id")
        holders = [n for n in online if snap["matrix"][rid].get(n) and not snap["matrix"][rid][n]["deleted"]]
        node = c2.selectbox("Apply at station", holders, format_func=ui.station_title, key="inc_upd_node")
        cur = snap["matrix"][rid][node]["payload"]
        sev = c3.selectbox("Severity", schemas.SEVERITIES, index=schemas.SEVERITIES.index(cur["severity"]))
        status = c3.selectbox("Status", schemas.INCIDENT_STATUSES, index=schemas.INCIDENT_STATUSES.index(cur["status"]))
        u1, u2 = st.columns(2)
        if u1.button("Update (version + 1)", key="inc_upd"):
            code, body = c.update(node, rid, {"severity": sev, "status": status})
            ui.flash("ok" if code == 200 else "err",
                     f"Updated at {node}: now v{body.get('version')}" if code == 200 else str(body))
            st.rerun()
        if u2.button("🪦 Delete (tombstone)", key="inc_del"):
            code, body = c.delete(node, rid)
            ui.flash("ok" if code == 200 else "err", "Tombstone written; it replicates so deleted data "
                     "is not resurrected." if code == 200 else str(body))
            st.rerun()

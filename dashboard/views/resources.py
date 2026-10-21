import streamlit as st

from dashboard import ui
from dcn import schemas


def render(c, snap, controller):
    online = [n for n in c.nodes if snap["online"][n]]
    if not online:
        st.error("No station is reachable.")
        return

    st.subheader("Submit a resource request")
    with st.form("request_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        station = c1.selectbox("Requesting station", online, format_func=ui.station_title)
        rtype = c1.selectbox("Resource type", schemas.RESOURCE_TYPES)
        qty = c1.number_input("Quantity", min_value=1, max_value=100000, value=50, step=1)
        prio = c2.selectbox("Priority", schemas.PRIORITIES, index=2)
        notes = c2.text_area("Notes (optional)", height=68, max_chars=2000)
        b1, b2 = st.columns(2)
        save = b1.form_submit_button("💾 Save at this station only", use_container_width=True)
        save_sync = b2.form_submit_button("💾 + 🔁 Save and synchronize network", type="primary",
                                          use_container_width=True)
    if save or save_sync:
        code, rec = c.create(station, "request", {
            "resource_type": rtype, "quantity": int(qty), "priority": prio, "notes": notes,
            "requesting_station": station})
        if code != 201:
            ui.flash("err", f"Rejected by {station}: {rec.get('details') or rec}")
        else:
            msg = f"Saved **{rec['id']}** at {station} (status: pending, version 1)."
            if save_sync:
                s = c.sync_network()
                msg += f" Sync took {s['duration_ms']} ms; converged = {s['convergence']['converged']}."
            ui.flash("ok", msg)
        st.rerun()

    st.divider()
    TABS = ["📍 View from one station", "🌐 Network-wide view", "🔀 Change status"]
    sel = ui.tabs(TABS, "req_tab")
    if sel == TABS[0]:
        node = st.selectbox("Station database to inspect", online, format_func=ui.station_title, key="req_view")
        df = ui.record_rows(snap, node, "request")
        st.dataframe(df, hide_index=True, use_container_width=True) if not df.empty \
            else st.info(f"{node} holds no requests.")
    if sel == TABS[1]:
        df = ui.network_matrix(snap, "request")
        st.dataframe(df, hide_index=True, use_container_width=True) if not df.empty \
            else st.info("No requests anywhere.")
        if st.button("🔁 Synchronize network now", key="req_sync"):
            s = c.sync_network()
            ui.flash("ok" if s["convergence"]["converged"] else "warn",
                     f"Sync finished in {s['duration_ms']} ms; converged = {s['convergence']['converged']}.")
            st.rerun()
    if sel == TABS[2]:
        ids = [i for i, per in snap["matrix"].items()
               if any(r and r["kind"] == "request" and not r["deleted"] for r in per.values())]
        if not ids:
            st.info("No requests yet.")
        else:
            c1, c2 = st.columns(2)
            rid = c1.selectbox("Request", ids, key="req_upd_id")
            holders = [n for n in online if snap["matrix"][rid].get(n)]
            node = c2.selectbox("Apply at station", holders, format_func=ui.station_title, key="req_upd_node")
            cur = snap["matrix"][rid][node]["payload"]["status"]
            allowed = sorted(schemas.TRANSITIONS[cur])
            st.write(f"Current status at {node}: **{ui.STATUS_ICON[cur]}** → allowed next: "
                     f"{', '.join(allowed) or '— (terminal state)'}")
            if allowed:
                nxt = st.selectbox("New status", allowed, key="req_next")
                if st.button("Apply transition", key="req_apply"):
                    code, body = c.update(node, rid, {"status": nxt})
                    ui.flash("ok" if code == 200 else "err",
                             f"{rid}: {cur} → {nxt} (v{body.get('version')})" if code == 200 else str(body))
                    st.rerun()
            st.caption("Illegal jumps (e.g. pending → fulfilled) are refused by the station with HTTP 409.")
    ui.explain("same rules as incidents",
               "Resource requests are versioned records stored and merged exactly like incidents. Status changes "
               "are *updates*: each bumps the version, so a later status can never be overwritten by an older one.")

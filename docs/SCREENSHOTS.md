# Screenshot checklist for the project report / evaluation

Capture from your own running system (`http://localhost:8501`) for the report. Sample images generated from the real Docker stack during development are in `docs/screenshots/` — they show what each should look like, but **take your own** for submission (they carry your machine's timestamps/ids).

Tip: browser zoom 80–90 %, window ≥ 1400 px wide, expand the sidebar.

| # | What to capture | How to reach it | Must be visible | Sample |
|---|---|---|---|---|
| 1 | **Overall system, all four stations online** | Page 1 after *Reset demo data* → create 2–3 records → *Synchronize* | 4 green station cards, `4 / 4` online, topology with all-green links, "converged ✅", fingerprints equal | `1_overview_all_online.png` |
| 2 | **Incident creation and replication** | Page 2 → submit to Alpha with *Save and synchronize network* → tab *Network-wide view* | New incident ID with `✓ v1` under alpha, bravo, charlie, delta; success banner with sync time | `2_incident_created_and_replicated.png` |
| 3 | **Partition: locally stored but not replicated** | Page 4 → *Isolate Alpha* → page 2 → *Save at this station only* → *Network-wide view* → *Synchronize* | "Network partition active" banner; the Alpha-only incident `✓ v1` at alpha and `✗ missing` at bravo/charlie/delta | `3_partition_local_not_replicated.png` |
| 4 | **Synchronization after reconnecting** | Page 4 → *Reconnect all links* → page 5 → *Synchronize whole network* | Results table with `success` rows, before/after replica comparison (✗ missing → ✓ v1), CONVERGED banner | `4_sync_after_reconnect.png` |
| 5 | **Node failure / restart recovery with metrics** | `docker compose stop station-bravo` (or Stop button) → page 1 (OFFLINE card + grey node) → start it → page 5 sync → page 7 *Live communication metrics* | 5a: red "Station(s) unavailable" banner, Bravo OFFLINE; 5b: successful vs **failed (`unreachable`)** exchanges, per-station counters | `5a_node_failure_overview.png`, `5b_recovery_sync_metrics.png` |

Optional extras: page 5 *Conflicts* after Scenario E (winner + metadata); page 6 any concept; page 7 *Architecture* diagram; `docker compose ps` showing five healthy services; `python -m unittest` output (45 tests OK).

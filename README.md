# Disaster-Resilient Distributed Incident Coordination Network

This is the readme file.

*A Dockerized, offline-capable distributed system for emergency incident reporting and resource coordination.*
Educational prototype for a Distributed Systems course — **not** a production emergency system.

Four independent **field stations** (separate containers, each with its own SQLite database in its own Docker volume) record incidents and resource requests locally, exchange data with reachable peers over real HTTP, and **converge after network partitions or node crashes**. A **Streamlit dashboard** (stateless — it stores no records) controls and observes the network.

```
Browser :8501 ─► Streamlit dashboard ──REST/JSON──► station-alpha ─┐
                                                    station-bravo  ├─ peer-to-peer sync (HTTP, anti-entropy)
                                                    station-charlie│   each station: own SQLite in own volume
                                                    station-delta ─┘
```

Stack: Python 3.12 · Streamlit · `http.server` + `urllib` · `sqlite3` · Docker Compose · `unittest` · JSON. Nothing else (no Flask/FastAPI/Redis/…). Only `streamlit` is a third-party dependency (`requirements.txt`).

---

## 1. Quick start (Windows 11 + Docker Desktop)

1. Install and start **Docker Desktop** (Linux containers / WSL 2 backend).
2. Open PowerShell in this folder (`D:\DS\miniproject`) and run:

```powershell
docker compose up --build -d
```

3. Open **http://localhost:8501**. Wait ~15 s for the first start (the dashboard waits for all four stations to be healthy).

```powershell
docker compose ps          # all 5 services should be "healthy"
docker compose logs -f     # live logs (Ctrl+C to leave)
```

**No Docker?** Run everything as plain processes (Python 3.12+ and `pip install -r requirements.txt` needed):

```powershell
python scripts/local_demo.py      # 4 station processes (ports 8001-8004) + dashboard on :8501
```
(Scenario D's Stop/Start buttons need Docker; in local mode use the guided manual flow or `python scripts/smoke.py D`.)

> **If Docker Hub is unreachable** (corporate/college network) you can point the build at a mirror or an already pulled image:
> `$env:BASE_IMAGE="your-registry/python:3.12-slim"; docker compose up --build -d`

---

## 2. Using the dashboard

| Page | What to do there |
|---|---|
| **1 · System Overview** | Station cards (health, local counts, pending ops), topology graph, partitions, replica fingerprints. |
| **2 · Incident Management** | Report incidents to a chosen station. *Save at this station only* = local-first write (not replicated). *Save + synchronize* writes then runs a sync. Sections: one station's DB (💾 LOCAL vs 🔁 REPLICATED), network-wide matrix, update/delete (tombstone). |
| **3 · Resource Coordination** | Resource requests with validated status transitions (pending → acknowledged → fulfilled / cancelled). |
| **4 · Network Simulator** | Disconnect/reconnect any of the 6 links, isolate a station, split the network, see per-station reachability (⛔ partitioned vs ❌ unreachable), stop/start containers. |
| **5 · Synchronization Explorer** | Sync the network, before/after replica comparison, missing records, event history, conflicts with winner metadata, idempotency demo, **guided scenarios A–E**. |
| **6 · Distributed Systems Concepts** | 10 concepts × (definition · implementing component · scenario · what to observe · limitations) with live values. |
| **7 · Architecture & Experiments** | Architecture diagram, sync flow, measured experiment results, live success/failure metrics. |

Sidebar: **Refresh view**, **Reset demo data**. The dashboard never writes or syncs on its own — only on button clicks (Streamlit reruns are read-only).

---

## 3. Demonstration script (≈10 minutes)

The five scenarios are buttons on **page 5 → "Guided scenarios A–E"** (each resets to a clean state, performs real operations, and verifies the outcome from the stations' data). To narrate them by hand:

**A · Normal operation.** Page 2 → submit to *Alpha* with **Save at this station only** → Network-wide view shows `✓ v1` only under alpha → press **Synchronize network now** → all four show `✓ v1`.

**B · Partition.** Page 4 → **Isolate Alpha**. Page 2 → create another incident at Alpha → it still works. Synchronize → Alpha's sync attempts are `skipped_partition`; bravo/charlie/delta do **not** have the incident. (Bravo/Charlie/Delta can still sync among themselves.)

**C · Recovery.** Page 4 → **Reconnect all links** → page 5 → **Synchronize whole network** → incident appears everywhere, exactly once; banner turns ✅ CONVERGED.

**D · Node crash & restart.** Page 4 → **⏹ Stop container** on Bravo (or `docker compose stop station-bravo`). Overview shows Bravo OFFLINE; other stations keep working; sync attempts to Bravo end as `unreachable`. **▶ Start container** (or `docker compose start station-bravo`) → Bravo's old records are still there (persistent volume) → synchronize → it catches up.

**E · Concurrent updates.** Page 5 → Scenario E. Alpha and Bravo (both disconnected) update the same incident to version 2; after healing, everyone keeps the update with the **later `updated_at`**; page 5 → *Conflicts* shows rule, winner and both copies' metadata.

Measured results and a full run of A–E are in `results/experiments.md` (generated by `scripts/run_experiments.py`).

---

## 4. Operations cheat-sheet

```powershell
docker compose logs -f station-alpha            # one station's log
docker compose stop station-bravo               # simulate a node crash
docker compose start station-bravo              # bring it back (data intact)
docker compose restart station-charlie          # restart a station
docker compose exec station-alpha python -c "import sqlite3;c=sqlite3.connect('/data/station.db');print(c.execute('select id,version,writer,origin from records').fetchall())"
docker volume ls                                # dcn-alpha-data, dcn-bravo-data, ...
docker volume inspect dcn-alpha-data            # where the data lives
docker compose down                             # stop + remove containers, KEEP data volumes
docker compose down -v                          # FULL RESET: also delete all station databases
docker compose up --build -d                    # rebuild after code changes
```

Direct station API (ports are published on 127.0.0.1 for debugging):
```powershell
curl http://localhost:8001/status
curl http://localhost:8001/records
curl -X POST http://localhost:8001/incidents -H "Content-Type: application/json" -d "{\"title\":\"Test\",\"category\":\"flood\",\"severity\":\"high\",\"location\":\"Sector 1\"}"
curl -X POST http://localhost:8001/sync/trigger -H "Content-Type: application/json" -d "{}"
```
**Reset only the data (keep containers):** sidebar → *Reset demo data*, or `curl -X POST http://localhost:800{1..4}/admin/reset` per station.

### Station HTTP API

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness (used by Docker health checks) |
| `GET /status` | Counts, fingerprint, pending ops, sync outcomes, counters, blocked links |
| `GET /peers` | Reachability probe: `reachable` / `partitioned` / `unreachable` |
| `GET /records[?kind=&deleted=1]`, `GET /records/<id>` | Local records |
| `POST /incidents`, `POST /requests` | Create (validated, saved to local SQLite first) |
| `PATCH /records/<id>` `{"changes":{…}}`, `DELETE /records/<id>` | Update (version+1) / tombstone |
| `POST /sync/exchange`, `POST /sync/push` | **Peer-only** sync messages (need `X-Node-Id`; rejected with `503 link_down` on a partitioned link) |
| `POST /sync/trigger` | Ask this station to sync with its peers now |
| `GET/POST /admin/links` | Partition configuration (persisted in the station's DB) |
| `POST /admin/reset`, `POST /admin/redeliver` | Demo helpers |
| `GET /events`, `GET /conflicts` | Sync log / conflict log |

---

## 5. How it works (short version)

* **Local-first.** A write is validated and committed to the receiving station's SQLite file before anything else happens. No other station is contacted.
* **Records** have `id`, `kind`, `payload`, `version`, `updated_at`, `writer`, `origin`, `created_at`, `deleted` (tombstone) and a content `digest`.
* **Synchronization (anti-entropy).** Initiator sends a summary `{id: [version, updated_at, writer, digest]}`; the peer replies with records the initiator lacks/has older and the ids it needs; the initiator merges, then pushes the rest. Merging is **idempotent** and **atomic** (one SQLite transaction); invalid or tampered records (digest mismatch) are rejected.
* **Merge rule (deterministic, total order).** Larger tuple `(version, updated_at, writer, digest)` wins → newer versions never lose to older; equal versions = conflict resolved by later timestamp, then higher node id.
* **Partitions** are enforced in the peer-communication code: the initiator skips blocked links (`skipped_partition`); the receiver answers `503 link_down` (`rejected_partition`). Both ends are configured by the dashboard; either end alone is enough to block.
* **Crash vs partition.** A stopped container produces `unreachable` (connection refused/timeout); a partition is detected as `partitioned` because the station itself is alive.
* **Timeouts & retries.** `HTTP_TIMEOUT` (2 s) and `HTTP_RETRIES` (1) per request, network errors only; dashboard calls have their own short timeouts, so unavailable peers never block the UI indefinitely.
* Metrics (pending ops, duplicates ignored, conflicts, success/failed attempts, last success) come from each station's SQLite tables and counters — nothing is hard-coded.

Details and limitations: `docs/REPORT.md`. Viva Q&A: `docs/VIVA.md`.

---

## 6. Tests

```powershell
python -m unittest discover -s tests -t . -v        # 45 tests, ~30 s, no internet needed
```
* `tests/test_unit.py` – persistence, restart persistence, validation, version merge, duplicate delivery, deterministic conflict resolution (all 24 delivery orders converge), tombstones, atomic batches.
* `tests/test_integration.py` – launches **four real station processes** on localhost and tests HTTP validation, two-station sync, partition enforcement (local skip *and* remote 503 rejection), recovery, SIGKILL + restart persistence, unreachable peers, and scenarios A–E (incl. repeatability).

Other scripts: `python scripts/smoke.py [A B C D E]` (print scenario steps), `python scripts/run_experiments.py [--compose | --in-docker]` (writes `results/experiments.*`), `docker compose exec dashboard python scripts/run_experiments.py --in-docker`.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose` can't pull `python:3.12-slim` | Network/proxy blocks Docker Hub → set `BASE_IMAGE` to a reachable mirror (see Quick start). |
| Dashboard shows stations OFFLINE right after start | Give it 10–15 s; check `docker compose ps` for `healthy`; `docker compose logs station-alpha`. |
| Port 8501/8001–8004 already in use | Stop the other program or change the left side of the `ports:` mappings in `compose.yaml`. |
| Stop/Start buttons missing, Scenario D disabled | The Docker socket mount was removed or isn't available; use `docker compose stop/start station-bravo` and the guided D1/D2/D3 flow. |
| `dashboard` can't control containers on Windows | Docker Desktop must expose the engine socket to Linux containers (default). Otherwise use the CLI commands. |
| Changes not visible after editing code | `docker compose up --build -d`. |
| Want a clean slate | Sidebar *Reset demo data* or `docker compose down -v`. |
| Sync shows `partial` / `failed` | Look at `docker compose logs station-<x>`; the event table (page 5) has the reason in `detail`. |

**Compose startup-order limits.** `depends_on: condition: service_healthy` only delays the *dashboard* until stations report healthy once; it does not keep ordering at runtime. Health checks prove the HTTP server answers `/health`, not that peers are reachable. Stations never depend on each other at startup — they tolerate absent peers by design (sync simply reports `unreachable`).

**Security note.** Mounting `/var/run/docker.sock` into the dashboard lets it stop/start containers (and effectively gives it root on the Docker host). It exists only to demo node crashes from the UI; delete that one line in `compose.yaml` if you don't want it. Station-to-station HTTP is unauthenticated and unencrypted — acceptable for a lab, not for real use.

---

## 8. Limitations (honest list)

* **Eventual consistency only.** No consensus, no strong consistency, no guaranteed delivery, no Byzantine tolerance. Replicas converge only when a sync is triggered (auto-sync is off by default; set `AUTO_SYNC_SECONDS` to enable).
* **Whole-record last-writer-wins.** The loser of a concurrent update is discarded (logged, not merged). Conflict *detection* uses equal integer versions; it cannot prove concurrency in general (a version vector would). Timestamps come from node clocks (skew can pick a surprising winner).
* **Full replication, full-summary exchange** (O(records) per sync) — fine for hundreds/thousands of records, not millions.
* Partitions are simulated at the application layer (symmetric, on/off); no latency/loss injection.
* Tombstones are never garbage-collected.
* The Docker-socket feature and the container build were validated in a sandbox using a locally imported base image (Docker Hub was unreachable there); see `docs/REPORT.md` §7.

## 9. Project layout

```
compose.yaml  Dockerfile  .dockerignore  requirements.txt  README.md
dcn/            station.py (HTTP service) · db.py (SQLite) · sync.py · merge.py · schemas.py
                netclient.py · client.py (cluster client) · scenarios.py · controllers.py · config.py · util.py
dashboard/      app.py · ui.py · views/ (7 pages)
tests/          test_unit.py · test_integration.py
scripts/        smoke.py · run_experiments.py · local_demo.py · capture_screenshots.py
docs/           REPORT.md · VIVA.md · SCREENSHOTS.md · architecture.png/.dot · screenshots/
results/        experiments.json / .md (measured)
```

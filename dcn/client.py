"""Cluster client used by the dashboard, scenarios, experiments and tests.

It holds NO data of its own: every call goes over HTTP to a station.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from .config import parse_peers
from .netclient import NetError, http_json

NODE_LABELS = {
    "alpha": ("Station Alpha", "North Zone"), "bravo": ("Station Bravo", "South Zone"),
    "charlie": ("Station Charlie", "East Zone"), "delta": ("Station Delta", "West Zone"),
}


class Cluster:
    def __init__(self, stations: dict, timeout=3.0):
        self.urls = dict(stations)
        self.nodes = list(stations)
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        return cls(parse_peers(os.environ.get("STATIONS", "")))

    # --- low level -----------------------------------------------------------
    def call(self, node, method, path, body=None, timeout=None, retries=0):
        """Return (status, json) or (None, {'error': 'unreachable', ...}) on network failure."""
        try:
            return http_json(method, self.urls[node] + path, body, None,
                             timeout or self.timeout, retries)
        except NetError as e:
            return None, {"error": "unreachable", "kind": e.kind, "detail": e.detail}

    def _all(self, fn, nodes=None):
        nodes = nodes or self.nodes
        with ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            return dict(zip(nodes, ex.map(fn, nodes)))

    # --- observation -----------------------------------------------------------
    def status_all(self):
        def one(n):
            st, b = self.call(n, "GET", "/status", timeout=2.0)
            return dict(b, online=True) if st == 200 else {"online": False, "node_id": n,
                                                           "error": b.get("error", f"HTTP {st}")}
        return self._all(one)

    def peers_all(self):
        def one(n):
            st, b = self.call(n, "GET", "/peers", timeout=6.0)
            return b if st == 200 else None
        return self._all(one)

    def records_all(self, include_deleted=False):
        def one(n):
            st, b = self.call(n, "GET", f"/records?deleted={int(include_deleted)}", timeout=4.0)
            return b["records"] if st == 200 else None
        return self._all(one)

    def events_all(self, limit=200):
        def one(n):
            st, b = self.call(n, "GET", f"/events?limit={limit}", timeout=3.0)
            return b["events"] if st == 200 else None
        return self._all(one)

    def conflicts_all(self):
        def one(n):
            st, b = self.call(n, "GET", "/conflicts", timeout=3.0)
            return b["conflicts"] if st == 200 else None
        return self._all(one)

    def link_flags(self):
        """{node: set(blocked peers)} as configured at each reachable end."""
        def one(n):
            st, b = self.call(n, "GET", "/admin/links", timeout=2.0)
            return set(b["blocked"]) if st == 200 else None
        return self._all(one)

    def blocked_pairs(self):
        flags = self.link_flags()
        pairs = set()
        for a, bl in flags.items():
            for b in (bl or ()):
                pairs.add(tuple(sorted((a, b))))
        return pairs

    def presence_matrix(self):
        """{record_id: {node: (version, digest) | None}} + per-node online map."""
        recs = self.records_all(include_deleted=True)
        ids = sorted({r["id"] for rs in recs.values() if rs for r in rs})
        by_node = {n: ({r["id"]: r for r in rs} if rs is not None else None)
                   for n, rs in recs.items()}
        matrix = {i: {n: (by_node[n].get(i) if by_node[n] is not None else None)
                      for n in self.nodes} for i in ids}
        return matrix, {n: rs is not None for n, rs in recs.items()}

    def convergence(self):
        """Do all ONLINE stations hold identical data?"""
        st = self.status_all()
        online = {n: s for n, s in st.items() if s["online"]}
        fps = {n: s["fingerprint"] for n, s in online.items()}
        return {"converged": len(set(fps.values())) <= 1 and bool(online),
                "fingerprints": fps, "online": sorted(online),
                "offline": sorted(n for n, s in st.items() if not s["online"])}

    # --- actions ---------------------------------------------------------------
    def create(self, node, kind, data):
        return self.call(node, "POST", "/incidents" if kind == "incident" else "/requests", data)

    def update(self, node, rid, changes):
        return self.call(node, "PATCH", f"/records/{rid}", {"changes": changes})

    def delete(self, node, rid):
        return self.call(node, "DELETE", f"/records/{rid}")

    def sync_node(self, node, peers=None):
        body = {"peers": peers} if peers else {}
        return self.call(node, "POST", "/sync/trigger", body, timeout=30.0)

    def sync_network(self, max_rounds=3):
        """Ask every station to sync with its peers, repeating until nothing moves.

        Stations are triggered one after another (deterministic). Unreachable stations
        simply report failed attempts; the call is bounded by timeouts.
        """
        t0 = time.perf_counter()
        rounds, all_results = [], []
        for i in range(max_rounds):
            moved = 0
            round_res = {}
            for n in self.nodes:
                st, b = self.sync_node(n)
                if st != 200:
                    round_res[n] = {"error": b.get("error", f"HTTP {st}")}
                    continue
                round_res[n] = b["results"]
                for r in b["results"]:
                    moved += r["sent"] + r["received"]
                    all_results.append(dict(r, initiator=n, round=i + 1))
            rounds.append(round_res)
            if moved == 0:
                break
        return {"rounds": len(rounds), "results": all_results,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "convergence": self.convergence()}

    def set_link(self, a, b, blocked):
        """Disconnect/reconnect link a<->b by configuring BOTH endpoints."""
        out = {}
        for x, y in ((a, b), (b, a)):
            st, body = self.call(x, "POST", "/admin/links", {"peer": y, "blocked": blocked})
            out[x] = st == 200
        return out

    def isolate(self, node, blocked=True):
        for other in self.nodes:
            if other != node:
                self.set_link(node, other, blocked)

    def heal_all(self):
        for i, a in enumerate(self.nodes):
            for b in self.nodes[i + 1:]:
                self.set_link(a, b, False)

    def reset_all(self):
        return self._all(lambda n: self.call(n, "POST", "/admin/reset", {})[0] == 200)

    def wait_online(self, node, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, _ = self.call(node, "GET", "/health", timeout=1.0)
            if st == 200:
                return round(time.time() - t0, 2)
            time.sleep(0.3)
        return None

    def wait_offline(self, node, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, _ = self.call(node, "GET", "/health", timeout=0.8)
            if st is None:
                return round(time.time() - t0, 2)
            time.sleep(0.3)
        return None

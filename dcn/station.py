"""Field station service: an independent process with its own SQLite DB and HTTP API.

Run:  python -m dcn.station      (configuration comes from environment variables)
"""
import json
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import schemas, sync
from .config import StationConfig
from .db import Database, NotFound, wire_record
from .netclient import NetError, http_json
from .util import NODE_RE, now_iso

MAX_BODY = 4 * 1024 * 1024
REC_RE = re.compile(r"^/records/([A-Za-z0-9_-]+)$")


class StationApp:
    def __init__(self, cfg: StationConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path, cfg.node_id)
        self.started_at = now_iso()
        self.t0 = time.time()
        self.sync_lock = threading.Lock()   # one outgoing sync run at a time

    # --- helpers used by handlers -------------------------------------------
    def info(self):
        c = self.cfg
        return {"node_id": c.node_id, "name": c.name, "zone": c.zone,
                "started_at": self.started_at, "uptime_s": round(time.time() - self.t0, 1),
                "peers": sorted(c.peers)}

    def status(self):
        st = self.db.stats()
        pend = self.db.pending_by_peer(sorted(self.cfg.peers))
        st.update(self.info())
        st["pending_by_peer"] = pend
        st["pending_total"] = sum(pend.values())
        st["blocked_links"] = self.db.blocked_peers()
        return st

    def probe_peers(self):
        """Distinguish 'partitioned' from 'unreachable (crashed/timeout)' per peer."""
        out = {}
        for p, url in sorted(self.cfg.peers.items()):
            if self.db.is_blocked(p):
                out[p] = {"state": "partitioned", "detail": "link disabled locally"}
                continue
            t0 = time.perf_counter()
            try:
                st, body = http_json("GET", f"{url}/health", None, {"X-Node-Id": self.cfg.node_id},
                                     timeout=min(self.cfg.http_timeout, 1.5), retries=0)
            except NetError as e:
                out[p] = {"state": "unreachable", "detail": f"{e.kind}: node down or timing out"}
                continue
            ms = round((time.perf_counter() - t0) * 1000, 1)
            if st == 503 and body.get("error") == "link_down":
                out[p] = {"state": "partitioned", "detail": "link disabled at peer", "ms": ms}
            elif st == 200:
                out[p] = {"state": "reachable", "detail": "healthy", "ms": ms}
            else:
                out[p] = {"state": "unreachable", "detail": f"HTTP {st}", "ms": ms}
        return out

    def trigger_sync(self, peers=None):
        if not self.sync_lock.acquire(timeout=0.1):
            return {"error": "sync_in_progress"}
        try:
            targets = [p for p in (peers or sorted(self.cfg.peers)) if p in self.cfg.peers]
            return {"node_id": self.cfg.node_id,
                    "results": sync.sync_all(self.db, self.cfg, targets)}
        finally:
            self.sync_lock.release()

    def redeliver(self, peer, record_id):
        """Demo helper: send one record to a peer again (duplicate delivery)."""
        rec = self.db.get_record(record_id)
        if not rec:
            raise NotFound(record_id)
        if peer not in self.cfg.peers:
            raise schemas.ValidationError("unknown peer")
        if self.db.is_blocked(peer):
            return {"outcome": "skipped_partition"}
        try:
            st, body = http_json("POST", f"{self.cfg.peers[peer]}/sync/push",
                                 {"from": self.cfg.node_id, "records": [wire_record(rec)]},
                                 {"X-Node-Id": self.cfg.node_id}, self.cfg.http_timeout, 0)
        except NetError as e:
            return {"outcome": "unreachable", "detail": str(e)}
        return {"outcome": "delivered" if st == 200 else "rejected", "http": st, "reply": body}

    def auto_sync_loop(self):
        while True:
            time.sleep(self.cfg.auto_sync_seconds)
            try:
                self.trigger_sync()
            except Exception as e:  # keep the loop alive
                print(f"auto-sync error: {e}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DCNStation/1.0"

    @property
    def app(self) -> StationApp:
        return self.server.app

    def log_message(self, fmt, *args):
        print(f"[{self.app.cfg.node_id}] {self.address_string()} {fmt % args}", flush=True)

    # --- plumbing -------------------------------------------------------------
    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise schemas.ValidationError("bad Content-Length", code="bad_request")
        if n > MAX_BODY:
            raise schemas.ValidationError("body too large", code="too_large")
        if n == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise schemas.ValidationError("Content-Type must be application/json",
                                          code="unsupported_media_type")
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise schemas.ValidationError("invalid JSON", code="invalid_json")
        if not isinstance(data, dict):
            raise schemas.ValidationError("JSON body must be an object", code="invalid_json")
        return data

    def _peer(self):
        """Identify a calling peer; reject it when the link is partitioned."""
        peer = self.headers.get("X-Node-Id")
        if peer is None:
            return None
        if not NODE_RE.match(peer) or peer not in self.app.cfg.peers:
            raise PermissionError("unknown peer")
        if self.app.db.is_blocked(peer):
            self.app.db.incr("partition_rejections")
            raise LinkDown(peer)
        return peer

    def _dispatch(self, method):
        url = urlparse(self.path)
        path, q = url.path.rstrip("/") or "/", parse_qs(url.query)
        app, db = self.app, self.app.db
        try:
            if method == "GET":
                if path == "/health":
                    self._peer()
                    return self._send(200, dict(app.info(), status="ok"))
                if path == "/status":
                    return self._send(200, app.status())
                if path == "/peers":
                    return self._send(200, app.probe_peers())
                if path == "/records":
                    recs = db.list_records((q.get("kind") or [None])[0],
                                           (q.get("deleted") or ["0"])[0] == "1")
                    return self._send(200, {"node_id": app.cfg.node_id, "records": recs})
                m = REC_RE.match(path)
                if m:
                    rec = db.get_record(m.group(1))
                    return self._send(200, rec) if rec else self._send(404, {"error": "not_found"})
                if path == "/events":
                    return self._send(200, {"events": db.events(int((q.get("limit") or [100])[0]))})
                if path == "/conflicts":
                    return self._send(200, {"conflicts": db.conflicts()})
                if path == "/admin/links":
                    return self._send(200, {"blocked": db.blocked_peers()})
            elif method == "POST":
                body = self._body()
                if path in ("/incidents", "/requests"):
                    kind = "incident" if path == "/incidents" else "request"
                    return self._send(201, db.create_record(kind, body))
                if path == "/sync/exchange":
                    peer = self._peer() or self._need_peer()
                    return self._send(200, sync.respond_exchange(db, peer, body.get("summary")))
                if path == "/sync/push":
                    peer = self._peer() or self._need_peer()
                    return self._send(200, sync.respond_push(db, peer, body.get("records")))
                if path == "/sync/trigger":
                    peers = body.get("peers")
                    if peers is not None and not isinstance(peers, list):
                        raise schemas.ValidationError("peers must be a list")
                    return self._send(200, app.trigger_sync(peers))
                if path == "/admin/links":
                    peer = body.get("peer")
                    if peer not in app.cfg.peers or not isinstance(body.get("blocked"), bool):
                        raise schemas.ValidationError("need {peer: <known peer>, blocked: bool}")
                    db.set_link(peer, body["blocked"])
                    return self._send(200, {"blocked": db.blocked_peers()})
                if path == "/admin/reset":
                    db.reset()
                    return self._send(200, {"reset": True})
                if path == "/admin/redeliver":
                    return self._send(200, app.redeliver(body.get("peer"), body.get("record_id")))
            elif method == "PATCH":
                m = REC_RE.match(path)
                if m:
                    return self._send(200, db.update_record(m.group(1), self._body().get("changes")))
            elif method == "DELETE":
                m = REC_RE.match(path)
                if m:
                    return self._send(200, db.delete_record(m.group(1)))
            return self._send(404, {"error": "not_found", "path": path})
        except LinkDown as e:
            return self._send(503, {"error": "link_down", "detail": f"link to {e} disconnected"})
        except PermissionError as e:
            return self._send(403, {"error": "forbidden", "detail": str(e)})
        except NotFound:
            return self._send(404, {"error": "not_found"})
        except schemas.ValidationError as e:
            code = {"invalid_transition": 409, "deleted": 409, "too_large": 413,
                    "unsupported_media_type": 415}.get(e.code, 400)
            return self._send(code, {"error": e.code, "details": e.details})
        except Exception as e:  # last resort: never kill the server thread
            print(f"[{app.cfg.node_id}] ERROR {method} {path}: {e!r}", file=sys.stderr, flush=True)
            return self._send(500, {"error": "internal", "detail": repr(e)})

    def _need_peer(self):
        raise PermissionError("X-Node-Id header required for sync endpoints")

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PATCH(self): self._dispatch("PATCH")
    def do_DELETE(self): self._dispatch("DELETE")


class LinkDown(Exception):
    pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(cfg: StationConfig, host="0.0.0.0"):
    srv = Server((host, cfg.port), Handler)
    srv.app = StationApp(cfg)
    return srv


def main():
    cfg = StationConfig.from_env()
    srv = make_server(cfg)
    if cfg.auto_sync_seconds > 0:
        threading.Thread(target=srv.app.auto_sync_loop, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    print(f"[{cfg.node_id}] {cfg.name} ({cfg.zone}) listening on :{cfg.port} "
          f"db={cfg.db_path} peers={sorted(cfg.peers)}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

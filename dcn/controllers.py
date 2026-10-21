"""Node lifecycle controllers: stop / start a station.

Three interchangeable implementations (same two methods):
  LocalCluster         real OS processes on localhost (tests, no-Docker demo)
  ComposeCliController `docker compose stop|start` run from the host
  DockerSocketController  Docker Engine API over /var/run/docker.sock (used by the
                          dashboard container when the socket is mounted)
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from .client import Cluster

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = [("alpha", "Station Alpha", "North Zone"), ("bravo", "Station Bravo", "South Zone"),
         ("charlie", "Station Charlie", "East Zone"), ("delta", "Station Delta", "West Zone")]


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


class LocalCluster:
    """Starts four real station processes (python -m dcn.station) on localhost."""

    def __init__(self, data_dir=None, ports=None, http_timeout=1.0, retries=0):
        self.data_dir = data_dir or tempfile.mkdtemp(prefix="dcn-")
        ports = ports or free_ports(len(NODES))
        self.ports = {n[0]: p for n, p in zip(NODES, ports)}
        self.urls = {n: f"http://127.0.0.1:{p}" for n, p in self.ports.items()}
        self.procs = {}
        self.http_timeout, self.retries = http_timeout, retries
        self.cluster = Cluster(self.urls)

    def _env(self, node):
        name, zone = next((n[1], n[2]) for n in NODES if n[0] == node)
        peers = ",".join(f"{n}={u}" for n, u in self.urls.items() if n != node)
        return dict(os.environ, NODE_ID=node, NODE_NAME=name, ZONE=zone,
                    PORT=str(self.ports[node]), PEERS=peers,
                    DB_PATH=os.path.join(self.data_dir, f"{node}.db"),
                    HTTP_TIMEOUT=str(self.http_timeout), HTTP_RETRIES=str(self.retries),
                    PYTHONUNBUFFERED="1", PYTHONPATH=ROOT)

    def start(self, node):
        if node in self.procs and self.procs[node].poll() is None:
            return
        log = open(os.path.join(self.data_dir, f"{node}.log"), "ab")
        try:
            self.procs[node] = subprocess.Popen([sys.executable, "-m", "dcn.station"],
                                                env=self._env(node), cwd=ROOT,
                                                stdout=log, stderr=log)
        finally:
            log.close()   # the child keeps its own copy of the descriptor

    def stop(self, node, kill=False):
        p = self.procs.get(node)
        if p and p.poll() is None:
            p.kill() if kill else p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def start_all(self, timeout=20):
        for n in self.ports:
            self.start(n)
        for n in self.ports:
            if self.cluster.wait_online(n, timeout) is None:
                raise RuntimeError(f"station {n} did not come up; see {self.data_dir}/{n}.log")

    def stop_all(self):
        for n in list(self.procs):
            self.stop(n)

    # uniform controller interface
    def stop_node(self, node):
        self.stop(node)

    def start_node(self, node):
        self.start(node)


class ComposeCliController:
    def __init__(self, project_dir=ROOT):
        self.project_dir = project_dir

    def _run(self, *args):
        subprocess.run(["docker", "compose", *args], cwd=self.project_dir, check=True,
                       capture_output=True, timeout=120)

    def stop_node(self, node):
        self._run("stop", f"station-{node}")

    def start_node(self, node):
        self._run("start", f"station-{node}")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect(self._path)


class DockerSocketController:
    SOCK = "/var/run/docker.sock"

    def __init__(self, container_fmt="dcn-station-{node}"):
        self.container_fmt = container_fmt

    @classmethod
    def available(cls, container="dcn-station-alpha"):
        """True only if the socket is mounted AND the station containers really exist."""
        if not os.path.exists(cls.SOCK):
            return False
        c = _UnixHTTPConnection(cls.SOCK)
        try:
            c.request("GET", f"/containers/{container}/json")
            r = c.getresponse()
            r.read()
            return r.status == 200
        except OSError:
            return False
        finally:
            c.close()

    def _post(self, path):
        c = _UnixHTTPConnection(self.SOCK)
        try:
            c.request("POST", path)
            r = c.getresponse()
            body = r.read()
            if r.status not in (204, 304):
                raise RuntimeError(f"docker API {r.status}: {body[:200]!r}")
        finally:
            c.close()

    def stop_node(self, node):
        self._post(f"/containers/{self.container_fmt.format(node=node)}/stop?t=2")

    def start_node(self, node):
        self._post(f"/containers/{self.container_fmt.format(node=node)}/start")

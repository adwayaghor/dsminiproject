"""No-Docker demo: starts 4 real station processes + the Streamlit dashboard on this machine.

    python scripts/local_demo.py            (Ctrl+C stops everything; data lives in ./local-data)

Stations listen on 8001-8004; dashboard on http://localhost:8501.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dcn.controllers import LocalCluster  # noqa: E402


def main():
    data = os.path.join(ROOT, "local-data")
    os.makedirs(data, exist_ok=True)
    lc = LocalCluster(data_dir=data, ports=[8001, 8002, 8003, 8004], http_timeout=2.0, retries=1)
    lc.start_all()
    env = dict(os.environ, PYTHONPATH=ROOT,
               STATIONS=",".join(f"{n}={u}" for n, u in lc.urls.items()))
    dash = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
                             "--server.port=8501", "--server.headless=true"], cwd=ROOT, env=env)
    print("\nStations: " + ", ".join(f"{n} {u}" for n, u in lc.urls.items()))
    print("Dashboard: http://localhost:8501   (Ctrl+C to stop)\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dash.terminate()
        lc.stop_all()


if __name__ == "__main__":
    main()

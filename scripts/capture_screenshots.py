"""Drive the running dashboard (http://localhost:8501) with Playwright and save the 5 evaluation
screenshots. Requires: pip install playwright (and a Chromium). Optional helper, not needed to run the project."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright
from dcn.client import Cluster

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
URL = os.environ.get("DASH_URL", "http://localhost:8501")
c = Cluster({"alpha": "http://localhost:8001", "bravo": "http://localhost:8002",
             "charlie": "http://localhost:8003", "delta": "http://localhost:8004"})
INC = {"description": "", "category": "flood", "severity": "high"}


def main():
    c.heal_all(); c.reset_all()
    c.create("bravo", "incident", dict(INC, title="Riverbank breach", location="South Ward", severity="critical"))
    c.create("charlie", "request", {"resource_type": "drinking water", "quantity": 400, "priority": "urgent"})
    c.create("delta", "request", {"resource_type": "rescue equipment", "quantity": 12, "priority": "high"})
    c.sync_network()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium"), args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1500, "height": 1900})
        def page(n):
            pg.get_by_text(f"{n} · ", exact=False).first.click(); time.sleep(3)
        def shot(name, full=True):
            time.sleep(1.5); pg.screenshot(path=os.path.join(OUT, name), full_page=full); print("saved", name)
        pg.goto(URL); pg.wait_for_selector("text=Field stations", timeout=40000); time.sleep(2)
        shot("1_overview_all_online.png")

        # 2. create an incident through the UI at Alpha and synchronize
        page(2)
        pg.get_by_label("Title").fill("Flooded underpass near Sector 4")
        pg.get_by_label("Location / zone").fill("Sector 4")
        pg.get_by_label("Description").fill("Water level rising on the main road")
        pg.get_by_role("button", name="💾 + 🔁 Save and synchronize network").click(); time.sleep(5)
        pg.get_by_text("🌐 Network-wide view", exact=True).click(); time.sleep(2)
        shot("2_incident_created_and_replicated.png")

        # 3. partition: isolate Alpha, create an incident there, do NOT sync it anywhere
        page(4)
        pg.get_by_role("button", name="Isolate Alpha").click(); time.sleep(4)
        page(2)
        pg.get_by_label("Title").fill("Bridge collapse (reported while isolated)")
        pg.get_by_label("Location / zone").fill("Sector 9")
        pg.get_by_role("button", name="💾 Save at this station only").click(); time.sleep(4)
        pg.get_by_text("🌐 Network-wide view", exact=True).click(); time.sleep(1)
        pg.get_by_role("button", name="🔁 Synchronize network now").click(); time.sleep(6)
        pg.get_by_text("🌐 Network-wide view", exact=True).click(); time.sleep(2)
        shot("3_partition_local_not_replicated.png")

        # 4. reconnect and synchronize
        page(4)
        pg.get_by_role("button", name="✅ Reconnect all links").click(); time.sleep(4)
        page(5)
        pg.get_by_role("button", name="🔁 Synchronize whole network").click(); time.sleep(6)
        shot("4_sync_after_reconnect.png")

        # 5. crash a station via the dashboard (Docker control), then restart + catch up
        page(4)
        pg.get_by_role("button", name="⏹ Stop container").nth(1).click(); time.sleep(8)
        c.create("alpha", "incident", dict(INC, title="Gas leak while Bravo is down", location="Sector 2", category="other"))
        c.sync_network()   # attempts to reach the stopped station fail as 'unreachable'
        page(1); shot("5a_node_failure_overview.png")
        page(4)
        pg.get_by_role("button", name="▶ Start container").first.click(); time.sleep(10)
        page(5)
        pg.get_by_role("button", name="🔁 Synchronize whole network").click(); time.sleep(6)
        page(7); pg.get_by_text("📡 Live communication metrics", exact=True).click(); time.sleep(3)
        shot("5b_recovery_sync_metrics.png")
        b.close()

main()

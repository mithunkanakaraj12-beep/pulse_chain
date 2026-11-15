#!/usr/bin/env python3
"""End-to-end browser check of the PulseChain AI web app (needs Playwright + Chromium).

    python scripts/e2e_check.py            # starts its own server on a throw-away database, runs, cleans up
    python scripts/e2e_check.py --url http://localhost:8000   # use a server you already started (it WILL be modified, then reset)

Checks: every page renders with no console errors, inventory edit ripples into projections, SMS is simulated and
logged, a transfer needs explicit approval, the monsoon scenario applies and reverts, and the offline path
(service worker, cached dashboards, edit queue, sync on reconnect) works. Screenshots go to docs/screenshots/.
"""
from __future__ import annotations

import argparse, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
PAGES = ["dashboard", "clinics", "clinics/C08", "forecast", "inventory", "redistribution", "alerts", "demo", "model"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail and not ok else ''}")


def start_server(port: int):
    tmp = tempfile.mkdtemp(prefix="pulsechain-e2e-")
    env = {**os.environ, "PULSECHAIN_DB_PATH": f"{tmp}/e2e.db", "PULSECHAIN_MODEL_DIR": f"{tmp}/models", "PULSECHAIN_PORT": str(port), "SMS_MODE": "simulated", "SMS_ALLOW_LIVE": "false"}
    cwd = ROOT / "backend"
    subprocess.run([sys.executable, "-m", "pulsechain.cli", "reset", "--train"], cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen([sys.executable, "-m", "pulsechain.cli", "serve", "--port", str(port)], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1); return proc
        except Exception:
            time.sleep(0.5)
    proc.kill(); raise SystemExit("server did not start")


def run(base: str, shots: bool) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=False)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("Pages")
        for pg in PAGES:
            page.goto(f"{base}/#/{pg}"); page.wait_for_selector("h1"); page.wait_for_timeout(500)
            check(f"{pg} renders", page.locator("h1").count() > 0 and page.locator(".err-box").count() == 0)
            body = page.locator("main").inner_text()
            bad = [t for t in ("null", "undefined", "NaN", "[object") if t in body]
            check(f"{pg} has no leaked null/undefined/NaN text", not bad, str(bad))
            if shots and pg in ("dashboard", "forecast", "inventory", "redistribution", "alerts", "model"):
                page.screenshot(path=str(SHOTS / f"{pg}.png"), full_page=True)
        check("no console errors across pages", not errors, "; ".join(errors[:3]))

        print("Inventory edit recalculates")
        page.goto(f"{base}/#/inventory?clinic=C01"); page.wait_for_selector("table.t")
        row = page.locator("table.t tbody tr", has_text="malaria treatment (ACT)").first
        before = row.locator("td").nth(2).inner_text()
        row.get_by_role("button", name="Edit").click()
        page.locator("input[aria-label^='Quantity, batch']").first.fill("0")
        page.get_by_role("button", name="Save", exact=True).first.click()
        page.wait_for_timeout(900)
        after = page.locator("table.t tbody tr", has_text="malaria treatment (ACT)").first.locator("td").nth(2).inner_text()
        check("usable stock changes after edit", before != after, f"{before!r} -> {after!r}")

        print("Redistribution needs approval")
        page.goto(f"{base}/#/redistribution"); page.wait_for_selector(".item")
        n0 = page.locator(".item", has_text="Needs human approval").count()
        check("proposals shown, none auto-applied", n0 > 0 and "No decisions yet" in page.locator("body").inner_text())
        page.get_by_role("button", name="Review and approve").first.click()
        check("confirmation dialog appears", page.locator("dialog[open]").count() == 1)
        page.get_by_role("button", name="Approve transfer").click(); page.wait_for_timeout(900)
        check("approval recorded", "Approved" in page.locator("body").inner_text())
        if shots: page.screenshot(path=str(SHOTS / "redistribution-after-approval.png"), full_page=True)

        print("SMS simulated and logged")
        page.goto(f"{base}/#/alerts"); page.wait_for_selector(".item")
        page.get_by_role("button", name="Simulate SMS").first.click()
        page.get_by_role("button", name="Simulate send").click(); page.wait_for_timeout(900)
        check("simulated SMS in log", "Simulated" in page.locator("body").inner_text() and page.get_by_role("button", name="Send real SMS").count() == 0)

        print("Monsoon demo")
        page.goto(f"{base}/#/demo"); page.wait_for_selector(".stage-actions")
        for i in range(7):
            page.locator(".stage-actions .btn.primary").click(); page.wait_for_timeout(250)
        text = page.locator("body").inner_text()
        check("8 steps reachable", "Step 8 of 8" in text)
        check("simulated + not-scientific disclaimer shown", "SIMULATED" in text.upper() and "NOT a scientifically established" in text)
        page.get_by_role("button", name="Apply this monsoon to the live dashboard").click()
        page.get_by_role("button", name="Apply monsoon").click(); page.wait_for_selector(".banner.scenario")
        check("scenario banner visible app-wide", True)
        page.get_by_role("button", name="Return to normal").click(); page.wait_for_timeout(900)
        check("scenario reverts", page.locator(".banner.scenario").count() == 0)

        print("Offline")
        page.goto(f"{base}/#/dashboard"); page.wait_for_selector("h1")
        page.evaluate("navigator.serviceWorker.ready.then(() => true)"); page.wait_for_timeout(1500)
        for pg in ("inventory?clinic=C02", "alerts", "forecast"):  # visit so their data is cached for offline
            page.goto(f"{base}/#/{pg}"); page.wait_for_selector("h1"); page.wait_for_timeout(700)
        check("service worker controls the page", page.evaluate("!!navigator.serviceWorker.controller"))
        ctx.set_offline(True)
        page.goto(f"{base}/#/dashboard"); page.reload(); page.wait_for_selector("h1"); page.wait_for_timeout(800)
        check("dashboard loads offline from cache", "Network overview" in page.locator("h1").inner_text())
        check("offline indicator shown", page.locator(".net.offline").count() == 1 and page.locator(".banner.offline").count() == 1)
        page.goto(f"{base}/#/inventory?clinic=C02"); page.wait_for_selector("table.t")
        page.locator("table.t tbody tr").first.get_by_role("button", name="Edit").click()
        qty = page.locator("input[aria-label^='Quantity, batch']").first
        qty.fill("77"); page.get_by_role("button", name="Save", exact=True).first.click(); page.wait_for_timeout(600)
        check("edit queued while offline", "waiting to sync" in page.locator(".net").inner_text())
        page.goto(f"{base}/#/forecast"); page.wait_for_selector("h1"); page.wait_for_timeout(600)
        check("forecast shows saved data, not a fake recompute", "saved on this device" in page.locator("body").inner_text())
        ctx.set_offline(False)
        page.wait_for_selector(".toast", timeout=10000); page.wait_for_timeout(800)
        check("queue synced on reconnect", "waiting to sync" not in page.locator(".net").inner_text() and "Online" in page.locator(".net").inner_text())

        inv = page.request.get(f"{base}/api/inventory?clinic_id=C02").json()["rows"]
        check("offline edit really reached the server", any(b["quantity"] == 77 for r in inv for b in r["batches"]))

        print("Mobile layout")
        m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2).new_page()
        m.goto(f"{base}/#/dashboard"); m.wait_for_selector("h1"); m.wait_for_timeout(600)
        check("no horizontal page scroll at 390px", m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        if shots:
            m.screenshot(path=str(SHOTS / "dashboard-mobile.png"), full_page=False)
            m.goto(f"{base}/#/demo"); m.wait_for_selector(".stage-actions"); m.wait_for_timeout(500); m.screenshot(path=str(SHOTS / "demo-mobile.png"))
        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--url"); ap.add_argument("--port", type=int, default=8765); ap.add_argument("--no-shots", action="store_true")
    a = ap.parse_args()
    proc = None
    try:
        base = a.url.rstrip("/") if a.url else None
        if not base:
            proc = start_server(a.port); base = f"http://127.0.0.1:{a.port}"
        run(base, not a.no_shots)
    finally:
        if proc: proc.terminate()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)

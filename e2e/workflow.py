"""End-to-end UI workflow against a live corca-serve + fake Prometheus.

Usage: python3 e2e/workflow.py <phase>
  setup     connect source, discover, dependencies, sensitivity, verify
  diagnose  concurrent-fault diagnosis, healthy-window abstention, validation, cancel, search, export, share link
  persist   after a server restart: history + settings still there; mobile layout
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = os.getenv("CORCA_E2E_URL", "http://localhost:8000").rstrip("/")
ROOT = Path(__file__).parent
SHOTS = ROOT / "shots"
INCIDENT = int((ROOT / "incident.txt").read_text())
SOURCE = json.loads((ROOT.parent / "lab/source.online-boutique.json").read_text())
EDGES = [("frontend", "adservice"), ("frontend", "cartservice"), ("frontend", "checkoutservice"),
         ("frontend", "currencyservice"), ("frontend", "productcatalogservice"),
         ("frontend", "recommendationservice"), ("frontend", "shippingservice"),
         ("checkoutservice", "cartservice"), ("checkoutservice", "currencyservice"),
         ("checkoutservice", "emailservice"), ("checkoutservice", "paymentservice"),
         ("checkoutservice", "productcatalogservice"), ("checkoutservice", "shippingservice"),
         ("recommendationservice", "productcatalogservice"), ("cartservice", "redis-cart")]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def local_minute(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")


def open_app(p, path="/app", **kw):
    browser = p.chromium.launch(executable_path=os.getenv("CORCA_E2E_CHROMIUM"))
    ctx = browser.new_context(viewport=kw.get("viewport", {"width": 1280, "height": 900}),
                              timezone_id="UTC", accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "fonts" not in (m.location or {}).get("url", "")
            and "ERR_TUNNEL" not in m.text and errors.append(m.text))
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    return browser, page, errors


def nav(page, view):
    if view in ('services', 'team'):
        page.locator('.sidebar [data-view="integrations"]').click()
        page.locator(f'.setup-tabs [data-view="{view}"]').click()
    else:
        page.locator(f'.sidebar [data-view="{view}"]').click()
    page.wait_for_timeout(400)


def setup(p):
    browser, page, errors = open_app(p)
    nav(page, "integrations")
    page.locator("[data-provider=prometheus]").click()
    page.fill("#source-url", os.getenv("CORCA_E2E_METRICS_URL", "http://127.0.0.1:9090"))
    page.locator("#source-metric-settings > summary").click()
    page.fill("#source-label", "service")
    page.select_option("#source-step", "5")
    page.fill("#source-queries", json.dumps(SOURCE["queries"], indent=2))
    page.click("#connect-source")
    # Connecting navigates to Services and runs discovery.
    expect(page.locator("#discovery-status")).to_contain_text("services found", timeout=30000)
    status = page.inner_text("#discovery-status")
    rows = page.locator("#service-inventory tbody tr")
    check("connect + discover lists all 11 services", rows.count() == 11, status)
    check("every service reporting cpu+mem", page.locator("#service-inventory .chip.tone-good").count() == 11)
    page.screenshot(path=SHOTS / "1-services-discovered.png", full_page=True)

    for i, (caller, callee) in enumerate(EDGES):
        page.click("#add-dependency")
        page.select_option(f'[aria-label="Caller for dependency {i + 1}"]', caller)
        page.select_option(f'[aria-label="Callee for dependency {i + 1}"]', callee)
    page.click("#save-topology")
    expect(page.locator("#topology-status")).not_to_be_empty(timeout=10000)
    check("save services & dependencies", not page.inner_text("#topology-error"), page.inner_text("#topology-status"))

    page.fill("#sensitivity", "6")
    page.click("#save-sensitivity")
    expect(page.locator("#sensitivity-status")).not_to_be_empty(timeout=10000)
    check("save manual sensitivity", not page.inner_text("#sensitivity-error"), page.inner_text("#sensitivity-status"))
    page.screenshot(path=SHOTS / "2-services-saved.png", full_page=True)

    nav(page, "integrations")
    page.click("#check-prom")
    expect(page.locator("#prom-check-result")).not_to_contain_text("Reading", timeout=60000)
    check("verify metrics", "verif" in page.inner_text("#prom-check-result").lower(), page.inner_text("#prom-check-result"))
    page.screenshot(path=SHOTS / "3-integrations-verified.png", full_page=True)

    nav(page, "overview")
    page.wait_for_timeout(800)
    page.screenshot(path=SHOTS / "4-overview-ready.png", full_page=True)
    check("no JS errors during setup", not errors, "; ".join(errors))
    browser.close()


def start_diagnosis(page, ts, method=None):
    nav(page, "diagnoses")
    if page.locator("#new-form").is_hidden():
        page.click("#new-btn")
    page.fill("#started", local_minute(ts))
    if method:
        page.locator(f"#methods >> text={method}").click()
    page.click("#start-btn")
    page.wait_for_timeout(800)


def wait_detail(page, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        text = page.inner_text("#detail")
        if any(s in text.lower() for s in ("root cause", "no significant", "failed", "insufficient", "partial")) \
                and "queued" not in text.lower() and "analyzing" not in text.lower():
            return text
        page.wait_for_timeout(1500)
    return page.inner_text("#detail")


def diagnose(p):
    browser, page, errors = open_app(p)
    # 1. Concurrent-fault incident.
    start_diagnosis(page, INCIDENT)
    err = page.inner_text("#new-error")
    check("submit concurrent-fault diagnosis", not err, err)
    text = wait_detail(page)
    page.screenshot(path=SHOTS / "5-diagnosis-result.png", full_page=True)
    found = [s for s in ("cartservice", "currencyservice") if s in text]
    check("diagnosis completes", "queued" not in text.lower(), text[:300].replace("\n", " | "))
    check("both concurrent root causes surfaced", len(found) == 2, f"found {found}")
    share_url = page.url

    # 2. Healthy window -> abstain.
    start_diagnosis(page, INCIDENT - 1500)
    text = wait_detail(page)
    check("healthy window abstains", "no anomaly" in text.lower() and "no root cause" in text.lower(), text[:200].replace("\n", " | "))
    page.screenshot(path=SHOTS / "6-healthy-window.png", full_page=True)

    # 3. Future time is rejected client-side.
    start_diagnosis(page, int(time.time()) + 3600)
    err = page.inner_text("#new-error")
    check("future incident time rejected", bool(err), err)
    page.click("#cancel-new")

    # 4. Queue a job whose window is not finished yet, then cancel it.
    start_diagnosis(page, int(time.time()) - 30)
    page.wait_for_timeout(1000)
    cancel = page.get_by_role("button", name="Cancel diagnosis")
    if cancel.count() == 0:
        cancel = page.locator("#detail button", has_text="Cancel")
    has_cancel = cancel.count() > 0
    if has_cancel:
        page.once("dialog", lambda d: d.accept())
        cancel.first.click()
        page.wait_for_timeout(1500)
    detail = page.inner_text("#detail").lower()
    check("queued diagnosis can be cancelled", has_cancel and "cancel" in detail, detail[:200].replace("\n", " | "))

    # 5. Search + status filter.
    page.fill("#job-search", "cartservice")
    page.wait_for_timeout(500)
    n_search = page.locator("#list li").count()
    page.fill("#job-search", "")
    page.select_option("#job-status", "cancelled")
    page.wait_for_timeout(500)
    n_cancelled = page.locator("#list li").count()
    page.select_option("#job-status", "all")
    page.wait_for_timeout(500)
    n_all = page.locator("#list li").count()
    check("search & status filters", n_search >= 1 and n_cancelled == 1 and n_all >= 3,
          f"search={n_search} cancelled={n_cancelled} all={n_all}")
    page.screenshot(path=SHOTS / "7-diagnoses-list.png", full_page=True)

    # 6. Export JSON of the concurrent-fault diagnosis via its shared link.
    browser2, page2, errors2 = open_app(p, path=share_url.replace(BASE, ""))
    page2.wait_for_timeout(1500)
    text2 = page2.inner_text("#detail")
    check("shared diagnosis link opens the result", "cartservice" in text2 and "currencyservice" in text2, share_url)
    exp = page2.get_by_role("button", name="Download result")
    ok = False
    if exp.count():
        with page2.expect_download(timeout=10000) as d:
            exp.first.click()
        path = d.value.path()
        data = json.loads(Path(path).read_text())
        ok = {"cartservice", "currencyservice"} <= set(data["result"]["root_causes"])
        (ROOT / "export.json").write_text(json.dumps(data, indent=2))
    check("export diagnosis JSON", ok)
    check("no JS errors during diagnosis", not errors and not errors2, "; ".join(errors + errors2))
    browser2.close()
    browser.close()


def persist(p):
    browser, page, errors = open_app(p)
    nav(page, "diagnoses")
    page.wait_for_timeout(1000)
    n = page.locator("#list li").count()
    check("history survives server restart", n >= 3, f"{n} diagnoses")
    nav(page, "services")
    page.wait_for_timeout(1000)
    deps = page.locator("#dependency-rows .dependency-row").count()
    check("services/dependencies survive restart", deps == len(EDGES), f"{deps} dependencies")
    check("sensitivity survives restart", page.input_value("#sensitivity") == "6", page.input_value("#sensitivity"))
    browser.close()
    for view in ("overview", "diagnoses", "alerts", "logs", "services", "integrations", "quality", "guide", "team"):
        b, pg, errs = open_app(p, path=f"/app?view={view}", viewport={"width": 375, "height": 800})
        pg.wait_for_timeout(800)
        expect(pg.locator(f"#view-{view}")).to_be_visible()
        overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        pg.screenshot(path=SHOTS / f"8-mobile-{view}.png", full_page=False)
        check(f"mobile {view}: no horizontal page scroll", overflow <= 1 and not errs, f"overflow={overflow}px; errors={errs}")
        b.close()
    b, pg, errs = open_app(p, path="/")
    links = pg.locator("a[href*='/app']").count()
    check("landing page links into workspace", links >= 1 and not errs, f"{links} links; {errs}")
    pg.screenshot(path=SHOTS / "0-landing.png", full_page=True)
    b.close()


if __name__ == "__main__":
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        {"setup": setup, "diagnose": diagnose, "persist": persist}[sys.argv[1]](p)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)

# End-to-end acceptance (browser)

For the current console, start with the controlled browser regression check:

```sh
# Once, in a development environment:
uv pip install playwright
uv run --no-sync python -m playwright install chromium
# With a local development server running:
uv run --no-sync python e2e/check_console_browser.py --url http://localhost:8000
```

It exercises navigation, diagnosis details, logs, service dependencies, and help
with controlled API responses. It does not save workspace data or verify live
vendor connectivity. The full workflow below uses a separate, writable test
workspace and synthetic monitoring data.

The joint-telemetry check imports synthetic metric, log, and trace files through
the browser, then verifies the saved evidence, repair estimate, probability
abstention, download, re-run, and mobile layout. It **saves test jobs**; use a
disposable database and a separate server port:

```sh
uv run --no-sync python e2e/check_joint_browser.py --url http://localhost:8047 --output e2e/shots/joint
```

Drives the real workspace UI against a live `corca-serve` and a stand-in Prometheus
that serves Online Boutique cpu/mem series with **two concurrent faults**
(cartservice CPU hog + currencyservice memory leak) and propagated CPU symptoms on
callers. No cluster or Docker needed.

```sh
# once: pip install playwright && python -m playwright install chromium
# Keep acceptance separate from an app already running on port 8000.
export CORCA_E2E_PORT=8040
export CORCA_E2E_URL=http://127.0.0.1:8040
export CORCA_E2E_METRICS_URL=http://127.0.0.1:9040
INC=$(( $(date +%s) - 400 ))
echo "$INC" > e2e/incident.txt
python e2e/fake_prometheus.py 9040 "$INC" &
PY=.venv/bin/python bash e2e/restart.sh fresh
python e2e/workflow.py setup      # connect → discover → 15 dependencies → sensitivity → verify
python e2e/workflow.py diagnose   # concurrent faults, healthy abstention, future-time check,
                                  # cancel, search/filter, shared link, JSON download
PY=.venv/bin/python bash e2e/restart.sh
python e2e/workflow.py persist    # history/settings after restart, 375px layout, landing page
```

Each phase exits non-zero on any failed check and writes screenshots to `e2e/shots/`.
The fixture is synthetic: it shows the application works end to end, not that the
diagnosis is accurate on real incidents.


The restart helper refuses to stop a PID that does not match its test server,
refuses an occupied application port, and exits nonzero if startup fails. It is
intended for an interactive terminal; process-supervising runners should start
`backend.api` in their own managed foreground session using the same database and
port. Set `CORCA_E2E_CHROMIUM` only when intentionally using a preinstalled browser;
otherwise Playwright uses its matching installed Chromium. All nine workspace
views are checked through their actual query-string routes during persistence
acceptance. Test state, screenshots, logs, and exports remain ignored by Git.

# CoRCA: Concurrent Root Cause Analysis for Microservices

CoRCA wraps an existing single-root-cause RCA method (BARO or CIRCA components from RCAEval)
in an iterative explain-away loop. Each round it takes the top root cause, removes
the symptoms that root cause explains (the root plus its transitive callers in the
Online Boutique call graph), and searches again. It stops when nothing anomalous
is left above a calibrated threshold `tau`.

## Setup (Python 3.12)

```bash
uv venv -p 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install --no-deps RCAEval==1.8.0
python -m pytest -q tests
```

## Data

`RE1-OB/` is the RCAEval Online Boutique metrics dataset: 125 single-fault cases,
5 services × {cpu, mem, disk, delay, loss} × 5 repeats. It comes from
https://zenodo.org/records/14590730.

## Run

```bash
# bounded baselines + CoRCA; use a new folder for older untracked results
corca-run --data RE1-OB --methods baro circa --out results/re1ob_current

# Top-k for the baselines, CoRCA set P/R/F1 with cross-validated tau
corca-summarize results/re1ob_current/runs.jsonl

# synthetic 2-fault sanity check (NOT a result - our own toy propagation model)
corca-demo
```

Each experiment saves `experiment.json` with settings, input/source hashes,
installed dependency versions, and the installed RCAEval source hashes. Resume
with the same command. A changed setting, source, dependency, or input requires a
new output folder. `--limit 3` is a smoke test and can be removed when resuming.
Use one writer per output folder. `--retry-failed` appends another attempt;
summaries use the latest attempt per case/method. Damaged JSONL records are
reported explicitly rather than skipped.

`--round-timeout 60` bounds each baseline and CoRCA rank call in a spawned
process. The timeout includes startup; custom timed rankers must be picklable.
CoRCA reuses the baseline ranking for its first round. `baseline_seconds` measures
one baseline diagnosis, while `corca_seconds` includes that first call and all
later rounds. These are evaluation times with `tau=None`, not the latency of a
deployed diagnosis that stops early. RCAEval fallback rankings count as errors.

Summaries include failed attempts in the score denominator and report missing
cases, errors, and timeouts. Outputs are `summary.csv`, `aggregate.csv`,
`report.json`, and per-method `calibration.json`. Thresholds are held out by
repeat ID; calibration fails explicitly if a fold lacks correctly ranked
single-fault training cases. Legacy files can be summarized with a warning,
but their failure detection and timing cannot be verified retroactively.

## Local Evaluation and CIRCA Input Requirements

The local lab and authenticated API are implemented; see `lab/README.md`.
Evaluate complete, recovered incidents without injecting additional faults:

```bash
runtime/.venv/bin/python -m runtime.evaluate --data data/local-circa-window-probe-v2 --out results/policy-v1-controls --methods baro circa --rates 0 --seeds 0 --workers 1 --timeout 60
cat results/policy-v1-controls/report.json
```

Use a new output folder when code or inputs change. Reports expose execution
success, errors, timeouts, insufficient evidence, and calibration availability.
Uncalibrated zero scores are conservative unavailable-prediction scores, not
measurements of the method's accuracy. Calibration uses only successful,
correct-first-root single-fault controls; dropout variants are not independent
incident repeats. `base_F1` is an oracle-root-count comparator.

CIRCA now uses RCAEval's PC graph learner and RHT scorer directly, with real
normal/fault sample counts and all graph nodes, including isolated metrics.
It does not resample five-second telemetry into invented one-second samples
or pad observations to a fixed future time. Graph conversion uses RCAEval's
cause-to-effect graph API. This is an adapted PC/RHT baseline, not an unchanged
RCAEval wrapper or a full reproduction of the original CIRCA paper. Historical
CIRCA results must not be pooled with this adapter's results.

Policy `pc-rht-observed-no-workload-v1` excludes every `*_workload` column
from CIRCA's PC/RHT input. Raw telemetry, BARO input, and CoRCA's shared
anomaly/residual evidence retain those counts. This fixed family-level rule
does not inspect fault truth or choose individually convenient columns.
Other singular dependencies still cause an explicit evidence rejection.

CoRCA now continues only when residual evidence is strictly above `tau`.
Equality stops, in both live diagnosis and offline scoring. Calibration files
carry feature and stopping policy versions; the API rejects older policies.
The local evaluator supports `--calibration` for frozen thresholds and rejects
incidents whose metric fingerprints overlap the calibration data.
Live results include `remaining_anomaly_score`, `remaining_service_scores`,
and `unexplained_services` (remaining service scores strictly above tau).
`partial_unexplained` means roots were returned but significant residual
evidence remains, for example after the three-round limit. `partial_timeout`
takes precedence when a round times out. These are partial suspect lists, not
complete incident resolutions. `diagnosed` means the residual stopping rule
was satisfied, not that the roots are proven correct. Offline summaries keep
execution `status` separate from `diagnosis_status` and use the residual at
the calibrated stopping point, not later diagnostic rounds run with tau=None.
See [the end-to-end runbook](docs/policy-v1-validation.md) for separate
collection, calibration, held-out evaluation, and release gates.
See [the CIRCA investigation](docs/circa-residual-investigation.md) for the
rejected scoring experiment and its local/public development comparison.

The conservative CIRCA input gate requires more normal samples than the number
of varying metrics plus two, and a nonsingular metric correlation matrix.
Insufficient or deterministically redundant telemetry returns
`insufficient_evidence`, rather than a fabricated ranking. These numerical
checks do not guarantee statistical validity or diagnosis accuracy.
See `docs/circa-fix.md` for the pilot findings and remaining validation work.

Missing-stream experiments use `--drop-telemetry 0.25 --seed 0` and a new output
folder. Root services receive no ground-truth protection. Freeze calibration
from separate full-telemetry controls. Fixed-threshold scoring rejects copied
calibration incidents, even when dropout settings or output folders differ:

```bash
corca-run --data RE1-OB-HELDOUT --methods baro circa --drop-telemetry 0.25 --seed 0 --out results/re1ob_holdout_drop25
corca-summarize results/re1ob_holdout_drop25/runs.jsonl --calibration results/re1ob_controls_new/calibration.json
```

The example paths above require a genuinely separate control/test split;
they are not directories automatically created by the runner.

## Running the diagnosis service

The API reads service metrics from Prometheus, Grafana Cloud Metrics, Datadog,
Dynatrace, or New Relic. Give it an incident time, or point Alertmanager at it.
Results and browser-managed connections are stored in SQLite and survive restarts.

Open `/` for the product page and `/app` for the workspace. Connect with the
service API key supplied by the deployment administrator. The overview shows
setup progress and recent diagnoses. Integrations connects a telemetry provider;
Services discovers reporting services and metric gaps, saves dependencies, and
loads matching calibration. Diagnoses supports search, status filters, shared
links, and JSON exports. Existing agents and OpenTelemetry collection can feed
the connected backend. See [telemetry setup](docs/telemetry-setup.md) for provider
credentials, query examples, resolution limits, and persistence behavior.

This is a **trusted-team, self-hosted beta**, with one shared API key and one
service replica. It does not implement individual identities, SSO, enforced roles,
tenant isolation, or an audit log. Use a private network or an HTTPS access gateway.
See [the beta review](docs/beta-review.md) for validation and rollout limits.

```bash
docker build -f runtime/Dockerfile -t corca:0.2.0 .
python -m runtime.deploy --image corca:0.2.0 --calibration results/controls-v1/calibration.json
# runtime.deploy targets the dedicated corca-lab cluster only.
# Other clusters: adapt runtime/deployment.yaml and mount your own source, calibration, and key.
```

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/diagnoses` `{"inject_time": <unix s>}` | key | queue a diagnosis; returns `202` + `id` |
| `GET /v1/diagnoses/{id}`, `GET /v1/diagnoses?limit=20` | key | result / history |
| `GET /v1/config`, `GET /v1/integrations` | key | capabilities and redacted integration configuration |
| `POST /v1/connections` | key | save one active source using server credential references; clears calibration |
| `POST /v1/services/discover` | key | discover services and per-metric coverage from the active source |
| `POST /v1/topology`, `POST /v1/calibration` | key | save service scope/dependencies and load matching calibration |
| `POST /v1/integrations/check` | key | verify recent metrics from the active source |
| `POST /v1/alerts` | key | Alertmanager webhook; repeats of one alert group are deduplicated |
| `POST /diagnose` | key | synchronous, caller uploads the metric matrix (evaluation path) |
| `GET /healthz`, `GET /readyz` | none | liveness; readiness = calibration loaded and job store usable |
| `GET /openapi.json` | key | API schema |

Auth is `X-API-Key: <token>` or `Authorization: Bearer <token>`; the token is in
`runtime/.state/api-token`. Example:

```bash
TOKEN=$(cat runtime/.state/api-token)
curl -s -X POST localhost:8000/v1/diagnoses -H "X-API-Key: $TOKEN" -H 'content-type: application/json' \
     -d "{\"inject_time\": $(($(date +%s) - 180))}"
curl -s localhost:8000/v1/diagnoses/<id> -H "X-API-Key: $TOKEN"   # status: queued|running|done|failed
```

A job waits until `inject_time + anomaly_seconds` (default 120 s) plus 15 s of
scrape lag, then pulls `normal_seconds` (default 300 s) of history. A finished
job's `result.status` is `diagnosed`, `partial_unexplained`, `partial_timeout`,
`insufficient_evidence` or `no_significant_anomaly`. Provider network failures are
retried 3 times, 30 s apart; other failures are reported in `error`.

Alertmanager receiver:

```yaml
receivers:
  - name: corca
    webhook_configs:
      - url: http://corca-api.corca.svc:8000/v1/alerts
        send_resolved: false
        http_config:
          authorization: {credentials_file: /etc/alertmanager/corca-token}
```

The diagnosis window starts `alert_lead_seconds` (default 60) before the alert's
`startsAt`, because alerts fire after their `for:` delay.

**Source config.** `provider` defaults to `prometheus`. `queries` maps metric kinds
to provider queries returning one series per service, labelled/grouped by
`service_label`. Use kinds `cpu`, `mem` (resource metrics
are kept as candidates during explain-away), `latency-90`, `error`, `workload`.
`services` filters which services count; `edges` (`[caller, callee]`) is the
call graph. Legacy configurations fall back to Online Boutique's graph; use the
Services page to confirm actual dependencies for another environment. Live
service IDs containing underscores are escaped in internal metric column names
and decoded for analysis and display.

**Operating limits.** The bundled Online Boutique config has CPU and memory only;
the lab's latency/error features come from trace files, which are not in
Prometheus. Without latency queries (OpenTelemetry spanmetrics, a service mesh)
delay faults are diagnosed only through their resource side effects. Calibrate
tau on controls collected with the same source config you deploy. One replica only
(SQLite on a ReadWriteOnce volume).

## Layout

| Path | What |
|---|---|
| `corca/topology.py` | Online Boutique call graph; "what does root r explain" |
| `corca/data.py` | Loads RCAEval-format cases (same windowing as RCAEval's `main.py`) |
| `corca/base.py` | Adapters: RCAEval method → ranked services; BARO-style anomaly score |
| `corca/corca.py` | The explain-away loop + `tau` calibration |
| `corca/metrics.py` | Top-k, set precision/recall/F1 |
| `corca/synthetic.py` | Toy multi-fault generator for tests only |
| `scripts/` | Evaluation runner, summary, synthetic demo |

## Multi-fault cases

Use the same folder layout, and either name the fault folder with `+`
(`cartservice_delay+currencyservice_cpu/1/`) or add a `ground_truth.json`
file: `{"root_causes": ["cartservice", "currencyservice"]}`.
Score these cases with per-method thresholds from separate single-fault data:
`corca-summarize ... --calibration results/re1ob_controls_new/calibration.json`.

## Known limitations (to test, and to report)

- A second root cause that is a *caller* of the first can be hidden. With
  `keep_resource=True` its own cpu/mem/disk metrics stay candidates, so
  resource faults survive but delay/loss faults on that service don't.
  `--no-keep-resource` is the ablation.
- Explain-away uses the supplied dependency graph (or static fallback), not a proven causal graph.
- The confidence field is an anomaly-score ratio, not a calibrated probability.
- Real simultaneous-fault validation requires a Kubernetes testbed. Synthetic
  faults and independently merged single-fault traces do not establish this claim.
- The first round assumes an incident alert. The API supports a separately
  configured anomaly gate for healthy-case abstention; continuous live ingestion
  and production-quality calibration are not established.

> Optional research tooling and historical evaluation context. For the user-facing
> beta, start with [the application README](../../README.md) and
> [operations guide](../guides/operations.md). New data collection is not required for beta use.

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
python -m scripts.run_eval --data RE1-OB --methods baro circa --out results/re1ob_current

# Top-k for the baselines, CoRCA set P/R/F1 with cross-validated tau
python -m scripts.summarize results/re1ob_current/runs.jsonl

# synthetic 2-fault sanity check (NOT a result - our own toy propagation model)
python -m scripts.demo_synthetic
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
.venv/bin/python -m lab.evaluate --data data/local-circa-window-probe-v2 --out results/policy-v1-controls --methods baro circa --rates 0 --seeds 0 --workers 1 --timeout 60
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
See [the evaluation runbook](evaluation.md) for separate collection, calibration,
and held-out evaluation. Use the [pilot checklist](../guides/pilot.md) for deployment acceptance.

The conservative CIRCA input gate requires more normal samples than the number
of varying metrics plus two, and a nonsingular metric correlation matrix.
Insufficient or deterministically redundant telemetry returns
`insufficient_evidence`, rather than a fabricated ranking. These numerical
checks do not guarantee statistical validity or diagnosis accuracy.

Missing-stream experiments use `--drop-telemetry 0.25 --seed 0` and a new output
folder. Root services receive no ground-truth protection. Freeze calibration
from separate full-telemetry controls. Fixed-threshold scoring rejects copied
calibration incidents, even when dropout settings or output folders differ:

```bash
python -m scripts.run_eval --data RE1-OB-HELDOUT --methods baro circa --drop-telemetry 0.25 --seed 0 --out results/re1ob_holdout_drop25
python -m scripts.summarize results/re1ob_holdout_drop25/runs.jsonl --calibration results/re1ob_controls_new/calibration.json
```

The example paths above require a genuinely separate control/test split;
they are not directories automatically created by the runner.

## Use calibrated results in the local lab

For normal application setup, follow [operations](../guides/operations.md).
The following command deploys only into the dedicated `corca-lab` cluster; install
server dependencies using the [contributor setup](../../CONTRIBUTING.md) first.

```sh
docker build -f backend/Dockerfile -t corca:0.2.0 .
python -m lab.deploy --image corca:0.2.0 --calibration results/controls-v1/calibration.json
```

The [API reference](../api/README.md) defines current authentication, readiness,
job behavior, Alertmanager delivery, and request contracts. The
[telemetry guide](../guides/telemetry.md) defines provider queries and service labels.
Keep one canonical source for those contracts rather than copying an endpoint table
into research instructions.

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
`python -m scripts.summarize ... --calibration results/re1ob_controls_new/calibration.json`.

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

## Interpretation and evaluation principles

- Dependency paths describe possible propagation; they do not establish causation.
- Relative anomaly scores and score gaps are not probabilities.
- Synthetic examples are sanity checks, not customer incident accuracy evidence.
- Calibrate stopping thresholds on separate single-fault controls; freeze them before held-out evaluation.
- Report missing telemetry, false extra causes, set precision/recall/F1, exact match, timeouts, and runtime.
- Keep resource signals available when explaining away callers; assess that choice with the existing ablation.

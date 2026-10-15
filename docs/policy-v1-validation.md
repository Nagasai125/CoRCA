# Policy V1: End-to-End Validation

## Status

This is a controlled research implementation, not an approved production RCA
service. CI/CD is outside the current scope. Passing the workflow does not
establish diagnosis accuracy, calibrated uncertainty, or a production SLA.
The older readiness document and `results/local-ready-v2` are historical
snapshots, not measurements of the current feature and stopping policies.

## Fixed Policies

- `pc-rht-observed-no-workload-v1`: exclude all `*_workload` columns from
  CIRCA graph learning and RHT scoring, without changing or deleting raw data.
  BARO input remains unchanged. Counts remain available to CoRCA's shared
  anomaly/residual calculation and explain-away loop.
- `residual-strictly-above-tau-v1`: continue after a diagnosed root only when
  the remaining anomaly score exceeds tau. Equality stops. Offline scoring
  and live diagnosis use the same boundary for both base methods.
- Require actual normal samples greater than graph metric count plus two,
  at least ten observations in both windows, finite values, and a nonsingular
  graph-input correlation matrix. These are numerical gates, not statistical
  power guarantees. Other dependencies still produce `insufficient_evidence`.
- Calibration files identify the feature and stopping policies. Configured
  API/deployment calibration rejects incompatible or unversioned policies.
  Explicit per-request tau remains a caller-controlled diagnostic override.

Both evaluators record policy identity. Local evaluation fingerprints also
cover workers, injection timestamps, reporting scripts, inputs, packages,
and ranker sources. Changed configurations require new output folders;
unfingerprinted raw results cannot be silently adopted during resume.

## Development Evidence

`data/local-circa-window-probe-v2/adservice_cpu/1` completed and recovered:
59 healthy and 55 fault observations. Preparation leaves 38 varying metrics;
their full-window correlation matrix has rank 36. Excluding ten workload
streams yields 28 graph metrics with full-rank healthy and combined-window
correlation matrices. Eight latency/error streams remain unusable.

This incident helped select the feature policy. It is development/calibration
data and cannot be called an untouched test. A threshold fitted from its one
correct-first-root residual is a smoke-test threshold, not production-quality
calibration. Do not retune it after observing the two-fault smoke results.

Current-policy evidence folders:

- `results/policy-v1-controls`: the existing longer development control, both
  methods, full CoRCA rounds, calibration metadata and fingerprints.
- `data/local-policy-v1-heldout-smoke`: rejected before injection because
  currencyservice restarted during the healthy window. Kubernetes recorded
  `OOMKilled` at the existing 128 MiB container limit; node memory pressure
  was false. No fault was injected and no diagnosis score is valid for it.
- `data/local-policy-v1-heldout-smoke-v2`: the bounded retry, using the same
  resource limits and policies after the container restarted. It completed
  and recovered with 59 healthy and 54 faulty observations, with no restarts
  during the run and no active chaos experiments left afterward.
- `results/policy-v1-heldout`: fixed-threshold evaluation, telemetry loss and
  ablations of that fresh incident. One incident with repeated dropout seeds
  is still one incident, not an independent accuracy sample per seed.
- Docker image `corca-api:policy-v1`: isolated nonroot, read-only HTTP smoke
  checks. It is not automatically rolled into the running Kubernetes API.
- `results/policy-v1-public-smoke`: three public adservice CPU incidents per
  method, six successful executions. Both methods rank adservice first on
  all three; repeat-held-out CoRCA scoring returns the exact singleton on two
  of three for each method. This small, same-service smoke is not a full public
  benchmark or an independent deployment accuracy estimate.

Current verification with `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1`: 68
runtime/core tests pass; 42 core-only tests pass with one runtime-dependent
diagnostic integration test skipped. Isolated real HTTP checks return only
adservice for the development control with both methods, reject unauthenticated
requests, and run as UID 10001 on a read-only filesystem. Capacity rejection,
threshold equality, policy incompatibility, unchanged BARO input, and dataset
overlap checks are covered. These checks are not a sustained-load study.

The fresh-incident evaluation completed all 24 planned configurations:
12 BARO and 12 adapted PC/RHT CIRCA, with no errors, timeouts or evidence
rejections. Frozen thresholds came from the separate development control;
neither thresholds nor policies were retuned against this incident.

| Missing streams | BARO-backed CoRCA F1 | CIRCA-backed CoRCA F1 | Seeds |
| --- | ---: | ---: | ---: |
| 0% | 1.000 | 0.400 | 1 |
| 10% | 1.000 | 0.533 | 3 |
| 25% | 0.889 | 0.489 | 3 |
| 50% | 0.689 | 0.656 | 3 |

These rows use resource retention and the observed normal-window topology.
Every row reuses the same one incident; the seeds are not independent
incident repeats. Full-telemetry BARO-backed CoRCA returns exactly adservice
and currencyservice. CIRCA-backed CoRCA returns adservice,
productcatalogservice and shippingservice: one missed root and two false
suspects. Its initial baseline already ranks currencyservice fourth, so the
problem is not merely the stopping threshold. Resource-removal and static
topology ablations do not fix the full-telemetry miss on this incident.

The final container HTTP smoke reproduces those same root lists using
configured calibration, without a per-request tau override. It is evidence
of execution consistency, not evidence that CIRCA's answer is correct.
See `results/policy-v1-controls/container-verification.json`,
`results/policy-v1-heldout/verification.json`, and the held-out raw diagnoses.

**Release decision: not production ready.** Do not promote this one-control
calibration or hide the CIRCA failure behind successful execution counts.
The next research work is to investigate the baseline's missed currency CPU
fault using development data, improve telemetry coverage and calibration
support, then evaluate a predeclared policy on genuinely new incidents.

That investigation is now recorded in
[the CIRCA residual investigation](circa-residual-investigation.md). A residual
noise-floor candidate fixed this one local case but regressed on five public
development cases, so it was not promoted. This incident is now development
data, not an untouched test for future algorithm changes. Current API results
flag unresolved above-tau evidence as `partial_unexplained`; the old container
verification's `diagnosed` label is historical. Offline reports distinguish
execution success from diagnosis status. None of these reporting changes
improves the underlying CIRCA root list or establishes production readiness.

## Reproduce Existing Evidence

Run from the repository root. Exact resume requires unchanged sources,
dependencies and inputs. Otherwise use a new output directory.

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 runtime/.venv/bin/python -m pytest -q tests runtime
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m pytest -q tests
runtime/.venv/bin/python -m runtime.evaluate --data data/local-circa-window-probe-v2 --out results/policy-v1-controls --methods baro circa --rates 0 --seeds 0 --workers 1 --timeout 60
runtime/.venv/bin/python -m runtime.evaluate --data data/local-policy-v1-heldout-smoke-v2 --out results/policy-v1-heldout --methods baro circa --rates 0 .1 .25 .5 --seeds 0 1 2 --workers 1 --timeout 60 --calibration results/policy-v1-controls/calibration.json
cat results/policy-v1-heldout/report.json
cat results/policy-v1-heldout/aggregate.csv
docker build -f runtime/Dockerfile -t corca-api:policy-v1 .
```

`--calibration` never fits thresholds on test cases. It checks method coverage,
policy compatibility, and calibration/test metric-hash overlap, including
copied incidents under renamed folders. Separate data paths alone are not
proof of a valid split. Single-fault held-out cases are scored too.

After calibration support and test results have been reviewed, deploy only
to the dedicated lab with an explicit matching image. This is not a
production deployment command, and the one-control smoke calibration above
is not approved for promotion:

```bash
runtime/.venv/bin/python -m runtime.deploy --image corca-api:policy-v1 --calibration results/policy-v1-calibration-new/calibration.json
```

## Broader Validation Campaign

Before running, agree on accuracy, extra-suspect, abstention and latency
targets. The suggested starting matrix below uses five repeats per scenario;
it is not a power calculation or a release criterion. Collection takes hours.
Use a plugged-in Mac with its lid open; caffeinate cannot guarantee protection
against host suspension, Docker pause, or a manual lid close.

Check only the dedicated lab:

```bash
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca get pods
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca get stresschaos,networkchaos
```

Collect separate new controls, then fit thresholds once:

```bash
caffeinate -dims .venv/bin/python -m lab.run_cases --out data/policy-v1-calibration-new --only currencyservice_cpu adservice_cpu checkoutservice_cpu --repeats 5 --normal-seconds 300 --fault-seconds 300
runtime/.venv/bin/python -m runtime.evaluate --data data/policy-v1-calibration-new --out results/policy-v1-calibration-new --methods baro circa --workers 1 --timeout 60
```

Freeze and review the policies and calibration artifact before collecting
untouched evaluation incidents, including new single-fault controls:

```bash
caffeinate -dims .venv/bin/python -m lab.run_cases --out data/policy-v1-test-new --repeats 5 --normal-seconds 300 --fault-seconds 300
runtime/.venv/bin/python -m runtime.evaluate --data data/policy-v1-test-new --out results/policy-v1-test-new --methods baro circa --workers 1 --timeout 60 --calibration results/policy-v1-calibration-new/calibration.json
```

Inspect every failed, timed-out and insufficient-evidence attempt. Do not
discard inconvenient incidents or tune tau on test results. A contaminated
evaluation requires another genuinely new test set after development ends.

## Production Gates Still Open

- Adequate independent calibration support across root services/fault types;
  predeclared held-out acceptance targets, uncertainty intervals grouped by
  incident, and evaluation of healthy/no-alert windows.
- Diagnosis quality under missing root telemetry, dependent faults, delay
  faults, topology drift, and incomplete trace instrumentation. The initial
  experiment does not cover all of these.
- End-to-end deadline and worker cancellation tests under sustained load,
  restart/recovery and resource pressure on the intended deployment hardware.
  A unit capacity test and an isolated HTTP smoke are not an SLA/load study.
- Investigate periodic Online Boutique currencyservice memory exhaustion
  under the tracing-enabled testbed. Never accept a contaminated healthy
  window, and recollect both calibration and test data after resource or
  instrumentation changes rather than silently mixing deployments.
- Operational ownership, incident retention/privacy, authentication and secret
  rotation, alerting, rollback, and environment-specific deployment review.
- Domain-owner review of the explain-away assumptions and the distinction
  between adapted PC/RHT rankings and proven causal identification.

Do not promote pilot thresholds, historical CIRCA scores, or heuristic
confidence ratios as production accuracy or calibrated probabilities.

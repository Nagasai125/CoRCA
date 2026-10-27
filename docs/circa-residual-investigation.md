# CIRCA Residual Investigation

## Decision

Do not promote the experimental residual noise floor. Keep the existing
`pc-rht-observed-no-workload-v1` rankings and frozen smoke-test thresholds.
The project remains a research implementation, not production ready.

The previously fresh two-fault smoke incident has now been inspected during
development. It must not be reused as an untouched test of a future policy.
All comparisons below are development evidence, not generalization estimates.

## What We Found

In `data/local-policy-v1-heldout-smoke-v2/currencyservice_cpu+adservice_cpu/1`,
the adapted PC/RHT baseline ranks productcatalogservice and shippingservice
above currencyservice. Their memory residual standard deviations are about
0.0021 MiB and 0.0060 MiB. Small deviations therefore produce large
standardized residual scores. Currency CPU evidence remains strong but the
three-round CoRCA budget is consumed by other services.

The original method also standardizes regression residuals against healthy
residual variation. This observation alone is not an implementation bug.
Our PC/RHT adapter is not the original architecture-informed CIRCA method.
See the [CIRCA paper](https://arxiv.org/abs/2206.05871) and
[original implementation](https://github.com/NetManAIOps/CIRCA).

An offline-only candidate scales regression residuals by at least 1% of the
healthy metric median (with existing fraction/count floors). It uses no fault
truth for ranking and does not change CoRCA's shared stopping score. It can
also suppress genuine memory-fault evidence relative to propagated CPU
effects, which is why checking more than the one failing incident matters.

| Development set | Existing adapter mean F1 | Candidate mean F1 | Existing exact sets | Candidate exact sets |
| --- | ---: | ---: | ---: | ---: |
| One local concurrent incident | 0.400 | 1.000 | 0/1 | 1/1 |
| Five public RE1-OB incidents | 0.733 | 0.667 | 2/5 | 1/5 |

The public cases include CPU, memory, and delay faults. Two separate public
adservice CPU controls supply their tau; the local comparison uses the
existing separate local single-fault control. The candidate and existing
adapter obtain the same tau within each dataset. Neither comparison justifies
a release claim, and the public regression rejects promotion of this candidate.

## Full Predeclared Public Screen

The five public incidents above are a subset. `structural-circa-plan.md`
predeclares 98 RE1-OB cases: repeats 1 through 4 of all 25 service/fault
combinations, excluding the two `adservice_cpu` controls that supply tau.
Repeat 5 was not inspected. Both policies received the same frozen tau 4.967.

| Policy | Mean F1 | Exact sets | Top-1 service |
| --- | ---: | ---: | ---: |
| `pc-rht-observed-no-workload-v1` | 0.532 | 0.204 | 0.561 |
| `experimental-rht-residual-floor-1pct-v1` | 0.500 | 0.153 | 0.520 |

The candidate reduces all three screen quantities, so the predeclared rule
rejects it on the full set, not only on the five-incident subset. By fault:

| Fault | Existing F1 | Candidate F1 | Existing exact | Candidate exact |
| --- | ---: | ---: | ---: | ---: |
| cpu | 0.824 | 0.696 | 0.647 | 0.412 |
| mem | 0.658 | 0.592 | 0.300 | 0.200 |
| disk | 0.458 | 0.433 | 0.000 | 0.000 |
| loss | 0.392 | 0.392 | 0.100 | 0.100 |
| delay | 0.400 | 0.442 | 0.050 | 0.100 |

A residual floor can only raise sigma, so it can only lower a score. It helps
when the suppressed stream is a false suspect, as with the lab's 0.08% memory
drift, and hurts when the true root is itself quiet. CPU faults are the
worst-affected class here, which is the same class the floor repairs on the
local incident. One relative constant does not separate those two situations.
This is a development screen on single-fault public data; it does not measure
concurrent-fault behaviour, and it does not establish that some other floor
derived from measurement resolution would fail.

`RE1-OB/checkoutservice_cpu/4` returned `error` under both policies. It is a
pre-existing failure unrelated to the candidate and is still uninvestigated.

Artifacts: `results/floor-public-development-v1/` holds `comparison.csv`,
`report.json`, `diagnoses.jsonl` and `experiment.json`.

```bash
PYTHONPATH=. .venv/bin/python -m scripts.investigate_circa --dataset re1-ob \
  --control RE1-OB/adservice_cpu/1 RE1-OB/adservice_cpu/2 \
  --case $(ls -d RE1-OB/*/[1-4] | grep -v -E 'adservice_cpu/[12]$') \
  --out results/floor-public-development-v1 --timeout 60
```

## Evidence And Commands

Saved artifacts:

- `results/circa-residual-local-development/report.json`
- `results/circa-residual-public-development/report.json`
- Each folder contains `comparison.csv`, per-round metric/parent/score traces
  in `diagnoses.jsonl`, input/source/package hashes in `experiment.json`, and
  `source-snapshot.tar.gz` preserving the source used for those experiments.

Run from the repository root. New output folders are required. These commands
rerun development experiments, not new held-out tests. The experimental
candidate is not available through the API or standard evaluation runner.

```bash
runtime/.venv/bin/python -m scripts.investigate_circa --control data/local-circa-window-probe-v2/adservice_cpu/1 --case data/local-policy-v1-heldout-smoke-v2/currencyservice_cpu+adservice_cpu/1 --out results/circa-residual-local-rerun --timeout 60
runtime/.venv/bin/python -m scripts.investigate_circa --dataset re1-ob --control RE1-OB/adservice_cpu/1 RE1-OB/adservice_cpu/2 --case RE1-OB/adservice_cpu/3 RE1-OB/adservice_mem/1 RE1-OB/cartservice_mem/1 RE1-OB/productcatalogservice_cpu/1 RE1-OB/checkoutservice_delay/1 --out results/circa-residual-public-rerun --timeout 60
```

## Reporting Correction

The old API called the failing three-root result `diagnosed` even though
currencyservice's remaining score was 21.277, above tau 2.947. The corrected
API returns `partial_unexplained` with remaining metric-derived service
evidence. Rankings, root lists, and thresholds are unchanged. Remaining
services are evidence to investigate, not additional diagnosed roots.

Offline local and public summaries now separate execution `status` from
`diagnosis_status`. They use the residual immediately after the last root
selected under tau, not the final residual of extra tau=None diagnostic
rounds. Equality with tau is resolved by the existing stopping rule. A
`diagnosed` label means this rule stopped, not that ground truth was recovered.
Consumers must handle the new partial status and must not auto-remediate from
these research suspect lists.

The container now defaults `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1`
to avoid each worker creating a numerical thread pool under the lab API's
one-CPU limit. These environment settings may be overridden deliberately for
other hardware. A resource-contention run had three worker-startup timeout
failures; the capped runtime/core suite passed all 68 tests in 17.20 seconds.
After isolating optional runtime imports, the final runtime suite passed in
17.56 seconds; the core-only environment passed 42 tests and skipped the one
local-runtime integration test without installing FastAPI.
That rerun is not a sustained-load or production deadline guarantee. The
existing Starlette/httpx TestClient deprecation warning remains.

## Verified Reporting Build

`results/diagnosis-evidence-v1` contains a new fingerprinted 24-configuration
evaluation using the unchanged pilot calibration. Every root prediction, F1,
exact-set score, and execution status matches `results/policy-v1-heldout`.
All 24 executions succeeded. The new stopping-evidence classification is:

- BARO: 12 `diagnosed`.
- Adapted CIRCA: seven `partial_unexplained`, five `diagnosed`.

These counts are not accuracy counts. The same single incident is reused for
dropout seeds and ablations; a resolved residual can still yield wrong roots.

`verification.json` records actual HTTP responses from
`corca-api:diagnosis-evidence-v1`, image identity, input/report hashes, test
results, and limitations. Both methods return only adservice on the control.
On the concurrent incident, BARO returns adservice and currencyservice;
CIRCA returns its same three suspects with `partial_unexplained` and includes
currencyservice in remaining above-tau evidence. HTTP and offline root lists
and residual scores agree. No per-request tau override was used.

The disposable container used UID 10001, a read-only root filesystem, no
external network, a one-CPU/1536 MiB allocation, dropped capabilities, and
no-new-privileges. Unauthenticated diagnosis returned HTTP 401. It was removed
after verification; the new image was not deployed to Kubernetes. The lab
had no active chaos experiments, but currencyservice's 16th restart was
`OOMKilled` at `2026-10-15T20:37:40Z` under its unchanged 128 MiB limit.
This remains a collection-stability blocker, not a reason to discard earlier
valid, recovered incidents or to claim new production readiness.

To inspect results or repeat the tests:

```bash
cat results/diagnosis-evidence-v1/report.json
cat results/diagnosis-evidence-v1/verification.json
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 runtime/.venv/bin/python -m pytest -q tests runtime
```

## Next Work

1. Investigate representative healthy history and architecture-informed graph
   assumptions as separately named, development-only policies. Do not silently
   relabel a new method as the current adapter or select features using truth.
2. Investigate the lab currencyservice memory exhaustion before a longer
   collection campaign. Deployment/resource changes require recollecting
   controls and evaluation incidents rather than mixing deployments.
3. Freeze the next policy, calibration split, and accuracy/abstention/latency
   targets before collecting genuinely new independent fault repeats.
4. Validate healthy windows, missing root telemetry, dependent faults,
   topology drift, and sustained API load before any production promotion.

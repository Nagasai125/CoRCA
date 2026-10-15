# CIRCA Failure Investigation

The investigation below records the original adapter and short-window pilot.
The current policy and end-to-end commands are in `policy-v1-validation.md`.
The new policy excludes workload counts from PC/RHT input; other singular
streams are still rejected. Original artifacts remain historical evidence.

## Evidence

The original `results/local-ready/diagnoses.jsonl` contains 200 evaluations:
100 BARO runs completed, while CIRCA had 76 errors and 24 completed runs.
All four CIRCA single-fault calibration controls failed. No valid CIRCA tau
could be fitted, so its CoRCA predictions were unavailable and scored zero.
This is not evidence that a successfully calibrated CIRCA has zero accuracy.

Reproducing `data/local-ready/adservice_cpu/1` exposed the upstream exception:
`Data correlation matrix is singular. Cannot run fisherz test.`
After preparation and constant removal, this case has 32 observations,
14 normal observations, and 39 varying metrics. Payment, checkout, and email
workload counters are perfectly correlated in this window. Longer collection
alone does not resolve deterministic counter dependencies.

RCAEval's wrapper replaced the exception with column-order fallback ranks.
Its RHT helper also hard-codes one-second resampling, a fixed normal window,
and scoring at injection plus 300 seconds. The short local data has five-second
samples and a deliberately removed fault-settling interval. Interpolation and
extrapolation must not be counted as new independent observations.

## Implemented Fix

- Call the existing PC and RHT components directly, so graph-learning exceptions
  remain visible rather than becoming fallback ranks.
- Use the actual observed normal/fault rows, without resampling or future padding.
- Preserve isolated metric nodes and use the library's causal edge conversion.
- Reject under-supported or singular inputs explicitly. No injected jitter,
  truth-dependent feature selection, or automatic deletion of correlated metrics.
- Preserve the typed rejection through the timeout worker. The API returns an
  `insufficient_evidence` response with a reason, not an internal-server failure.
- Report execution status, calibration availability, failure stage, and elapsed
  baseline time even on failed calls. Failed/timeout controls cannot calibrate tau.
- Preserve the original experiments. A report-only audit is saved separately
  in `results/local-ready-audit`; corrected-code runs use `results/local-ready-v2`.

This adapter changes window handling, node inclusion, and graph conversion.
It must be labeled as an adapted PC/RHT baseline and evaluated afresh; previous
CIRCA numbers are historical, not interchangeable with this version.

## Reproduce

From the project root:

```bash
runtime/.venv/bin/python -m pytest -q tests runtime/test_api.py runtime/test_evaluate.py
runtime/.venv/bin/python -m runtime.evaluate --data data/local-ready --out results/local-ready-current-policy --methods baro circa --rates 0 .1 .25 .5 --seeds 0 1 2 --workers 2
cat results/local-ready-v2/calibration.json
cat results/local-ready-v2/aggregate.csv
```

Resume only with unchanged inputs, code, configuration, and dependencies.
For report-only analysis of older results, choose a new audit folder:

```bash
runtime/.venv/bin/python -m runtime.evaluate --diagnoses results/local-ready/diagnoses.jsonl --out results/local-ready-audit-new
```

## Remaining Work At The Original Investigation

The current pilot is insufficient for CIRCA calibration and for production
claims. Define an explicit, truth-independent metric schema that resolves
redundant shared workload counters, then collect longer healthy and fault
windows. At five-second cadence, the present 39-metric schema needs at least
42 actual normal observations just to pass the conservative sample-count gate;
passing this gate is not a guarantee of adequate statistical power.

Use more independent controls (including checkout CPU) and independent
multi-fault repeats. Keep calibration and held-out test incidents separate;
all missing-stream seeds for an incident must stay in the same split. Freeze
the schema and thresholds before evaluating held-out faults.

BARO calibration currently uses only two correct-first-root controls out of
four. Its extra suspects, missed roots, and the explain-away assumptions still
need case-level investigation. No threshold was hand-tuned against multi-fault
truth, and this fix does not establish production-ready accuracy.

The API image `corca-api:circa-fix` is built separately. The running Kubernetes
API is not automatically upgraded with weak pilot calibration.

## Verification Results

- 35 tests passed in the runtime environment; the core-only environment passed
  its 24 tests. Coverage includes isolated metrics, actual window boundaries,
  graph orientation, singular/short inputs, typed worker errors, and reports.
- The nonroot, read-only Docker image passed real authenticated HTTP CIRCA
  diagnosis and singular-input rejection checks with networking disabled except
  for the container's own loopback interface.
- Three longer-window public incidents completed for both methods (six runs),
  with CIRCA identifying the injected adservice first in all three. This is a
  smoke test, not a full benchmark or an independent accuracy estimate.
- The corrected local evaluation finished 200 unique configurations: 100 BARO
  successes and 100 CIRCA insufficient-evidence responses, with no missing
  configurations. All BARO rankings and round values match the original run.
- Source hashes stayed unchanged during evaluation. Resuming the same command
  scheduled no new evaluations. Original data and results remain preserved.

The local CIRCA calibration remains unavailable. The fix makes unsupported
inputs and unavailable predictions explicit; it does not manufacture an
accuracy improvement or turn the pilot into production validation.

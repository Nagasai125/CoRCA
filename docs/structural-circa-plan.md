# Structural CIRCA Development Plan

Recorded before running the structural candidates. These experiments are
development only; the current API method and calibration are unchanged.

## Fixed Candidates

- Existing `pc-rht-observed-no-workload-v1` as the comparator.
- `experimental-structural-rht-v1`: a service-architecture metric graph and
  the existing RCAEval linear RHT scorer, without descendant adjustment.
- `experimental-structural-rht-da-v1`: the same graph and scorer, plus
  RCAEval's descendant adjustment with the paper's three-sigma threshold.

Use the existing observation windows and preprocessing. Exclude `*_workload`
as in the current adapter. Map CPU/memory/disk/socket metrics to saturation,
latency metrics to latency, and error/log-error metrics to errors. Interpret
RE1 `*_load` as traffic; this dataset-schema assumption requires owner review
before promotion. Unknown varying metric kinds cause evidence rejection,
not silent deletion. Every observed metric remains a graph node, including
metrics from services absent from the supplied topology.

Use the intra-service and cross-service golden-signal relations described
in [CIRCA, Section 4](https://arxiv.org/html/2206.05871v1). Propagate parents
through unobserved meta metrics and carry error indicators to callers.
Reject cyclic service/metric graphs. This is a single-family mapping with
linear regressions, not the paper's full component-specific mapping or SVR
implementation and not a claim of faithful full reproduction.

Structural regressions need at least ten observed samples per window and
more healthy samples than each node's parent count plus two. They do not use
PC's global correlation-inversion gate. Collinear parents use sklearn's
ordinary least-squares minimum-norm fit; this does not identify individual
causal coefficients. Do not swallow regression errors into fallback rankings.
Preserve actual rows, no interpolation, jitter, truth-selected features,
residual-noise floors, hand-tuned tau, or additional CoRCA rounds.

## Development Split And Decision

Use local historical control `data/local-circa-window-probe-v2/adservice_cpu/1`
and the already inspected concurrent incident under
`data/local-policy-v1-heldout-smoke-v2`. Both remain old-deployment development
data and are not pooled with future incidents from the new memory policy.

For public development, use two separate controls, `RE1-OB/adservice_cpu/1`
and `/2`. Evaluate the 98 other RE1-OB cases from repeats 1 through 4.
Do not inspect repeat 5 for candidate selection in this investigation. That
repeat is a reserved comparison, not a promise that historical results have
never been viewed. Calibration is method-specific, fitted only on correct-first
single-fault controls at q=0.9. Report missing calibration and every failure.

Reject promotion if a candidate misses either local root or reduces public
development mean F1, exact-set rate, or top-1 service accuracy relative to
the current adapter. This is a conservative development screen, not a release
criterion or a power calculation. Do not tune candidates after looking at
reserved outcomes. A candidate that passes still requires broader independent
calibration and new concurrent incidents, including missing telemetry.

## Memory Policy Gate

The new lab-only policy sets currencyservice to a 256 MiB container limit,
128 MiB request, and `NODE_OPTIONS=--max-old-space-size=64`; CPU and tracing
are unchanged. The option limits V8 old space, not total process memory.
See the [Node CLI documentation](https://nodejs.org/api/cli.html#--max-old-space-sizesize-in-mib).

Start with a 900-second no-fault smoke, requiring unchanged pod/container,
restart count, deployment template and limit, all required pods ready, no
node memory pressure, no chaos, active currencyservice traces, and memory
below 90% of its limit at every sample. This is shorter than the old roughly
45-minute OOM cycle, so require a separate >=3600-second soak before marking
the collection-stability gate passed. Neither soak proves an absence of leaks
or establishes an API SLA. Preserve old incidents; recollect calibration and
evaluation data after this deployment change.

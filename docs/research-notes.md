# CoRCA research notes

CoRCA targets multi-root-cause RCA for microservice failures under partial observability. The useful scope is narrow:

- Use RCAEval as the benchmark/data harness and compare against its baselines.
- Use BARO and CIRCA as base rankers, not as competitors we reimplement.
- Add value by iteratively explaining away propagation symptoms so more than one independent root cause can surface.
- Report confidence as an interpretable score gap, not a probabilistic guarantee.
- Treat synthetic results as sanity checks only; real claims need RCAEval or Chaos Mesh cases.

## Working assumptions

- A service dependency graph tells us where symptoms can propagate; it does not prove causation.
- A root cause explains itself and transitive callers. For explained callers, resource metrics can remain candidates because downstream failures rarely cause caller CPU/memory/disk spikes.
- Missing telemetry is modeled first as missing metric streams. More detailed log/trace loss can wait until RE2/RE3 data is in scope.

## Evaluation checklist

- Baselines: BARO via RCAEval; adapted CIRCA PC/RHT using actual observed windows.
- Single-root data: calibrate `tau` and measure false extra roots.
- Multi-root data: measure set precision, recall, F1, exact match, and average confidence.
- Partial observability: repeat after dropping metric streams at fixed rates.
- Runtime: report per-case seconds and timeout rate.

The local CIRCA pilot exposed singular input correlations and incorrect fixed
window assumptions in the upstream adapter. See `circa-fix.md` for the evidence,
adapter changes, experiment separation, and limits on interpreting zero scores.

# Joint telemetry investigations

Open **Diagnoses → Import telemetry**. A monitoring connection is not required
for imported investigations. Name the environment, choose the metric-window
JSON and optional OTLP JSON/JSONL trace and log exports, and set the sensitivity.
Use a stable environment name for comparable incident reviews.

The metric file uses the existing Incident fields: columns (including time),
rows, inject_time (Unix seconds), and optional caller-to-callee edges. Supply at
least ten observations on each side of the incident, at most 2,500 rows, and
at most 199 combined metric columns. Without traces, edges are required; an
empty list explicitly means independent services. Uploads total at most 4 MiB.
Each log/trace export is limited to 20,000 events.

Logs use resourceLogs/scopeLogs/logRecords and traces use
resourceSpans/scopeSpans/spans from the [OTLP JSON format](https://opentelemetry.io/docs/specs/otlp/).
Resources need service.name. Trace and span IDs are hexadecimal, timestamps
are Unix nanoseconds, and logs require severityNumber (1–24).

The API accepts POST /v1/diagnoses/import with:
- environment: the environment name
- incident: the metric-window object, including an explicit tau
- traces: one OTLP export object or an array of exports (optional)
- logs: one OTLP export object or an array of exports (optional)

The console converts JSONL exports into arrays. Derived numeric evidence and
the input configuration persist with the job. Raw log messages and raw spans
are not retained. Downloads include the diagnosis, chart evidence, outcome
review, and the current uncertainty assessment. Re-running an imported
investigation uses its saved inputs and configuration.

## What is combined

Trace duration percentiles, trace error fractions, and log-error counts are
aligned to the metric clock before the selected localizer runs. This is feature
fusion: the ranking and residual inspection see the combined numeric matrix.
It is not an LLM reading log messages. Missing event buckets stay missing;
the imported BARO adapter retains flat normal baselines so step changes are
not dropped by the benchmark preprocessing. The historical benchmark path
retains its existing preprocessing. Combined
streams still pass the existing coverage and minimum-sample checks. The results
list which modalities survived those checks.

Trace parent-child pairs identify observed cross-service calls. Normal-window
and incident-window edges are combined with any explicitly supplied edges for
that diagnosis. Newly observed incident edges and normal edges not seen again
are reported separately. Missing spans do not prove an edge disappeared.
The application's saved monitoring configuration is not overwritten.

Combined evidence has a different score distribution from metric-only input.
Imported sensitivity is explicitly manual; do not reuse a metric-only calibrated
threshold without validating it against the combined telemetry.

## Repair estimates

The repair card fits a linear structural model on normal observations,
reversing call direction to model dependency-to-caller propagation. It holds
inferred incident disturbances fixed, sets one selected service's response to
its normal mean, and propagates that intervention. Other independent
disturbances therefore remain in the estimate.

These are model-based counterfactual estimates, conditional on an acyclic,
correct graph, no unmeasured common causes, and stable linear mechanisms.
They are not experimentally established causal effects. The model abstains
for cycles, missing/constant evidence, too few samples, or normal-window
validation error worse than a constant baseline. It requires at least 30 normal
and ten incident samples and more history for large parent sets. All effects
and validation errors are included in the evidence download.

The reconstruction-and-intervention procedure follows the principles described
in [DoWhy's counterfactual documentation](https://www.pywhy.org/dowhy/v0.14/user_guide/causal_tasks/what_if/counterfactuals.html);
CoRCA uses its existing NumPy/scikit-learn dependencies.

## Candidate reliability

Review completed incident outcomes using independent evidence. Candidate
reliability is computed from earlier, nonoverlapping, confirmed incidents with
the same environment, input schema, method, threshold, algorithm policies,
sampling interval, topology policy and window lengths. Synthetic outcomes and
the target incident are excluded.

At least 30 eligible incidents are required. A chronological split trains a
sigmoid calibration model and assesses held-out Brier score. Both correct and
incorrect candidates are required; each incident receives equal weight.
Probabilities are withheld if the held-out score is worse than the original
score or the target score is outside the training range.

This follows the separation of calibration and evaluation data described in
[scikit-learn's calibration guide](https://scikit-learn.org/stable/modules/calibration.html).
The display reports sample counts and validation error. It estimates correctness
of selected candidates, not whether every true cause was found. Environment
drift can invalidate it. Sparse evidence produces an explicit unavailable state,
not a made-up probability.

## Verification scope

Regression tests cover alignment, missing data, parent-child topology, invalid
exports, raw-content privacy, immutable re-runs, and outcome separation.
A real BARO adapter test confirms trace evidence can trigger diagnosis when
resource metrics are quiet. Controlled structural examples check that a repair
estimate retains a separate caller disturbance. These tests establish software
behavior; they do not replace independent concurrent-fault field experiments.


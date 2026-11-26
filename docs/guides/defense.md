# Committee review

The review workflow runs the actual diagnosis engine, saves per-case results,
publishes comparisons into an isolated CoRCA workspace, and packages the source
and inputs. It does not edit the submission paper or historical experiment runs.

Install Python 3.12 dependencies as described in [CONTRIBUTING.md](../../CONTRIBUTING.md).
Every build needs a **new output directory**. No live monitoring connection or
cloud account is required for the recorded-data workflow.

## Quick end-to-end demonstration

```sh
uv run --no-sync python -m scripts.defense demo --out results/defense-demo
uv run --no-sync python -m scripts.defense verify --out results/defense-demo
uv run --no-sync python -m scripts.defense serve --out results/defense-demo
```

Open <http://localhost:8048/app?view=quality>. The demonstration imports a healthy
window and a concurrent-fault window containing metrics, logs, and traces. It
executes the real API, queue, worker, outcome review and evaluation publication.
Both cases are explicitly **synthetic**. They establish software behavior only.

## Full research evaluation

Supply the public RCAEval Online Boutique dataset and complete local collections.
The public dataset uses `service_fault/repeat/data.csv` plus `inject_time.txt`.
Local folders use the [lab collection format](../../lab/README.md), including
`collection.json`, `metrics.csv`, `topology.json` and independent ground truth.

```sh
uv run --no-sync python -m scripts.defense evaluate \
  --public-data RE1-OB \
  --controls data/local-circa-window-probe-v2 \
  --heldout data/local-policy-v1-heldout-smoke-v2 \
  --out results/defense-review --workers 4
uv run --no-sync python -m scripts.defense verify --out results/defense-review
uv run --no-sync python -m scripts.defense serve --out results/defense-review
```

The runner copies the evaluation inputs into the output folder, calibrates local
thresholds on separate controls, runs BARO and the CIRCA adapter on all public
and local cases, generates comparisons, and publishes per-case reports. Public
thresholds use held-out repeats. Local calibration/test file overlap is rejected.
The supplied local paths currently contain one control and one concurrent-fault
incident; a completed run does not turn these into a repeated multi-fault study.

| Artifact | What the committee can inspect |
| --- | --- |
| `pipeline.json`, stage logs | Commands, source hashes, completion/failure status |
| `public/summary.csv` | Per-case public baseline and CoRCA results; failures retained |
| `controls/calibration.json` | Thresholds and calibration input fingerprints |
| `heldout/summary.csv` | Concurrent cases, resource-preservation/topology ablations, missing telemetry |
| `tables/rq_tables.csv`, `paired_diffs.csv` | Main results and incident-level bootstrap comparisons |
| `tables/matched_escalation*.csv` | Baseline given the same output count as CoRCA; isolates reranking |
| `tables/sensitivity.csv` | Frozen-threshold multipliers and round limits; not used for tuning |
| `reports/*.json` | Validated reports importable through Evaluations → Pipeline tools |
| `exports/*.json` | Recorded example diagnoses, reviews, evidence, availability of probability estimates |
| `checksums.json`, `source-snapshot.zip` | Artifact integrity and exact source used |
| `committee-review.zip` | Portable source, selected data, results, and review workspace |

The comparison at matched output size is a diagnostic control: the baseline is
given CoRCA's candidate count. It is not a separately deployed selection policy.
Dropout seeds reuse incidents and are never counted as independent repeats in
the main tables. Complete exact-match metrics require a complete diagnosis;
raw set-exact scores in research CSVs can also include partial diagnoses.

## Review in the application

1. Open **Evaluations** and select the CoRCA primary run.
   Cause-set F1, complete matches, precision and recall are shown first; expand
   ranking/runtime measurements for top-k diagnostics.
2. Compare it with **Top-1**, **Top-2**, or **Matched output size** on the same data.
3. Inspect incident rows, failures, fault types, timing, and limitations.
4. Open **Diagnoses** for a recorded example, inspect its signals and dependency
   map, then download its evidence or rerun the saved input.
5. Use the CSVs for ablation and sensitivity tables and the logs to reproduce commands.

The new telemetry import path and the historical research evaluator have
different preprocessing policies. Recorded API examples are labelled separately;
they are not substituted for the benchmark's recorded predictions. Joint imports
currently use explicit sensitivity, and log/trace inputs are uploaded exports.
Repair estimates are conditional model estimates, and calibrated candidate
probabilities remain unavailable without sufficient independent confirmed cases.

Keep the original ZIP unchanged. Serving the workspace or recording reviews can
change its database hash; verify the original package before opening it. Stop the
review server with Ctrl+C. It uses port 8048 and its own database, so it does not
replace an existing app on port 8000.

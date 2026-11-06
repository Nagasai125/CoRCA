# Evaluation runbook (after data collection)

Freeze the source revision and evaluation criteria before comparing runs. Use a new
`--out` folder for every run. Never put test incidents in the calibration folder.

```bash
PY=.venv/bin/python

# 0. Gate: currencyservice memory soak must pass before collecting anything.
$PY -m lab.soak --out results/soak-60m --seconds 3600

# 1. Calibration controls: single faults only, their own folder.
$PY -m lab.run_cases --out data/controls-v1 --repeats 5 \
    --only currencyservice_cpu adservice_cpu checkoutservice_cpu
$PY -m lab.evaluate --data data/controls-v1 --out results/controls-v1 --rates 0 --seeds 0
#    -> results/controls-v1/calibration.json is the frozen tau. Do not refit later.

# 2. Held-out test set: new single-fault repeats (RQ2 "no harm") + 2/3-fault incidents.
$PY -m lab.run_cases --out data/heldout-v1 --repeats 5
$PY -m lab.evaluate --data data/heldout-v1 --out results/heldout-v1 \
    --calibration results/controls-v1/calibration.json
#    Multi-fault incidents also run the ablations: telemetry dropout (rates x seeds),
#    keep_resource=False, and the static topology graph.

# 3. Public single-fault benchmark (RQ2 on RE1-OB; tau held out by repeat).
$PY -m scripts.run_eval --data RE1-OB --methods baro circa --out results/re1ob-final
$PY -m scripts.summarize results/re1ob-final/runs.jsonl

# 4. Final tables for the report.
$PY -m scripts.report results/re1ob-final results/heldout-v1 --out results/final
$PY -m scripts.plots results/final   # fig_f1, fig_dropout, fig_sensitivity (.png + .pdf)

# 5. Deploy the service with a tau fitted on the metrics it will see (Prometheus cpu/mem).
#    Run right after step 1: lab Prometheus loses history when its pod restarts.
kubectl --kubeconfig lab/.state/kubeconfig -n corca port-forward service/prometheus 9090:9090 &
$PY -m lab.export_cases --from data/controls-v1 --out data/controls-v1-live --prometheus-url http://127.0.0.1:9090
$PY -m lab.evaluate --data data/controls-v1-live --out results/controls-v1-live --rates 0 --seeds 0
docker build -f backend/Dockerfile -t corca:0.2.0 .
$PY -m lab.deploy --image corca:0.2.0 --calibration results/controls-v1-live/calibration.json
```

The report's numbers (steps 1-4) use the lab's full telemetry; the deployed service
(step 5) uses only what its source config queries. Do not mix their taus.

## Reading `results/final`

- `rq_tables.csv`: one row per source/method/fault count/ablation, P/R/F1/exact as
  `mean [95% CI]`, bootstrapped over incidents (dropout seeds of one incident count once).
  - RQ1 = `top1set_*` (the base method as-is) and `top2set/top3set` on `n_true` 2-3.
  - RQ2 = `corca_*` vs those, on `n_true` 2-3; "no harm" = `corca_*` vs `top1set_*` at `n_true` 1.
  - `oracle_k_*` is told the true root count: an upper bound, not a competitor.
- `paired_diffs.csv`: CoRCA F1 minus each baseline, paired per incident. A CI that
  excludes 0 is the claim; one that includes 0 is "no detectable difference".
- `sensitivity.csv`: CoRCA at tau x {0.5, 0.75, 1, 1.5, 2} and max_rounds 1-3. Ablation only;
  never pick tau from it.
- Ablation rows: `drop > 0` (missing telemetry), `keep_resource=False`, `graph_mode=static`.

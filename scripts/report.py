"""Final RQ1/RQ2 tables from finished result folders.

Each folder must hold summary.csv + calibration.json + runs.jsonl (scripts.summarize)
or diagnoses.jsonl (lab.evaluate). Writes to --out:
  rq_tables.csv     P/R/F1/exact with 95% bootstrap CIs over incidents, per approach
  paired_diffs.csv  CoRCA F1 minus each top-k baseline, paired by incident, with CI
  sensitivity.csv   CoRCA F1/exact at tau x {0.5..2} and max_rounds 1..3 (ablation)

    python -m scripts.report results/re1ob_current results/lab-heldout --out results/final
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from corca.metrics import TOP_K_BASELINES, exact_match, set_prf
from scripts.experiment import read_runs
from scripts.summarize import predicted_set

APPROACHES = [*[f"top{k}set" for k in TOP_K_BASELINES], "oracle_k", "corca"]
VARIANT = {"drop": 0.0, "keep_resource": True, "graph_mode": "observed"}  # RE1-OB has no ablation columns
KEYS = ["source", "method", "n_true", *VARIANT]
TAU_SCALES = (0.5, 0.75, 1.0, 1.5, 2.0)
N_BOOT = 2000


def ci(values, rng):
    """Mean and percentile-bootstrap 95% CI, resampling incidents."""
    x = np.asarray(values, float)
    lo, hi = np.percentile(rng.choice(x, (N_BOOT, len(x))).mean(axis=1), [2.5, 97.5])
    return x.mean(), lo, hi


def load(folder):
    folder = Path(folder)
    frame = pd.read_csv(folder / "summary.csv")
    for col, default in VARIANT.items():
        if col not in frame:
            frame[col] = default
    if "calibrated" in frame:
        skipped = int((~frame["calibrated"]).sum())
        if skipped:
            print(f"WARNING {folder.name}: {skipped} uncalibrated rows excluded (scores would be 0 by construction)")
        frame = frame[frame["calibrated"]]
    frame["source"] = folder.name
    missing = [f"{a}_F1" for a in APPROACHES if f"{a}_F1" not in frame]
    if missing:
        raise ValueError(f"{folder}/summary.csv lacks {missing}; re-run summarize/evaluate on it first")
    runs = folder / "runs.jsonl"
    records = (read_runs(runs) if runs.exists() else
               [json.loads(line) for line in (folder / "diagnoses.jsonl").read_text().splitlines() if line.strip()])
    return frame, records, json.loads((folder / "calibration.json").read_text())["methods"]


def tables(frame, rng):
    rows, diffs = [], []
    for key, group in frame.groupby(KEYS):
        per_case = group.groupby("case").mean(numeric_only=True)  # dropout seeds reuse one incident
        ident = dict(zip(KEYS, key), n_incidents=len(per_case))
        row = dict(ident)
        for a in APPROACHES:
            for m in ("P", "R", "F1", "exact"):
                mean, lo, hi = ci(per_case[f"{a}_{m}"], rng)
                row[f"{a}_{m}"] = f"{mean:.3f} [{lo:.3f}, {hi:.3f}]"
        rows.append(row)
        for a in APPROACHES[:-1]:
            mean, lo, hi = ci(per_case["corca_F1"] - per_case[f"{a}_F1"], rng)
            diffs.append({**ident, "vs": a, "corca_minus_vs_F1": mean, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows), pd.DataFrame(diffs)


def sensitivity(source, records, calibration):
    rows = []
    for rec in records:
        if rec["method"] not in calibration or any(rec.get(c, d) != d for c, d in VARIANT.items()):
            continue
        cal = calibration[rec["method"]]
        tau = cal.get("fold_taus", {}).get(rec.get("case_id"), cal["tau"])  # RE1-OB: held-out-repeat tau
        for scale in TAU_SCALES:
            for rounds in (1, 2, 3):
                pred = predicted_set({**rec, "rounds": rec.get("rounds", [])[:rounds]}, tau * scale)
                rows.append({"source": source, "method": rec["method"], "n_true": len(rec["truth"]),
                             "tau_scale": scale, "max_rounds": rounds, "corca_F1": set_prf(pred, rec["truth"])[2],
                             "corca_exact": exact_match(pred, rec["truth"]), "n_pred": len(pred)})
    return pd.DataFrame(rows)


def matched_escalation(source, records, calibration):
    """Hold output size fixed to separate reranking from returning more candidates."""
    rows = []
    for rec in records:
        if rec["method"] not in calibration:
            continue
        cal = calibration[rec["method"]]
        tau = cal.get("fold_taus", {}).get(rec.get("case_id"), cal["tau"])
        predicted = predicted_set(rec, tau)
        baseline = rec.get("base_rank", [])[:len(predicted)]
        rows.append({"source":source,"case":rec["case"],"method":rec["method"],
                     "n_true":len(rec["truth"]),**{k:rec.get(k,v) for k,v in VARIANT.items()},
                     "n_pred":len(predicted),"baseline_n_pred":len(baseline),
                     "corca_F1":set_prf(predicted,rec["truth"])[2],
                     "matched_F1":set_prf(baseline,rec["truth"])[2],
                     "corca_exact":exact_match(predicted,rec["truth"]),
                     "matched_exact":exact_match(baseline,rec["truth"]),
                     "size_matched":len(predicted)==len(baseline)})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    loaded = [load(f) for f in args.folders]
    frame = pd.concat([f for f, _, _ in loaded], ignore_index=True)
    sens = pd.concat([sensitivity(Path(p).name, r, c) for p, (_, r, c) in zip(args.folders, loaded)],
                     ignore_index=True)
    # Self-check: the default operating point must reproduce the pipelines' own CoRCA F1.
    base = frame[(frame["drop"] == 0) & frame["keep_resource"] & (frame["graph_mode"] == "observed")]
    here = sens[(sens.tau_scale == 1.0) & (sens.max_rounds == 3)].groupby(["source", "method", "n_true"]).corca_F1.mean()
    there = base.groupby(["source", "method", "n_true"]).corca_F1.mean()
    shared = here.index.intersection(there.index)
    assert np.allclose(here[shared], there[shared]), "sensitivity sweep disagrees with summary.csv"

    rq, diffs = tables(frame, np.random.default_rng(0))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rq.to_csv(out / "rq_tables.csv", index=False)
    diffs.to_csv(out / "paired_diffs.csv", index=False)
    sens_table = sens.groupby(["source", "method", "n_true", "tau_scale", "max_rounds"]).mean().round(3)
    sens_table.to_csv(out / "sensitivity.csv")
    matched = pd.concat([matched_escalation(Path(p).name,r,c) for p,(_,r,c) in zip(args.folders,loaded)],ignore_index=True)
    matched.to_csv(out / "matched_escalation_cases.csv",index=False)
    matched.groupby(KEYS).agg(incidents=('case','nunique'),attempts=('case','size'),
        size_match_rate=('size_matched','mean'),corca_F1=('corca_F1','mean'),
        matched_F1=('matched_F1','mean'),corca_exact=('corca_exact','mean'),
        matched_exact=('matched_exact','mean')).to_csv(out / "matched_escalation.csv")
    main_rows = rq[(rq["drop"] == 0) & rq["keep_resource"] & (rq["graph_mode"] == "observed")]
    with pd.option_context("display.width", 250, "display.max_columns", None):
        print(main_rows[["source", "method", "n_true", "n_incidents", *[f"{a}_F1" for a in APPROACHES]]].to_string(index=False))
        print(diffs[(diffs["drop"] == 0) & diffs["keep_resource"] & (diffs["graph_mode"] == "observed")]
              [["source", "method", "n_true", "vs", "corca_minus_vs_F1", "ci_low", "ci_high"]].round(3).to_string(index=False))
    print(f"Wrote {out}/rq_tables.csv, paired_diffs.csv, sensitivity.csv")


if __name__ == "__main__":
    main()

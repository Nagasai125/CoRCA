import pandas as pd

from scripts import plots


def test_figures_render_from_report_tables(tmp_path):
    ci = "0.700 [0.600, 0.800]"
    base = {"source": "lab", "method": "baro", "keep_resource": True, "graph_mode": "observed", "n_incidents": 5,
            **{f"{a}_F1": ci for a in ("corca", "top1set", "top2set", "top3set", "oracle_k")}}
    pd.DataFrame([{**base, "n_true": n, "drop": d} for n in (1, 2) for d in (0.0, 0.25)]).to_csv(
        tmp_path / "rq_tables.csv", index=False)
    pd.DataFrame([{"source": "lab", "method": "baro", "n_true": 2, "tau_scale": t, "max_rounds": r,
                   "corca_F1": .5, "corca_exact": 0, "n_pred": 2} for t in (.5, 1, 2) for r in (1, 2, 3)]).to_csv(
        tmp_path / "sensitivity.csv", index=False)
    rq = pd.read_csv(tmp_path / "rq_tables.csv")
    plots.fig_f1(rq, tmp_path)
    plots.fig_dropout(rq, tmp_path)
    plots.fig_sensitivity(pd.read_csv(tmp_path / "sensitivity.csv"), tmp_path)
    assert {p.name for p in tmp_path.glob("fig_*.pdf")} == {"fig_f1.pdf", "fig_dropout.pdf", "fig_sensitivity.pdf"}

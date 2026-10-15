import json

import numpy as np
import pandas as pd
import pytest
from RCAEval.graph_heads.rht import ANMRegressor

from corca.circa import rank_circa
from scripts.investigate_circa import FloorRegressor, TraceScorer, main, metric_hash


def test_local_overlap_fingerprint_uses_actual_loaded_metrics(tmp_path):
    import hashlib
    (tmp_path / "data.csv").write_text("unused-public-data")
    metrics = b"actual-local-data"
    (tmp_path / "metrics.csv").write_bytes(metrics)
    (tmp_path / "collection.json").write_text('{"status": "complete"}')
    assert metric_hash(tmp_path) == hashlib.sha256(metrics).hexdigest()


def test_floor_limits_tiny_baseline_noise_without_hiding_large_memory_shift():
    normal = 100 + np.linspace(-.001, .001, 30)
    fault = np.full(30, 100.5)
    empty = np.empty((0, 0))
    raw = ANMRegressor().score(empty, empty, normal, fault)
    candidate = FloorRegressor("mem")
    guarded = candidate.score(empty, empty, normal, fault)
    assert raw.min() > 100 and guarded.max() == pytest.approx(.5)
    assert candidate.score(empty, empty, normal, fault + 25).min() > 25
    np.testing.assert_allclose(guarded, candidate.score(empty, empty, normal * 1024**2, fault * 1024**2))


def test_zero_baseline_error_fraction_has_finite_scores():
    score = FloorRegressor("error").score(np.empty((0, 0)), np.empty((0, 0)), np.zeros(30), np.full(30, .2))
    np.testing.assert_allclose(score, 20)


def test_candidate_does_not_swallow_regression_shape_errors():
    with pytest.raises(ValueError):
        FloorRegressor("cpu").score(np.ones((30, 1)), np.ones((30, 2)), np.arange(30), np.arange(30))


def test_tracing_preserves_baseline_and_candidate_preserves_input(monkeypatch):
    from RCAEval.graph_construction import pc
    monkeypatch.setattr(pc, "pc_default", lambda data: np.zeros((len(data.columns), len(data.columns)), dtype=int))
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"time": np.arange(60) * 5, "root_cpu": rng.normal(10, 1, 60),
                       "quiet_mem": np.tile(100 + np.linspace(-.001, .001, 30), 2)})
    df.loc[30:, "root_cpu"] += 30
    df.loc[30:, "quiet_mem"] += .5
    original = df.copy(deep=True)
    default = rank_circa(df, 150, "re2-ob")
    trace = TraceScorer()
    assert rank_circa(df, 150, "re2-ob", scorer=trace) == default
    assert default[0] == "quiet_mem"
    assert rank_circa(df, 150, "re2-ob", scorer=TraceScorer(floor=True))[0] == "root_cpu"
    assert len(trace.details) == 2 and trace.details[0]["parents"] == []
    pd.testing.assert_frame_equal(df, original)


def test_development_cli_writes_bounded_traces_and_rejects_overlap(monkeypatch, tmp_path):
    pytest.importorskip("fastapi", reason="local collection diagnostics require the runtime environment")
    folders = []
    for repeat in (1, 2):
        folder = tmp_path / "cartservice_cpu" / str(repeat)
        folder.mkdir(parents=True)
        rng = np.random.default_rng(repeat)
        values = rng.normal(10, 1, 60)
        values[30:] += 40
        pd.DataFrame({"time": np.arange(60) * 5, "cartservice_cpu": values}).to_csv(folder / "metrics.csv", index=False)
        for name, value in {"collection.json": {"status": "complete"},
                            "ground_truth.json": {"root_causes": ["cartservice"]},
                            "topology.json": {"edges": []}}.items():
            (folder / name).write_text(json.dumps(value))
        (folder / "inject_time.txt").write_text("150")
        folders.append(folder)
    output = tmp_path / "comparison"
    args = ["investigate", "--control", str(folders[0]), "--case", str(folders[1]), "--out", str(output)]
    monkeypatch.setattr("sys.argv", args)
    main()
    report = json.loads((output / "report.json").read_text())
    records = [json.loads(line) for line in (output / "diagnoses.jsonl").read_text().splitlines()]
    assert report["scope"] == "development_only_not_held_out"
    assert len(records) == 4 and all(r["status"] == "ok" for r in records)
    assert all(r["traces"][0]["metrics"][0]["metric"] == "cartservice_cpu" for r in records)
    assert all(r["F1"] == 1 for r in report["comparisons"])
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit):
        main()
    args = ["investigate", "--control", str(folders[0]), "--case", str(folders[0]), "--out", str(tmp_path / "bad")]
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(SystemExit):
        main()

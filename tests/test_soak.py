import json

import pytest

from lab import soak


def sample():
    return {"time": 1, "pod": "currency-1", "uid": "uid", "container_id": "container",
            "restarts": 0, "memory_bytes": 100, "limit_bytes": 256, "template_sha256": "template"}


@pytest.mark.parametrize("change", [{"uid": "replacement"}, {"container_id": "restart"},
                                  {"restarts": 1}, {"template_sha256": "changed"},
                                  {"limit_bytes": 512}, {"memory_bytes": 231}])
def test_soak_rejects_restarts_drift_and_memory_pressure(change):
    first = sample()
    with pytest.raises(ValueError):
        soak.check_sample(dict(first, **change), first, .9)


def test_soak_records_bounded_success_and_preserves_existing_output(monkeypatch, tmp_path):
    monkeypatch.setattr(soak, "snapshot", sample)
    monkeypatch.setattr(soak, "trace_snapshot", lambda *args: [])
    monkeypatch.setattr(soak, "spans_from_records", lambda records: [("currencyservice", {})])
    report = soak.run(tmp_path / "out", .001, .001, .9)
    assert report["status"] == "passed" and not report["long_soak_gate_met"]
    assert report["max_memory_bytes"] == 100 and report["currencyservice_spans"] == 1
    assert json.loads((tmp_path / "out/report.json").read_text()) == report
    with pytest.raises(FileExistsError):
        soak.run(tmp_path / "out", .001, .001, .9)


def test_soak_persists_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(soak, "snapshot", lambda: (_ for _ in ()).throw(ValueError("no healthy lab")))
    with pytest.raises(ValueError, match="no healthy lab"):
        soak.run(tmp_path / "out", .001, .001, .9)
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert report["status"] == "failed" and report["samples"] == 0

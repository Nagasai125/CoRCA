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
    monkeypatch.setattr(soak, "spans_from_records", lambda records: [("currencyservice", {"kind": 2, "name": "Convert"})])
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


@pytest.mark.parametrize("wall,mono,last,interval,valid", [(1010, 10, 0, 30, True),
                                                         (1200, 10, 0, 30, False),
                                                         (1050, 50, 0, 10, False),
                                                         (900, 10, 0, 30, False)])
def test_soak_clock_checks_elapsed_time_and_wall_clock_drift(monkeypatch, wall, mono, last, interval, valid):
    monkeypatch.setattr(soak.time, "time", lambda: wall)
    monkeypatch.setattr(soak.time, "monotonic", lambda: mono)
    if valid:
        assert soak.check_clock(0, 1000, last, interval) == mono
    else:
        with pytest.raises(ValueError, match="interrupted"):
            soak.check_clock(0, 1000, last, interval)


@pytest.mark.parametrize("span", [{"kind": 2, "name": "grpc.health.v1.Health/Check"},
                                 {"kind": 2, "name": "Convert", "status": {"code": 2}},
                                 {"kind": 3, "name": "Convert"}])
def test_health_failed_or_client_spans_cannot_pass_workload_gate(monkeypatch, tmp_path, span):
    monkeypatch.setattr(soak, "snapshot", sample)
    monkeypatch.setattr(soak, "trace_snapshot", lambda *args: [])
    monkeypatch.setattr(soak, "spans_from_records", lambda records: [("currencyservice", span)])
    with pytest.raises(ValueError, match="no successful non-health"):
        soak.run(tmp_path / "out", .001, .001, .9)
    assert json.loads((tmp_path / "out/report.json").read_text())["status"] == "failed"

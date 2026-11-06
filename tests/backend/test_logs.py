import json

import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.logs import MAX_LOG_BYTES, analyze_logs, timestamp


def test_json_lines_group_errors_with_evidence_and_incident_comparison():
    lines = [
        {"timestamp": "2026-11-04T14:30:00Z", "service": "checkout", "level": "INFO", "message": "request complete"},
        {"timestamp": "2026-11-04T14:32:00Z", "service": {"name": "checkout"}, "level": "ERROR", "message": "cart timeout for request 123"},
        {"timestamp": "2026-11-04T09:33:00-05:00", "resource": {"attributes": {"service.name": "cart"}}, "level": "ERROR", "message": "cart timeout for request 456"},
    ]
    result = analyze_logs("\n".join(map(json.dumps, lines)).encode(), "incident.jsonl", incident_start=timestamp("2026-11-04T14:31:00Z")[0])
    assert result["entries"] == 3 and result["errors"] == 2
    assert result["patterns"][0]["count"] == 2
    assert result["patterns"][0]["services"] == ["cart", "checkout"]
    assert result["patterns"][0]["samples"][0]["line"] == 2
    assert "request 123" in result["patterns"][0]["samples"][0]["sample"]
    assert result["comparison"]["before"] == {"entries": 1, "errors": 0}
    assert result["comparison"]["after"] == {"entries": 2, "errors": 2}
    assert sum(bucket["entries"] for bucket in result["timeline"]) == 3


def test_plain_text_traceback_and_new_error_are_separate_events():
    content = b'''2026-11-04 14:32:00 ERROR service=checkout request failed
Traceback (most recent call last):
  File "app.py", line 12, in handle
ValueError: cart is unavailable
ERROR IOError: another failure
'''
    result = analyze_logs(content, "checkout.log")
    assert result["entries"] == 2 and result["errors"] == 2
    assert any("UTC was assumed" in note for note in result["notes"])
    sample = next(p for p in result["patterns"] if "cart is unavailable" in p["pattern"])["samples"][0]
    assert sample["line"] == 1 and sample["line_end"] == 4
    assert "Traceback" in sample["sample"]


def test_missing_and_invalid_timestamps_are_not_invented():
    result = analyze_logs(b'ERROR service=cart failed\nnot structured\n{bad json\n', "raw.txt", service="checkout", incident_start=100)
    assert result["entries"] == 3 and result["timestamped"] == 0 and not result["timeline"]
    assert any("not valid JSON" in note for note in result["notes"])
    assert result["comparison"]["after"]["entries"] == 0
    result = analyze_logs(b'{"message":"previous incident at 2026-11-04T14:32:00Z"}', "raw.jsonl")
    assert result["timestamped"] == 0
    assert timestamp(float("inf"))[0] is None
    assert timestamp("2026-99-99T00:00:00Z")[0] is None
    assert timestamp(1793802720000)[0] == 1793802720


@pytest.mark.parametrize("content", [b"", b"\x00binary", b"\xff", b"x" * (MAX_LOG_BYTES + 1), b"\n" * 50001, b"x" * 16385])
def test_bad_or_excessive_files_fail_explicitly(content):
    with pytest.raises(ValueError):
        analyze_logs(content, "bad.log")


def test_log_endpoint_requires_access_and_works_without_telemetry(tmp_path):
    token = "test-log-token-long-enough"
    with TestClient(create_app(token, db_path=tmp_path / "workspace.db")) as client:
        path = "/v1/logs/analyze?filename=incident.log"
        assert client.post(path, content="ERROR fail").status_code == 401
        headers = {"X-API-Key": token, "content-type": "text/plain"}
        response = client.post(path, content="ERROR service=checkout request 123 failed", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "log_observations"
        assert response.json()["errors"] == 1
        assert client.get("/v1/diagnoses", headers=headers).json() == []  # no persistence or fabricated metric diagnoses
        assert client.post(path, content=b"x" * (MAX_LOG_BYTES + 1), headers=headers).status_code == 413
        assert client.post(path.replace("incident.log", "archive.zip"), content="ERROR fail", headers=headers).status_code == 422
        assert client.post(path + "&incident_start=NaN", content="ERROR fail", headers=headers).status_code == 422
        assert client.post(path, content="ERROR fail", headers={**headers, "Origin": "https://other.example"}).status_code == 403
        assert not list(tmp_path.glob("*.log"))

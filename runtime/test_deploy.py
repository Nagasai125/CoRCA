import json

import pytest

from runtime import deploy


def test_deploy_loads_and_applies_the_requested_image(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy", "--image", "corca-api:policy-v1"])
    loaded, applied = [], []
    monkeypatch.setattr(deploy.subprocess, "run", lambda args, **kwargs: loaded.append(args))
    def kube(*args, **kwargs):
        if args[0] == "create":
            return "\n".join(json.dumps(obj) for obj in [
                {"kind": "Deployment", "spec": {"template": {"spec": {
                    "containers": [{"name": "api", "image": "old"}]}}}},
                {"kind": "Service", "metadata": {"name": "corca-api"}},
            ])
        if "payload" in kwargs:
            applied.append(kwargs["payload"])
        return "ready"
    monkeypatch.setattr(deploy, "kube", kube)
    deploy.main()
    assert loaded[0][1:] == ["load", "docker-image", "corca-api:policy-v1", "--name", "corca-lab"]
    config = next(p for p in applied if p["kind"] == "ConfigMap")["data"]
    assert json.loads(config["source.json"])["prometheus_url"].startswith("http")
    objects = applied[-1]["items"]
    assert objects[0]["spec"]["template"]["spec"]["containers"][0]["image"] == "corca-api:policy-v1"
    assert objects[1]["metadata"]["name"] == "corca-api"


def test_incompatible_calibration_cannot_load_an_image(monkeypatch, tmp_path):
    calibration = tmp_path / "old.json"
    calibration.write_text('{"methods": {"circa": {"tau": 6}}}')
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy", "--calibration", str(calibration)])
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: pytest.fail("must not load an image"))
    monkeypatch.setattr(deploy, "kube", lambda *a, **k: pytest.fail("must not modify the cluster"))
    with pytest.raises(ValueError, match="recalibrate"):
        deploy.main()

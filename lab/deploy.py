"""Deploy the API image only into the dedicated local lab."""
import argparse
import base64
import json
import os
import secrets
import subprocess
from pathlib import Path

from lab.setup import ROOT, TOOLS
from lab.telemetry import kube
from backend.api import calibration_thresholds
from backend.telemetry import load_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration")
    parser.add_argument("--source", default=str(Path(__file__).with_name("source.online-boutique.json")),
                        help="Prometheus source config for /v1/diagnoses and /v1/alerts")
    parser.add_argument("--image", default="corca:0.2.0", help="exact locally built image tag to load and deploy")
    args = parser.parse_args()
    state = ROOT / "backend" / ".state"
    state.mkdir(parents=True, exist_ok=True)
    token_file = state / "api-token"
    if not token_file.exists():
        with token_file.open("x") as stream:
            os.chmod(token_file, 0o600)
            stream.write(secrets.token_urlsafe(32))
    token = token_file.read_text().strip()
    if len(token) < 16:
        raise ValueError("invalid API token")
    calibration = json.loads(Path(args.calibration).read_text()) if args.calibration else {"methods": {}}
    calibration_thresholds(calibration)
    source = load_source(args.source)  # validate before touching the cluster
    subprocess.run([str(TOOLS / "kind"), "load", "docker-image", args.image, "--name", "corca-lab"], check=True, timeout=180)
    kube("apply", "-f", "-", payload={"apiVersion": "v1", "kind": "Secret",
                                       "metadata": {"name": "corca-api", "namespace": "corca"},
                                       "data": {"api-token": base64.b64encode(token.encode()).decode()}})
    kube("apply", "-f", "-", payload={"apiVersion": "v1", "kind": "ConfigMap",
                                       "metadata": {"name": "corca-calibration", "namespace": "corca"},
                                       "data": {"calibration.json": json.dumps(calibration),
                                                "source.json": json.dumps(source)}})
    rendered = kube("create", "--dry-run=client", "-f", ROOT / "lab" / "deployment.yaml", "-o", "json")
    # kubectl emits consecutive JSON objects for a multi-document manifest.
    decoder, objects = json.JSONDecoder(), []
    while rendered.strip():
        rendered = rendered.lstrip()
        obj, end = decoder.raw_decode(rendered)
        objects.append(obj)
        rendered = rendered[end:]
    for obj in objects:
        if obj["kind"] == "Deployment":
            obj["spec"]["template"]["spec"]["containers"][0]["image"] = args.image
    kube("apply", "-f", "-", payload={"apiVersion": "v1", "kind": "List", "items": objects})
    kube("-n", "corca", "rollout", "restart", "deployment/corca-api")
    print(kube("-n", "corca", "rollout", "status", "deployment/corca-api", "--timeout=180s", timeout=210))
    print(f"API deployed. Token is stored at {token_file}; its value is not logged.")


if __name__ == "__main__":
    main()

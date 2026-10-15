"""Submit a collected incident to the local deployed API without logging its token."""
import argparse
import contextlib
import json
import selectors
import socket
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

from corca.synthetic import drop_telemetry
from lab.setup import KUBECONFIG, ROOT


@contextlib.contextmanager
def forward():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(["kubectl", "--kubeconfig", str(KUBECONFIG), "--context", "kind-corca-lab",
                                "-n", "corca", "port-forward", "service/corca-api", f"{port}:8000", "--address", "127.0.0.1"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if selector.select(timeout=1):
                    line = process.stdout.readline()
                    if "Forwarding from" in line:
                        break
                if process.poll() is not None:
                    raise RuntimeError("API port forward exited before becoming ready")
            else:
                raise TimeoutError("API port forward did not become ready")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def incident_payload(folder, method="baro", tau=None, drop=0, seed=0):
    folder = Path(folder)
    collection = json.loads((folder / "collection.json").read_text())
    if collection["status"] != "complete":
        raise ValueError("only collected and recovered incidents may be diagnosed")
    df = drop_telemetry(pd.read_csv(folder / "metrics.csv"), drop, seed=seed)
    edges = json.loads((folder / "topology.json").read_text())["edges"]
    payload = {"columns": df.columns.tolist(),
               "rows": [[None if pd.isna(v) else float(v) for v in row] for row in df.values],
               "inject_time": int((folder / "inject_time.txt").read_text()), "method": method, "edges": edges}
    if tau is not None:
        payload["tau"] = tau
    return payload


def submit(url, payload):
    token = (ROOT / "runtime" / ".state" / "api-token").read_text().strip()
    response = requests.post(url + "/diagnose", json=payload, headers={"X-API-Key": token}, timeout=80)
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case")
    parser.add_argument("--method", choices=["baro", "circa"], default="baro")
    parser.add_argument("--tau", type=float)
    parser.add_argument("--drop-telemetry", type=float, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", help="save the diagnosis JSON")
    args = parser.parse_args()
    payload = incident_payload(args.case, args.method, args.tau, args.drop_telemetry, args.seed)
    with forward() as url:
        result = submit(url, payload)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

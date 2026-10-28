"""Re-export collected lab incidents through a telemetry source config.

The deployed service sees only what its source config queries, so its tau must be
calibrated on those same metrics. This rewrites each complete incident's metrics.csv
from the configured provider (same window, same labels) so runtime.evaluate can calibrate on it:

    kubectl -n corca port-forward service/prometheus 9090:9090 &
    python -m runtime.export_cases --from data/controls-v1 --out data/controls-v1-live \\
        --prometheus-url http://127.0.0.1:9090
    python -m runtime.evaluate --data data/controls-v1-live --out results/controls-v1-live --rates 0 --seeds 0

Prometheus must still hold the incidents (lab Prometheus keeps data only while its pod lives).
"""
import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from runtime.telemetry import fetch_window, load_source


def export(source, origin, out):
    exported = 0
    for collection in sorted(Path(origin).glob("*/*/collection.json")):
        if json.loads(collection.read_text()).get("status") != "complete":
            continue
        folder = collection.parent
        target = Path(out) / folder.relative_to(origin)
        if target.exists():
            raise FileExistsError(f"{target} exists; use a new --out folder")
        times = pd.read_csv(folder / "metrics.csv", usecols=["time"])["time"]
        frame = fetch_window(source, float(times.min()), float(times.max()))
        target.mkdir(parents=True)
        for name in ("collection.json", "ground_truth.json", "inject_time.txt"):
            shutil.copy2(folder / name, target / name)
        frame.to_csv(target / "metrics.csv", index=False)
        exported += 1
    if not exported:
        raise ValueError(f"no complete incidents under {origin}")
    return exported


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="origin", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source", default=str(Path(__file__).with_name("source.online-boutique.json")))
    parser.add_argument("--prometheus-url", help="override, e.g. a port-forward")
    args = parser.parse_args()
    source = load_source(args.source)
    if args.prometheus_url:
        if source.get("provider", "prometheus") not in ("prometheus", "grafana"):
            raise ValueError("--prometheus-url applies only to Prometheus-compatible sources")
        source["prometheus_url"] = args.prometheus_url
    print(f"exported {export(source, args.origin, args.out)} incidents to {args.out}")


if __name__ == "__main__":
    main()

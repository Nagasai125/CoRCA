"""Load RCAEval-format failure cases.

Expected layout (same as RCAEval):
    <root>/<service>_<fault>/<case_id>/data.csv        (RE1)  or metrics.csv (RE2/RE3)
    <root>/<service>_<fault>/<case_id>/inject_time.txt (unix seconds)

For our own multi-fault cases (week 6+), write the same layout, but name the
folder with all root causes joined by '+', e.g. 'cartservice_delay+currencyservice_cpu/1',
or put a ground_truth.json ({"root_causes": ["cartservice", "currencyservice"]}) in the case dir.
"""
import glob
import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .topology import service_of


@dataclass
class Case:
    path: str                      # case directory
    root_causes: list              # ground-truth services
    faults: list = field(default_factory=list)  # fault types, aligned with root_causes
    case_id: str = ""


def list_cases(root: str) -> list:
    """Find every case directory under `root` and parse its ground truth."""
    if not os.path.isdir(root):
        raise FileNotFoundError(f"data root not found: {root}")
    files = glob.glob(os.path.join(root, "**", "data.csv"), recursive=True)
    files += glob.glob(os.path.join(root, "**", "metrics.csv"), recursive=True)
    case_dirs = sorted({os.path.dirname(f) for f in files})
    if not case_dirs:
        raise FileNotFoundError(f"no data.csv or metrics.csv files under: {root}")
    cases = []
    for d in case_dirs:
        gt_file = os.path.join(d, "ground_truth.json")
        if os.path.exists(gt_file):
            with open(gt_file) as f:
                gt = json.load(f)
            rcs, faults = gt["root_causes"], gt.get("faults", [])
        else:
            label = os.path.basename(os.path.dirname(d))      # e.g. 'adservice_cpu'
            parts = [p.split("_", 1) for p in label.split("+")]
            rcs = [service_of(p[0]) for p in parts]
            faults = [p[1] if len(p) > 1 else "" for p in parts]
        cases.append(Case(d, rcs, faults, os.path.basename(d)))
    return cases


def load_case(case_dir: str, length_min: int = 20):
    """Return (df, inject_time) with the same cleaning and windowing as RCAEval's main.py:
    `length_min/2` minutes either side of the injection time (sampled at 1 s)."""
    path = os.path.join(case_dir, "data.csv")
    if not os.path.exists(path):
        path = os.path.join(case_dir, "metrics.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing data.csv or metrics.csv in: {case_dir}")
    df = pd.read_csv(path)
    if "time" not in df.columns:
        raise ValueError(f"{path} must contain a 'time' column")
    df = df.drop(columns=[c for c in df.columns if c == "time.1"])     # duplicated header in some RE1-OB cases
    df = df.loc[:, ~df.columns.str.endswith("_latency-50")]           # RCAEval keeps only p90 latency
    df = df.replace([np.inf, -np.inf], np.nan).ffill().fillna(0)

    with open(os.path.join(case_dir, "inject_time.txt")) as f:
        inject_time = int(f.readline().strip())

    half = length_min * 60 // 2
    normal = df[df["time"] < inject_time].tail(half)
    anomal = df[df["time"] >= inject_time].head(half)
    if normal.empty or anomal.empty:
        raise ValueError(f"{case_dir} has no samples on both sides of inject_time={inject_time}")
    return pd.concat([normal, anomal], ignore_index=True), inject_time

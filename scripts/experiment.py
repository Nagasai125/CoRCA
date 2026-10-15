"""Experiment identity and resumable JSONL records."""
import hashlib
import json
import platform
from importlib import metadata
from pathlib import Path

from corca.circa import FEATURE_POLICY
from corca.corca import STOPPING_POLICY


def digest_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_experiment(args, cases):
    root = Path(args.data).resolve()
    source = Path(__file__).resolve().parents[1]
    code = list((source / "corca").glob("*.py")) + list((source / "scripts").glob("*.py"))
    code += [source / "pyproject.toml", source / "uv.lock"]
    package = metadata.distribution("RCAEval")
    rcaeval_code = {str(p): digest_file(package.locate_file(p)) for p in package.files or []
                    if str(p).startswith("RCAEval/") and str(p).endswith(".py")}
    inputs = {}
    for case in cases:
        folder = Path(case.path)
        inputs[str(folder.resolve().relative_to(root))] = {
            name: digest_file(folder / name) for name in
            ("data.csv", "metrics.csv", "inject_time.txt", "ground_truth.json")
            if (folder / name).is_file()}
    config = {
        "schema_version": 2, "data_root": str(root), "inputs": inputs,
        "feature_policies": {m: FEATURE_POLICY if m == "circa" else "rcaeval-baro-v1" for m in args.methods},
        "stopping_policy": STOPPING_POLICY,
        "methods": sorted(set(args.methods)), "dataset": args.dataset,
        "max_rounds": args.max_rounds, "keep_resource": not args.no_keep_resource,
        "round_timeout": args.round_timeout, "seed": args.seed,
        "drop_telemetry": args.drop_telemetry,
        "code": {str(p.relative_to(source)): digest_file(p) for p in code if p.is_file()},
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": {d.metadata["Name"]: d.version for d in metadata.distributions()},
        "rcaeval_code": rcaeval_code,
    }
    identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    manifest = {"experiment_id": identity, "config": config}
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "experiment.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != manifest:
            raise ValueError("experiment configuration, code, environment, or data changed; use a new --out folder")
    elif (output / "runs.jsonl").exists():
        raise ValueError("existing results have no experiment fingerprint; preserve them and use a new --out folder")
    else:
        with path.open("x") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
    return identity


def read_runs(path, experiment_id=None):
    """Latest attempt per case/method. Never silently discard a damaged record."""
    latest = {}
    with open(path) as stream:
        for number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
                if experiment_id is not None and record.get("experiment_id") != experiment_id:
                    raise ValueError("record belongs to another experiment")
                latest[(record["case"], record["method"])] = record
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"invalid result at {path}:{number}: {exc}") from exc
    return list(latest.values())

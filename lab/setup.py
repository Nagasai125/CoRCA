"""Prepare or deploy the dedicated local CoRCA testbed."""
import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "lab"
STATE = LAB / ".state"
TOOLS = ROOT / ".tools"
KUBECONFIG = STATE / "kubeconfig"
KIND_VERSION = "v0.33.0"
HELM_VERSION = "v3.19.0"
CHAOS_VERSION = "2.8.4"
BOUTIQUE_VERSION = "v0.10.7"
BOUTIQUE_SHA = "ae3c20181c051048e6a5cae51fa530681affa4043ba60ae95d4f39e1262f2744"
KIND_SHA = {
    "darwin-arm64": "0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f",
    "darwin-amd64": "5a99f26f57246dc9319dd294803313197a0f34d33c525b3ea8b655db5916ece0",
    "linux-arm64": "20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2",
    "linux-amd64": "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d",
}


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def checked(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("download checksum mismatch")
    return data


def run(command, timeout=60, capture=False):
    print(" ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, timeout=timeout,
                          capture_output=capture, text=True)


def prepare():
    target = platform.system().lower() + "-" + {"arm64": "arm64", "aarch64": "arm64",
                                              "x86_64": "amd64"}.get(platform.machine(), "unsupported")
    if target not in KIND_SHA:
        raise ValueError("local setup supports macOS/Linux on arm64/amd64")
    if not shutil.which("kubectl"):
        raise ValueError("kubectl is required")
    TOOLS.mkdir(exist_ok=True)
    STATE.mkdir(exist_ok=True)
    kind = TOOLS / "kind"
    if not kind.exists() or hashlib.sha256(kind.read_bytes()).hexdigest() != KIND_SHA[target]:
        kind.write_bytes(checked(fetch(f"https://github.com/kubernetes-sigs/kind/releases/download/{KIND_VERSION}/kind-{target}"),
                                 KIND_SHA[target]))
    kind.chmod(0o755)
    helm = TOOLS / "helm"
    archive_url = f"https://get.helm.sh/helm-{HELM_VERSION}-{target}.tar.gz"
    checksum = fetch(archive_url + ".sha256sum").decode().split()[0]
    archive = checked(fetch(archive_url), checksum)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
        member = stream.extractfile(f"{target}/helm")
        if member is None:
            raise ValueError("helm archive is missing its executable")
        helm.write_bytes(member.read())
    helm.chmod(0o755)
    vendor = LAB / "vendor"
    vendor.mkdir(exist_ok=True)
    upstream_url = f"https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/{BOUTIQUE_VERSION}/release/kubernetes-manifests.yaml"
    (vendor / "online-boutique.yaml").write_bytes(checked(fetch(upstream_url), BOUTIQUE_SHA))
    rendered = run(["kubectl", "kustomize", LAB], capture=True).stdout
    (STATE / "online-boutique-rendered.yaml").write_text(rendered)
    chart = run([helm, "template", "chaos-mesh", "chaos-mesh", "--repo", "https://charts.chaos-mesh.org",
                 "--version", CHAOS_VERSION, "--namespace", "chaos-mesh", "--include-crds",
                 "--values", LAB / "chaos-values.yaml"], capture=True).stdout
    (STATE / "chaos-rendered.yaml").write_text(chart)
    inventory = {
        "kind": KIND_VERSION, "helm": HELM_VERSION, "chaos_mesh": CHAOS_VERSION,
        "online_boutique": BOUTIQUE_VERSION, "upstream_sha256": BOUTIQUE_SHA,
        "kind_sha256": KIND_SHA[target], "helm_archive_sha256": checksum,
        "boutique_rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "kubeconfig": str(KUBECONFIG), "namespace": "corca", "cluster": "corca-lab",
        "deployment_verified": False,
    }
    (STATE / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    return inventory


def deploy(inventory):
    info = json.loads(run(["docker", "info", "--format", "{{json .}}"], timeout=15, capture=True).stdout)
    if info["MemTotal"] < 6 * 1024**3:
        raise ValueError("allocate at least 6 GiB to Docker Desktop before deploying this lab; 8 GiB is preferred")
    kind, helm = TOOLS / "kind", TOOLS / "helm"
    clusters = run([kind, "get", "clusters"], capture=True).stdout.splitlines()
    if "corca-lab" not in clusters:
        run([kind, "create", "cluster", "--name", "corca-lab", "--config", LAB / "kind.yaml",
             "--kubeconfig", KUBECONFIG, "--wait", "180s"], timeout=600)
    else:
        run([kind, "export", "kubeconfig", "--name", "corca-lab", "--kubeconfig", KUBECONFIG])
    KUBECONFIG.chmod(0o600)
    kube = ["kubectl", "--kubeconfig", KUBECONFIG, "--context", "kind-corca-lab"]
    run([*kube, "apply", "-k", LAB])
    run([helm, "upgrade", "--install", "chaos-mesh", "chaos-mesh", "--repo", "https://charts.chaos-mesh.org",
         "--version", CHAOS_VERSION, "--namespace", "chaos-mesh", "--create-namespace",
         "--kubeconfig", KUBECONFIG, "--kube-context", "kind-corca-lab",
         "--values", LAB / "chaos-values.yaml", "--wait", "--timeout", "5m"], timeout=360)
    run([*kube, "-n", "corca", "rollout", "status", "deployment", "--timeout=300s"], timeout=360)
    run([*kube, "-n", "chaos-mesh", "rollout", "status", "daemonset/chaos-daemon", "--timeout=120s"], timeout=150)
    inventory["deployment_verified"] = True
    inventory["docker_memory_bytes"] = info["MemTotal"]
    (STATE / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    print("Testbed ready. No faults have been applied; verify telemetry before injection.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="download and render without creating a cluster")
    args = parser.parse_args()
    try:
        inventory = prepare()
        if args.prepare_only:
            print(f"Prepared and rendered manifests: {STATE}")
        else:
            deploy(inventory)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Local testbed setup stopped: {exc}\n")


if __name__ == "__main__":
    main()

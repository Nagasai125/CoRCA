# Local CoRCA Testbed

This lab targets only `corca-lab`, using `lab/.state/kubeconfig` and namespace
`corca`. It does not use the default Kubernetes context. The pinned components
are kind 0.33.0, Kubernetes 1.34.11, Online Boutique 0.10.7, Helm 3.19.0, and
Chaos Mesh 2.8.4. Redis is pinned by digest. Downloaded tools stay in `.tools/`.

## Prepare and Deploy

```bash
.venv/bin/python lab/setup.py --prepare-only
.venv/bin/python lab/setup.py
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca get pods
```

Docker Desktop must finish its first-run setup and have at least 6 GiB of
memory available to its VM; 8 GiB is preferred. The setup script verifies
downloads, renders both deployments, and waits for workload and chaos-daemon
readiness. The inventory records whether live deployment was verified.
The workload uses five simulated users. Chaos Mesh has one controller and is
restricted to the annotated `corca` namespace. Its dashboard is disabled.

View the shop using a localhost-only forward:

```bash
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca port-forward service/frontend 8080:80 --address 127.0.0.1
```

## Collection Gate

Deploying the lab is not an evaluated multi-fault experiment. Before injection,
verify timestamped CPU/memory and request latency/error telemetry, service
identity, a stable normal window, sustained load, and post-fault recovery.
Capture the actual application time of every fault from Chaos Mesh status.
Do not label the time of `kubectl apply` as proof that faults were active.

For each case, save `metrics.csv`, `inject_time.txt`, and `ground_truth.json`
using the existing RCAEval folder layout. Also retain the raw telemetry,
cluster inventory, workload settings, manifests, controller status, and
injection/recovery timestamps. Reject incomplete or non-overlapping incidents.

Start with independent CPU faults, then CPU faults on the same dependency
path, then combinations involving delay, then three roots. Run single-fault
controls under the same deployment and workload. Each scenario needs at least
five repeats; keep calibration and evaluation repeats separate. Freeze the
single-fault thresholds before scoring multi-fault or missing-telemetry data.

## Sources

- [kind setup](https://kind.sigs.k8s.io/docs/user/quick-start/)
- [Online Boutique release](https://github.com/GoogleCloudPlatform/microservices-demo/releases/tag/v0.10.7)
- [Chaos Mesh installation](https://chaos-mesh.org/docs/production-installation-using-helm/)
- [StressChaos](https://chaos-mesh.org/docs/simulate-heavy-stress-on-kubernetes/)
- [NetworkChaos](https://chaos-mesh.org/docs/simulate-network-chaos-on-kubernetes/)

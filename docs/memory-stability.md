# Lab Memory Stability

## Change And Scope

Only the dedicated `kind-corca-lab` currencyservice deployment was changed.
The repository overlay now requests 128 MiB and limits the container to
256 MiB, with `NODE_OPTIONS=--max-old-space-size=64`. Tracing, image, CPU
request/limit, five-user workload, and the running RCA API are unchanged.
The option limits V8 old space, not total process/native memory. See the
[Node documentation](https://nodejs.org/api/cli.html#--max-old-space-sizesize-in-mib).

Before the change, the service had reached 18 restarts. Recent Kubernetes
termination records showed `OOMKilled`/exit 137 under its 128 MiB limit,
while the node reported no memory pressure. Direct cgroup readings were
close to the limit. This supports a container-headroom problem; it does not
prove a particular memory leak or establish that the change fixes one.
A Node diagnostic process also exited 137 in the near-full old container;
do not run extra heavyweight diagnostics inside a container with no headroom.

Prometheus must not simply sum retained series across old container IDs
after a restart: that can yield apparent pod memory above the container
limit. The soak reads the current pod's cgroup directly and tracks its UID,
container ID, restart count, template hash, and memory limit.

`results/currency-memory-v1` preserves before/desired/after deployment
objects and pre-rollout pod status. The local inventory records the new
rendered manifest hash and keeps `memory_policy_validated` false until the
longer gate has actually passed. Existing incidents and scores are unchanged.

## Run And Inspect

The bounded 15-minute smoke is stored in
`results/currency-memory-v1/soak15m`. It is shorter than the old roughly
45-minute OOM cycle and cannot close the collection-stability gate.

These commands only inspect the dedicated lab:

```bash
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca get pods
kubectl --kubeconfig lab/.state/kubeconfig --context kind-corca-lab -n corca get stresschaos,networkchaos
tail -1 results/currency-memory-v1/soak15m/samples.jsonl
cat results/currency-memory-v1/soak15m/report.json
```

After reviewing the smoke, run a separate hour-long no-fault soak. Use a new
output folder; existing evidence is never overwritten:

```bash
PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 caffeinate -dims runtime/.venv/bin/python -m lab.soak --out results/currency-memory-v1/soak60m --seconds 3600 --interval 30
cat results/currency-memory-v1/soak60m/report.json
```

On non-macOS hosts omit `caffeinate`. Keep the Mac plugged in and its lid
open. The monitor rejects long sample gaps; caffeinate is not a guarantee
against host suspension or Docker pauses.

## Gate Semantics

A pass requires stable pod/container identity, restart count, deployment and
limit; all required application/load/telemetry pods ready; no node memory
pressure or active chaos; memory below 90% of the container limit at every
sample; unchanged lab configuration; and observed currencyservice traces
from the active workload. Failures are saved in `report.json` and raise an
error. No faults are injected or resources changed by the monitor.

Only a passed run requested for at least 3600 seconds sets
`long_soak_gate_met=true`. This is a bounded operational check, not proof
that memory can never grow, that future faults cannot cause an OOM, or that
the RCA service meets production accuracy or latency targets.

Do not collect a new benchmark during a healthy soak. After the long gate,
collect both new controls and independent test incidents under this same
deployment; do not reuse thresholds fitted under the old memory policy.
Algorithm selection must finish on development data before new held-out
incidents are used. A stable lab alone does not approve any CIRCA candidate.

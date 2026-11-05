# Milestone 1 — a controlled, trusted-team pilot

The objective is to establish whether one team's CoRCA deployment is useful and
operable with its real monitoring. This is not a multi-tenant SaaS release.
No additional onboarding or dashboard screens are required for this milestone.

## Current position

At the November 4 milestone baseline, the local Docker deployment responds to liveness and workspace-storage checks.
It has no telemetry connection, selected service scope, confirmed dependencies,
or sensitivity configured. A local preflight report is saved under
`results/pilot-m1/local-preflight.json` (local evidence, excluded from Git).
Application/fixture tests establish covered behavior; they are not customer
accuracy, live Alertmanager delivery, or sustained-load evidence.

| Gate | Completion evidence | Current status |
| --- | --- | --- |
| Deployment setup | Preflight with live metric verification; frozen image ID and source revision | Tool implemented; local telemetry setup pending |
| Diagnosis usefulness | Independently confirmed fault and healthy exports, scored with criteria fixed before evaluation | Assessment tool implemented; real cases pending |
| Storage recovery | Isolated snapshot/restore with matching contents and integrity checks | Local container drill passed; target-environment acceptance pending |
| Application recovery | Approved restart/rollback exercise preserves setup/history and correctly resumes queued jobs | Existing fixture coverage; target-deployment exercise pending |
| Alert delivery, if used | Real Alertmanager sends a firing alert; repeat delivery reuses its diagnosis ID | Local webhook fixture coverage; real sender pending |
| Reliability | Agreed workload, concurrency, observation period, deadline/error targets, and measured results | Targets and target-hardware run pending |
| Operational ownership | Named owner confirms access path, credential rotation, backup retention, rollback, and incident response | Team-specific decisions pending |

## 1. Freeze the pilot scope and acceptance criteria

Before collecting evaluation cases, record these decisions in your private pilot
notes. Choose numbers with the responsible team; the repository does not silently
supply a production accuracy or availability target.

- Environment/provider, service scope, fault classes, and intended users.
- Pilot owner and the engineers independently confirming incident causes.
- Image ID, source revision, dependencies, metric resolution, and sensitivity mode.
  Save the preflight report; back up the workspace and secrets separately.
- Number of independent fault incidents and healthy windows; how they are selected.
  Include known concurrent faults if concurrent-root performance is in scope.
- Minimum complete exact-root-set rate, acceptable healthy false-positive rate,
  unavailable/partial-result limits, and how uncertainty will be reported.
- End-to-end deadline (separate incident-window waiting from analysis), load,
  concurrency, observation period, acceptable failures, and recovery targets.

Use development incidents to select policies/thresholds. Freeze those choices
before new evaluation incidents. Do not inject faults into a real environment
without its owner's explicit authorization. Historical incidents can be reviewed
without creating a new outage. Synthetic cases stay labeled synthetic.

## 2. Run deployment preflight

From the project environment:

```sh
.venv/bin/python -m runtime.pilot preflight \
  --url http://localhost:8000 --verify-metrics \
  --out results/pilot-m1/preflight-001.json
```

`--verify-metrics` issues a read-only query through the configured provider using
the existing metrics-check endpoint. Without it, live metrics remain pending.
No connections, thresholds, or diagnosis jobs are created or changed.
The report includes endpoint timings; these individual timings are not a load test.

For an authenticated deployment, set `CORCA_API_TOKEN` in the process environment,
or select an existing variable with `--token-env`. Do not put the secret in command
arguments. Remote token use requires HTTPS; redirects are not followed. Reports
exclude deployment URLs, credential values, queries, and raw response bodies.

Exit 0 means the command completed without a blocking setup failure; it never
means production approval. Exit 2 means setup is incomplete and a report was
written. Exit 1 means the command or evidence input is invalid. All reports use
new paths, refuse overwrite, and are created with owner-only permissions.

If setup is missing, use Integrations and Services, then rerun with a new output
path. Keep the earlier report. Access review and real alert delivery remain manual
acceptance gates even when every automatic check passes.

## 3. Assess confirmed incidents

Download diagnosis JSON files from the application. Copy
[pilot-cases.example.json](pilot-cases.example.json) into your private evidence
folder and replace every example entry with confirmed evidence. Export paths
are relative to the manifest. Use `root_causes: []` only for a confirmed healthy
window. Declare `evidence_kind` as `synthetic`, `controlled_drill`, or `real_incident`.
This is an operator declaration; the tool cannot independently verify that label.

```sh
.venv/bin/python -m runtime.pilot assess \
  --manifest results/pilot-m1/cases.json \
  --out results/pilot-m1/assessment-001.json
```

Use one case per independent incident and one manifest per comparison run.
Duplicate job IDs and repeated incident-time/method pairs are rejected.
Missing exports, failed jobs, and insufficient evidence remain in the relevant
method's fault-score and healthy-abstention denominators. Partial results can
match the root set but do not count as complete exact results. Healthy
false-positive rate uses only evaluable healthy cases; review unavailable cases
separately. Per-case results and input hashes make the calculation reviewable.

A method with no fault or healthy examples has null metrics for that category.
An assessment cannot establish a held-out split, unchanged deployment, adequate
sample size, causal identification, or production readiness. Review its results
against the predeclared criteria, without tuning on the evaluation set.
Uploaded log reports can support human incident review; they are not labelled
causal diagnoses and are not accepted as metric-diagnosis exports by this scorer.

## 4. Exercise storage recovery without touching the live workspace

On the deployment host/container, point at the actual SQLite database:

```sh
docker compose exec -T corca python -m runtime.pilot recovery \
  --db /var/lib/corca/corca.db --work-dir /tmp/pilot-drill \
  --out /tmp/pilot-recovery-001.json
# Export the report from tmpfs into a new, owner-readable local file:
umask 077
set -C
docker compose exec -T corca cat /tmp/pilot-recovery-001.json > results/pilot-m1/recovery-001.json
```

The command makes a consistent snapshot, restores it to another temporary file,
compares logical database contents, and deletes both temporary databases. The
source stays unchanged; no second application worker is started. The report
contains counts and a hash, not connection settings or job contents.

This does not create a retained/off-host backup or prove application recovery.
Use the [operations guide](operations.md) for a retained backup and a planned
restart/rollback drill. Restored queued jobs can run when the application starts;
use a separate isolated environment for that exercise. Recover secrets through
your existing secret manager. Record elapsed recovery time and actual behavior.

## 5. Complete environment acceptance

Run and retain the real Alertmanager delivery check if automatic alerts are used.
Agree and execute the load/deadline test on intended hardware. Have the operational
owner review all gate evidence and explicitly record whether the pilot can begin,
continue with restrictions, or stop. Do not call the platform production ready
because fixtures, preflight, or a SQLite restore passed.

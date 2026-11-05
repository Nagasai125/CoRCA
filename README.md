# CoRCA — incident investigation workspace

CoRCA is a self-hosted beta for teams investigating microservice incidents.
Connect your monitoring backend, confirm service dependencies, and choose an
incident start time. CoRCA ranks candidate causes, shows unexplained symptoms,
and keeps shareable, exportable diagnosis history.

Supports Prometheus, Grafana Cloud Metrics, Datadog, Dynatrace, and New Relic.
OpenTelemetry can feed your existing historical metrics backend. Alertmanager
can start investigations automatically.

## Start the beta

Requires Docker with Compose. From this checkout:

```sh
docker compose up -d --build
```

Open [the workspace](http://localhost:8000/app). A fresh local installation opens
directly; enable individual accounts before sharing access (see below). The application keeps its SQLite database in a persistent Docker volume. No
Kubernetes cluster, benchmark dataset, or new data collection is required.
The first image build downloads pinned runtime dependencies.

1. In **Integrations**, connect your existing metrics backend and verify metrics.
2. In **Services**, discover services, select those to investigate, and save actual
   caller → callee dependencies. Explicitly confirm an empty graph for independent services.
3. **Set investigation sensitivity**. Start with the manual BARO threshold of 6,
   then adjust it based on your operational experience. This is a heuristic,
   not environment calibration. Existing calibration files are an advanced option.
4. Choose **New diagnosis**, enter the incident start, and review the findings.
   The service waits for the selected incident window to finish before analysis.
5. Share the diagnosis link with teammates on the same deployment, or download
   its JSON. Cancel queued work when an investigation is no longer needed.

For a Prometheus server on your host machine, use `host.docker.internal` instead
of `localhost` in the connection form. Allow the hostname before starting:

```sh
CORCA_ALLOWED_SOURCE_HOSTS=host.docker.internal docker compose up -d
```

For vendor credentials, private endpoints, query examples, and window resolution,
see [telemetry setup](docs/telemetry-setup.md). Enter API keys/tokens directly in the connection form, or use server environment
variables through the advanced option. Secret values stay in private server files;
SQLite settings and exports contain references only. Remote users need your private network
or HTTPS access gateway: Compose binds to local loopback by default.

## Operate and support

```sh
docker compose ps
docker compose logs --tail=100 corca
docker compose restart corca
# Stop while preserving saved workspace data:
docker compose down
```

Use [the operations guide](docs/operations.md) for credential setup, alerts,
backup/restore, upgrades, and troubleshooting. Do not use `down -v` unless you
intend to delete the workspace volume.

This beta supports one service replica and a trusted team with access to the deployment.
Named local accounts, administrator/responder roles, and configuration activity
are available under Team & access. Before accounts are enabled, access is shared
and everyone who can reach the deployment can change settings. SSO, MFA, and
tenant isolation are not included.

Diagnosis results are investigation leads. Manual sensitivity is labeled in
results; partial results show evidence still unexplained. Score shares are not
probabilities. Confirm findings against dashboards/logs before remediation.
BARO is the default; CIRCA remains an advanced method enabled by compatible
existing calibration. CPU/memory coverage alone may miss latency/error faults.

## Evaluation evidence for customers

Open **Quality** to review published diagnosis evaluations, AI help quality,
and release checks separately. Each summary includes sample counts, scope,
evidence type, limitations, completion time, publisher, build revision when
provided, and a pipeline link when provided. Empty categories have no assumed
score. Passing software checks do not establish incident accuracy.

Administrators can upload prepared summaries. Pipelines can publish them with a
separate, publish-only token. Existing pilot assessments and JUnit reports have
CLI converters; CI retains a software-check summary artifact and optionally
publishes it on default-branch pushes when configured.
See [evaluation operations](docs/operations.md#evaluation-results-and-pipeline-publication).
CoRCA displays results; Jenkins and other pipelines still execute their own jobs.

## Ask CoRCA

Choose **Ask CoRCA** from any workspace screen, or open **Help**. Ask about
connections, alerts, log uploads, roles, or investigation results. Answers include
product-guide sources and links to the relevant screen. Guide answers work
locally without a model; the interface clearly identifies this mode.

An optional server-side OpenAI Responses connector provides generative answers
using the bundled guides and basic setup status. It needs explicit administrator
activation, a compatible model, and a server-side API key. No AI provider is
configured by default. See [customer-help operations](docs/operations.md#customer-help-bot).
No logs, credentials, raw queries, service names, or incident contents are
automatically attached. Do not type secrets into chat. CoRCA does not persist
chat; it clears on reload. The bot explains and navigates; it cannot change
settings or investigate incident evidence itself.

## Individual accounts and roles

Open **Team & access** for setup instructions. Create the first administrator on
this deployment's host; the command prompts privately for a password:

```sh
docker compose exec corca python -m runtime.access bootstrap-admin \
  --db /var/lib/corca/corca.db --username your.name
```

Reload the console and sign in. Administrators can add people, change roles,
disable accounts, reset passwords, and review configuration activity. Responders
can investigate, analyze logs, and read shared history; configuration changes and
team management are restricted to administrators. Everyone can change their own
password. Password resets, role changes, and disabling accounts revoke sessions.

For shared deployments, set `CORCA_REQUIRE_ACCOUNTS=1` in `.env` **after bootstrap**
and recreate the service. Startup then refuses a workspace without accounts.
Use HTTPS for remote access. Keep a separate `CORCA_API_TOKEN`/`CORCA_TOKEN_FILE`
for Alertmanager; once accounts exist, this token grants webhook delivery only.
Existing gateway-injected shared keys no longer grant console access.
See [operations](docs/operations.md#individual-access) for deployment and recovery.
Local accounts do not provide SSO, MFA, or tenant isolation.

## Live alerts

Open **Alerts** to see received Alertmanager events, firing/resolved state,
severity, delivery time, and the linked investigation. The visible console
refreshes every five seconds. Search and status filters query the stored inbox;
up to 200 matching records are displayed. State reflects the latest received
webhook, not a direct health check of Alertmanager.

**Connect alerts** generates the receiver configuration for your reachable
CoRCA address, with resolved notifications enabled. Merge the receiver and route
the alerts you want to it in your existing Alertmanager. **Test CoRCA receiver** (under Connection settings → Troubleshooting and advanced details) checks
the receiver/storage only, queues no diagnosis, and is visibly distinguished from
external delivery. Real firing alerts start diagnoses once monitoring and
sensitivity are configured. Rules, routing, and silences stay in Alertmanager.

## Analyze log files

Open **Logs**, choose a UTF-8 `.log`, `.txt`, `.jsonl`, or `.ndjson` file, and select
**Analyze logs**. No monitoring connection is required. Optionally enter an incident
start and a fallback service name. Reports show recurring error/warning patterns,
line-numbered excerpts, per-service counts, and a timeline for recognized timestamps.
These are log observations, not causal root-cause findings. Download the JSON report
to keep it; uploads and reports are not saved on the server.

Limits: 2 MiB, 50,000 lines, 16,384 characters per line. JSON logs use one object per
line, with common timestamp, level, message, and service fields. ISO timestamps and
Unix seconds/milliseconds are supported; timestamps without an offset assume UTC.
Missing timestamps/severity and malformed JSON lines are disclosed in the report.
Incident comparisons are counts over available entries, not equal-duration rates.

Clients can send raw file bytes to `POST /v1/logs/analyze?filename=incident.log`,
with optional `service` and `incident_start` (Unix seconds) query parameters.
The deployment's API authentication and request limits apply.

## Controlled pilot

Use the [pilot milestone plan](docs/pilot-plan.md) to check deployment setup,
assess exported diagnoses against independently confirmed incidents, and exercise
storage recovery. These operator checks do not grant production approval.

## Develop

Python 3.12, with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen --extra dev --extra server
uv pip install --no-deps -r runtime/rcaeval.lock
uv run --no-sync corca-serve
```

Open `http://localhost:8000/app` directly; no key entry is needed.
Run application and core checks with:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run --no-sync python -m pytest -q tests runtime
# Navigation handler regressions (Node.js, development only):
node --test tests/console-navigation.test.cjs tests/console-html.test.cjs
```

The same checks run in GitHub Actions. The package includes the web console and
`corca-serve` / `corca-admin` commands. The Docker image uses hash-pinned runtime
requirements and runs as a non-root user with a read-only filesystem in Compose.

## Project documentation

- [Operations and troubleshooting](docs/operations.md)
- [Telemetry integrations](docs/telemetry-setup.md)
- [Beta scope and prior validation](docs/beta-review.md)
- [Research and benchmark workflow](docs/research-workflow.md) — optional development tooling

Application code lives in `runtime/`, the ranking engine in `corca/`, and
optional experiment tooling in `scripts/` and `lab/`.

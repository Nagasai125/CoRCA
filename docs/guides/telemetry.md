# Connect telemetry and discover services

CoRCA reads one active telemetry backend for a deployment. Each backend can supply
metrics for many services. Keep your existing agents or OpenTelemetry
instrumentation; you do not need to enter a business API key for every service.

## In the workspace

1. Open **Setup → Connect & verify** and select a provider card. Enter its query endpoint,
   or choose a region for Datadog/New Relic. Only that provider’s form is shown.
2. Adapt the example queries to your actual metrics and service label/dimension.
   Every metric query must return one series per service.
3. Enter the provider's API key/token directly. Grafana also needs its Metrics
   instance ID; Datadog needs both API and application keys; New Relic needs its
   account ID. Datadog and New Relic use a region selector. Prometheus supports
   optional bearer tokens or basic-auth username/password. These are API
   connections, not OAuth sign-in with a provider account.
   The advanced environment-variable option remains available.
4. Choose **Verify & connect**. CoRCA verifies metrics before replacing the saved
   connection. Failed verification keeps the existing connection. Discovery lists services, per-metric
   observed coverage, and missing configured metrics. It does not install agents
   or infer call relationships from metric names.
5. On **Setup → Services**, select the reporting services to analyze, define caller → callee
   pairs, and save. For independent services, explicitly confirm an empty graph.
6. Set investigation sensitivity to enable BARO with an explicitly manual threshold.
   Start with 6 and review findings against your dashboards. No new collection is
   required. Alternatively upload an existing compatible calibration artifact fitted
   to the same queries, resolution, service scope, and dependencies. Then start a
   diagnosis or configure the Alertmanager receiver.

CoRCA validates calibration values and algorithm policies. The administrator is
responsible for verifying that the artifact was collected from the stated
telemetry; uploading an unrelated artifact is not valid environment calibration.

## Direct credential storage

Directly entered secret values are stored in owner-only JSON files (0600) under
`credentials/` (0700), beside the workspace database. The default Docker volume
keeps this directory at `/var/lib/corca/credentials`. The application does not
provide encryption at rest; protect the volume using your host/disk controls.
Use HTTPS for remote browser access and remote authenticated telemetry endpoints.

Passwords/API keys are never returned in metadata, stored in browser storage,
written into SQLite, or included in source downloads. Exported source files use
server-variable placeholders; provide secrets separately when reusing them.
Verification runs read-only queries against the configured, allowed endpoint.
Old credential files remain for queued diagnoses using their original connection.
Back up these files separately from SQLite, or reconnect providers after restore.
Do not delete old files while queued/running jobs still reference them.

## Provider requirements

| Provider | Connection | Server credentials | Query contract |
| --- | --- | --- | --- |
| Prometheus | Prometheus query base URL | None, optional bearer `token_env`, or basic `username` + `token_env` | PromQL, grouped by `service_label` |
| Grafana Cloud Metrics | Metrics **query endpoint**, not the dashboard URL | Metrics instance ID as `username`; access-policy token via `token_env` with `metrics:read` | PromQL through the Prometheus-compatible query API |
| Datadog | Regional API URL, such as `https://api.datadoghq.com` | `api_key_env` and `app_key_env`; keys with query access | Timeseries query grouped by the service tag; explicit 60-second rollup |
| Dynatrace | Environment URL or an approved ActiveGate environment base URL | `token_env`, with `metrics.read` | Metrics API v2 selector split by the service dimension; one-minute resolution |
| New Relic | Regional NerdGraph URL ending in `/graphql` | `token_env` referencing a User API key, plus numeric `account_id` | NRQL SELECT with one service FACET and one numeric `AS value`; CoRCA supplies SINCE, UNTIL, TIMESERIES, and LIMIT |

Examples in the form are starting points, not automatic mappings for every
installation. For example, Datadog APM metric names depend on the instrumented
span name, and Dynatrace series may use entity IDs rather than friendly names.
Dependency names must match the service IDs returned by the queries.

Vendor adapters use 60-second buckets. Each healthy and incident window must
contain at least ten intervals: **at least ten minutes per window**. The console
adjusts the defaults. The API extends omitted default windows for the source
resolution and rejects explicitly shorter windows. Prometheus-compatible
sources support query steps of 1–60 seconds through the source file; the console
provides common choices. Missing values stay missing when adapters read them;
the existing diagnosis preparation policy handles short gaps.

### Official provider references

- [Datadog application instrumentation](https://docs.datadoghq.com/tracing/trace_collection/) and [timeseries query API](https://docs.datadoghq.com/api/latest/metrics/query-timeseries-points/).
- [Dynatrace OneAgent](https://docs.dynatrace.com/docs/ingest-from/dynatrace-oneagent) and [Metrics API v2 data queries](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/metric-v2/get-data-points).
- [New Relic instrumentation](https://docs.newrelic.com/docs/distributed-tracing/concepts/quick-start/) and [NerdGraph NRQL](https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-nrql-tutorial/).
- [Grafana Cloud query endpoints and authentication](https://grafana.com/docs/grafana-cloud/send-data/metrics/metrics-prometheus/query-http-api/).

## OpenTelemetry

Use your language's supported auto-instrumentation/SDK and set `service.name` for
each service. Route metrics through an OpenTelemetry Collector to a durable
Prometheus-compatible backend, then connect that backend above. A Collector's
scrape exporter alone does not provide historical query windows.

CoRCA is not an OTLP receiver, trace storage system, or log backend. It can analyze
latency/error metrics derived by your existing pipeline, including span metrics,
but it does not derive service dependencies from traces itself.
See [Collector configuration](https://opentelemetry.io/docs/collector/configuration/)
and [OpenTelemetry/Prometheus integration](https://opentelemetry.io/docs/compatibility/prometheus/otlp-metrics-export/).

## Administration and persistence

- Start with a persistent job database (`--db`, default
  `backend/.state/corca.db`). Browser-managed connections, selected services,
  dependencies, and thresholds persist in SQLite. Secret **values** are resolved from private credential files or server environment
  variables when querying; they are not persisted in SQLite.
- Existing `--source` and `--calibration` files bootstrap a deployment. Once a
  connection has been saved through the workspace, the saved connection and its
  calibration take precedence on restart. Manage subsequent changes in the UI.
- Credential references saved through the UI must begin `CORCA_TELEMETRY_`.
  Allow remote private/custom source hostnames with the comma-separated server
  variable `CORCA_ALLOWED_SOURCE_HOSTS`. Loopback hosts and recognized supported
  SaaS API hosts are allowed by default. Redirects are disabled to avoid forwarding
  credentials to another endpoint. Use HTTPS for remote sources.
- Saving a source or topology clears the active sensitivity settings. Set manual
  sensitivity or upload matching existing calibration afterward. Each newly queued diagnosis snapshots its source and
  thresholds, so later connection changes do not change that job's configuration.
  Credential rotation still uses the current value of its server variable.
- Existing pending jobs are bound to the startup configuration when migrating
  an older database. Existing history is retained; the new SQLite column and
  settings table are added in place.
- This is a shared workspace with direct access by default. Anyone who can reach
  the deployment can change its settings. Enable named accounts under Team & access before sharing it. Administrators manage setup, responders investigate, and configuration changes have actor-attributed activity. SSO, MFA, and tenant isolation remain unavailable.
- The diagnosis input limit is 199 metric columns plus time. Narrow service scope
  or queries for larger fleets. Discovery visibility is limited to what the
  configured queries and provider return; it is not a complete infrastructure
  inventory or automatic instrumentation service.

## Validation boundary

Provider adapters have tests for response parsing, authentication placement,
missing observations, malformed/partial responses, duplicate series, and failures.
The full local browser workflow also runs against an HTTP Prometheus fixture and
executes the actual diagnosis worker. These checks do not establish live SaaS
connectivity: verify each customer's keys, region, query schema, reporting
resolution, historical retention, and alert delivery in that environment.

For standard Docker deployment, credential overrides, queue limits, and backup/restore,
see [the operations guide](operations.md). Readiness now checks application storage
independently of sensitivity setup so fresh installations can reach onboarding.

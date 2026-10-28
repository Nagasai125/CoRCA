# CoRCA beta review — October 27, 2026

The recent API and console changes provide a useful foundation for a self-hosted,
trusted-team beta. Passing tests establish the covered behavior, not production
readiness or causal accuracy on every workload. This review focused on the latest
console, landing page, job API, telemetry adapters, and deployment instructions.
See [telemetry setup](telemetry-setup.md) for current provider contracts.

## Changes made

- Replaced the single-screen console with Overview, Diagnoses, Services, Integrations, and
  Workspace guide. Navigation and diagnosis links remain within the existing
  FastAPI application; no frontend framework or build step was introduced.
- Added an administrator/responder onboarding flow, live job counts, recent jobs,
  search, status filters, diagnosis exports, and API schema downloads.
- Added browser-managed connections for Prometheus, Grafana Cloud Metrics, Datadog,
  Dynatrace, and New Relic. Credential values stay in the server environment;
  source files and saved settings hold variable references. OpenTelemetry metrics
  can flow through the connected historical backend.
- Added service discovery with observed metric coverage, missing metrics, service
  selection, dependency editing, calibration upload, and source revision checks.
  Discovery does not infer dependencies from metric names.
- Added persistent connection settings and per-job source/calibration snapshots.
  Saving a different source or topology clears the active calibration. Minute-level
  vendor metrics require at least ten minutes on each side of an incident.
- Kept authenticated, redacted integration metadata and a deployed-source metrics
  verification. Verification is a timestamped snapshot; Alertmanager receiver
  readiness still requires an actual delivery test.
- Corrected queue/retry descriptions, bounded browser requests, guarded responses
  from a disconnected session, and stopped unchanged polling responses from
  rebuilding the diagnosis content.
- Corrected the landing page's absolute root-cause claim and deployment advice:
  `runtime.deploy` operates on the dedicated local lab, not an arbitrary cluster.
- Preserved system appearance, keyboard focus, labels, and reduced-motion support.
  Light blue status text is #195fba on #e3eefb (5.28:1). Dark blue status text uses
  #91bcff on #1d3152. Body text uses #4a5468 on white (7.61:1) and #a9b3c4 on
  #172036 (7.66:1). Tables scroll locally on narrow screens.

## How teams connect

1. An administrator starts one service replica with persistent storage, a private
   access path/HTTPS gateway, and an API key. They set telemetry credential variables
   and allow private endpoint hostnames as needed.
2. They connect a provider in Integrations and discover reporting services.
3. They select services, save real caller/callee dependencies, and upload calibration
   fitted to the same telemetry, scope, resolution, and topology.
4. They add the Alertmanager receiver and route; the token file must be mounted in
   Alertmanager. A real firing test alert verifies delivery and diagnosis creation.
5. Trusted responders receive the deployment URL and key, choose an incident start,
   inspect candidate causes and unexplained symptoms, and share links or export JSON.

Administrator and responder are workflow responsibilities, **not enforced roles**.
Every API key holder has the same access to this deployment's history. Diagnosis
links require access to the same deployment and a valid key.

## Validation

- Original baseline: **101 tests passed** after installing the repository's runtime
  requirements and httpx into the local environment.
- Updated suite: **120 tests passed** with `python -m pytest -q tests runtime`.
  Tests cover provider response contracts, authentication placement, redaction,
  secret references, host restrictions, missing data, duplicate/partial series,
  persistence, stale revision rejection, calibration invalidation, job snapshots,
  legacy SQLite migration, and escaped service IDs. The dependency emits a
  Starlette TestClient/httpx deprecation warning.
- Headless Chrome checks exercised bad and valid API keys against the local API,
  missing-source setup, source JSON download and invalid JSON, populated diagnoses,
  search and status filters, result export, invalid/future incident time, diagnosis
  submission, connection verification, disconnect, mobile layout, dark appearance,
  and reduced motion. Additional checks covered all workspace views at 320px,
  direct integration links, and disconnect after an expired key. No JavaScript
  errors occurred.
- Additional browser checks used a real local HTTP Prometheus fixture and the
  actual API/worker to connect, discover services, save dependencies, upload
  calibration, queue an incident, and render a result. Other populated scenarios
  used intercepted fixtures. No live customer Prometheus,
  Alertmanager delivery, Kubernetes rollout, or enterprise identity provider was
  exercised. Actual integration delivery remains an environment acceptance check.

## Limits before wider enterprise rollout

| Area | Current behavior | Required before broader rollout |
| --- | --- | --- |
| Identity and authorization | Shared service key | Individual identities, SSO, session policy, and enforced permissions |
| Isolation | One deployment and shared job history | Tenant isolation if hosting multiple organizations |
| Auditability | Operational logs and job history | Durable actor-attributed audit records and retention policy |
| Availability | SQLite, one service replica | Database/worker architecture and recovery verification before scaling |
| Connectivity | Five telemetry query adapters, Alertmanager webhook | Verify real network reachability, representative metrics, and alert delivery |
| Diagnosis quality | Evidence ratios and supplied topology | Environment-specific calibration and held-out fault/healthy validation |
| Telemetry coverage | Bundled source has CPU and memory | Add latency/error sources for fault classes not visible in resource metrics |

The beta UI deliberately exposes these limits. It does not advertise SSO, RBAC,
or unavailable integrations as working features.

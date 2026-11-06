# CoRCA API

CoRCA exposes a same-origin HTTP API for a single persistent investigation
workspace. The default Compose URL is `http://localhost:8000`. Most endpoints
use `/v1`; health probes, the synchronous `/diagnose` endpoint, and the schema
use their paths below. JSON requests use `Content-Type: application/json`.
Log uploads use raw bytes.

Use the [OpenAPI 3.1 snapshot](openapi.json) for exact request fields, defaults,
enums, query parameters, and numeric bounds. An authenticated deployment also
serves its current schema at `GET /openapi.json`. Swagger `/docs` and ReDoc
`/redoc` are disabled. Many handlers return dictionaries without response models:
an empty response schema does **not** mean the response has no body. Response
behavior and authorization details below supplement the generated schema.

## Authentication and permissions

| Deployment or actor | How to authenticate | Access |
| --- | --- | --- |
| Fresh local workspace, no accounts or API token | No credentials | Shared administrator access; keep deployment private |
| Shared-key workspace, accounts disabled | `X-API-Key: <token>` or `Authorization: Bearer <token>` | Administrator access |
| Named administrator or responder | Log in, then send the `corca_session` cookie | Role-based workspace access |
| Alertmanager with accounts enabled | Server API token in either token header | Only `POST /v1/alerts` |
| Evaluation publisher | Separate evaluation token in either token header | Only `POST /v1/evaluations` |

Configure the service key using `CORCA_API_TOKEN` or `CORCA_TOKEN_FILE`, and the
separate publisher key using `CORCA_EVALUATION_TOKEN` or
`CORCA_EVALUATION_TOKEN_FILE`. Keys must be at least 16 characters; the publisher
key must differ from the service key. Header credentials do not create user sessions.
Account-enabled deployments do not offer a general-purpose administrator API token.
The OpenAPI security schemes describe token headers; cookie roles and scoped-token
restrictions are enforced by the backend and described here.

Create the first administrator on the deployment host with
`python -m backend.access bootstrap-admin --db <workspace.db> --username <name>`.
There is no public registration or default password. See [individual access](../guides/operations.md#individual-access).
Login requires HTTPS except on localhost/loopback. Cookies are HttpOnly,
SameSite=Strict, Secure on HTTPS, and expire after eight hours. Changes to passwords
or account access revoke affected sessions. Cross-origin changes are rejected when
an `Origin` header is present and does not match the request base URL.

Examples use Bash or Zsh, curl, and Python 3. They assume a local shared workspace:

```sh
BASE_URL=http://localhost:8000
AUTH=()
# For shared-key mode instead:
# AUTH=(-H "X-API-Key: $CORCA_API_TOKEN")
```

For an account-enabled workspace, read credentials interactively and use a private
cookie jar; avoid putting literal passwords in shell history:

```sh
COOKIE_JAR=$(mktemp)
python3 -c '
import getpass, json, sys
print("Username: ", end="", file=sys.stderr, flush=True)
print(json.dumps({"username": input(), "password": getpass.getpass()}))
' | curl --fail-with-body -sS --cookie-jar "$COOKIE_JAR" \
  -H 'Content-Type: application/json' --data-binary @- "$BASE_URL/v1/access/login"
AUTH=(--cookie "$COOKIE_JAR")
```

After signing out, remove the cookie jar with `rm "$COOKIE_JAR"`. Protect this
file like a credential while the session is active.

## Endpoint reference

Access labels: **public** needs no authentication; **user** permits named
administrators/responders or shared administrator mode; **admin** requires
administrator access; **account admin** additionally requires enabled accounts.
Token-only publishers and Alertmanager have only the explicitly scoped routes.
Unless shown otherwise, success is HTTP 200.

### Health and discovery

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/healthz` | public | `{"status":"ok"}` when the process responds |
| `GET` | `/readyz` | public | Checks workspace storage and worker liveness; 503 if unavailable; setup can still be incomplete |
| `GET` | `/openapi.json` | user | Current OpenAPI schema; excluded from its own schema |
| `GET` | `/v1/config` | user | Enabled methods, `prometheus` connection flag, alert method |
| `GET` | `/v1/integrations` | user | Safe provider metadata, source revision, thresholds, resolution and queue limit |
| `POST` | `/v1/integrations/check` | user | Queries the saved source; returns verification time and observed metric/service counts |
| `POST` | `/v1/integrations/prometheus/check` | user | Deprecated alias of the provider-neutral check |

The legacy `prometheus` field means a telemetry source is configured even when
the provider is Grafana, Datadog, Dynatrace, or New Relic. Readiness does not verify
provider availability or diagnosis accuracy. Source URLs, query text, and secrets
are not returned by integration metadata.

### Connections and service configuration

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `POST` | `/v1/connections` | admin | `ConnectionSetup`: source, revision, optional credentials/verify; saves connection and clears thresholds |
| `GET` | `/v1/services` | user | Saved scope and dependencies; coverage is unverified until explicitly queried |
| `POST` | `/v1/services/discover` | user | Live service discovery; returns revision and checked time; does not save scope |
| `POST` | `/v1/topology` | admin | `TopologySetup`: revision, selected services, edges; saves scope and clears thresholds |
| `POST` | `/v1/sensitivity` | admin | `SensitivitySetup`: revision and tau; enables manual BARO sensitivity |
| `POST` | `/v1/calibration` | admin | `CalibrationSetup`: revision and artifact; loads supported thresholds after policy validation |

`source.provider` is `prometheus`, `grafana`, `datadog`, `dynatrace`, or `newrelic`.
Provider-specific source fields and credential requirements are in
[telemetry setup](../guides/telemetry.md). Source and calibration objects are
also validated beyond the generic dictionary types in OpenAPI. Connections with
supplied credentials are verified before saving, even when `verify` is false.
Verification failure does not replace the saved connection. Remote credentials
require HTTPS. Custom hosts must be allowed by `CORCA_ALLOWED_SOURCE_HOSTS`;
provider hostname rules are enforced separately. Environment credential references
must use `CORCA_TELEMETRY_` names.

Configuration changes use optimistic concurrency: use `revision: null` for the
first connection; otherwise copy the current source revision from
`GET /v1/integrations`. A mismatched revision returns 409. Refetch metadata after
changes. Edges are `[caller, dependency]`, and both names must be in the saved
service scope. Saving connections or topology invalidates previous thresholds;
set manual sensitivity or upload compatible calibration afterward. Manual tau
is 0.1–1000 and enables BARO only; it is not a confidence percentage.

Example first connection, with a query that must match your actual metric labels:

```sh
curl --fail-with-body -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
  "$BASE_URL/v1/connections" --data-binary @- <<'JSON'
{
  "source": {
    "provider": "prometheus",
    "prometheus_url": "http://localhost:9090",
    "service_label": "service",
    "step": 15,
    "queries": {"cpu": "sum by (service) (rate(container_cpu_usage_seconds_total[1m]))"}
  },
  "revision": null,
  "verify": true
}
JSON
```

In Docker, `localhost` refers to the CoRCA container. Use an accessible source
hostname and allow it on the server as described in operations. Next:

1. Call `POST /v1/services/discover` and select actual discovered names.
2. Save `POST /v1/topology` with `{"revision":"<current>","services":["checkout-api","payments-db"],"edges":[["checkout-api","payments-db"]]}` using your names.
3. Read the source revision again, then send `POST /v1/sensitivity` with
   `{"revision":"<current>","tau":6}` or load an independently calibrated artifact.
4. Confirm available methods through `GET /v1/config` before submitting work.

### Diagnosis jobs

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `POST` | `/v1/diagnoses` | user | `WindowRequest`; HTTP 202 with the queued job |
| `GET` | `/v1/diagnoses` | user | Most recent jobs; `limit` 1–200, default 20; returns an array |
| `GET` | `/v1/diagnoses/{job_id}` | user | One job; 404 for unknown IDs |
| `POST` | `/v1/diagnoses/{job_id}/cancel` | user | Cancels queued jobs only; returns updated job, or 409 if no longer queued |
| `POST` | `/diagnose` | user | `Incident`; synchronous diagnosis of supplied metric rows, without retained job history |

`WindowRequest` fields:

| Field | Type | Default / constraint |
| --- | --- | --- |
| `inject_time` | number | Required, positive Unix seconds; at most one hour into the future |
| `normal_seconds` | integer | 300; 60–3600 |
| `anomaly_seconds` | integer | 120; 60–1800 |
| `method` | string | `baro`; `baro` or `circa`, enabled by saved thresholds |

Each side of a query window needs at least ten source-resolution intervals.
Omitted durations grow automatically to meet that minimum; explicitly short
durations return 422. Jobs wait until their analysis window ends plus 15 seconds
for scrape lag. HTTP 202 means accepted, not completed. The queue defaults to 100
pending jobs (`CORCA_MAX_PENDING`); full queues return 503 with `Retry-After: 30`.
Do not blindly resubmit manual jobs on a client timeout: there is no caller-provided
idempotency key. Inspect recent history before retrying.

```sh
INCIDENT_START=$(python3 -c 'import time; print(int(time.time()) - 600)')
curl --fail-with-body -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
  "$BASE_URL/v1/diagnoses" \
  --data "{\"inject_time\":$INCIDENT_START,\"method\":\"baro\",\"normal_seconds\":300,\"anomaly_seconds\":180}"
# Poll the id returned above:
curl --fail-with-body -sS "${AUTH[@]}" "$BASE_URL/v1/diagnoses/<job_id>"
```

Job responses contain `id`, `status`, `method`, `inject_time`, `window` (start/end
Unix seconds), `created_at`, `attempts`, `error`, and `result`. Lifecycle states
are `queued`, `running`, `done`, `failed`, and `cancelled`. Interrupted running
jobs are requeued after startup acquires exclusive workspace worker ownership.
Opening the database does not reset active jobs; a second application instance
against that database fails startup. Transient telemetry requests can retry up to three
attempts. Running jobs cannot be cancelled.

`result.status` describes the finding separately: `diagnosed`,
`no_significant_anomaly`, `insufficient_evidence`, `partial_unexplained`, or
`partial_timeout`. Inspect missing/dropped metrics, unexplained services, and
threshold provenance before acting. Relative scores and score ratios are not
probabilities. Result fields vary for early abstention or insufficient evidence.

For `/diagnose`, supply unique `columns` including `time`, matching `rows`, and
`inject_time`. Use `service_metric` column names; 20–2500 rows, 2–200 columns,
strictly increasing timestamps, and at least ten samples on each side are required.
Missing metric values may be null. Optional fields include `method`, `edges`,
`tau`, and `anomaly_threshold`; tau or server calibration is required. This route
has bounded synchronous capacity and does not fetch provider data.

### Alertmanager

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `GET` | `/v1/alerts` | user | Inbox, counts and receiver readiness; `status=all\|firing\|resolved`, optional `q` up to 128 characters |
| `POST` | `/v1/alerts` | service token with accounts; shared admin otherwise | Standard webhook body; records episodes and queues ready firing investigations |
| `POST` | `/v1/alerts/test` | admin | Local receiver/storage check; does not queue a diagnosis or verify external delivery |

Webhook fields are `status`, `groupKey`, and `alerts` (up to 1000). Each alert has
`status`, timezone-aware `startsAt`, optional `endsAt`, `fingerprint`, `labels`,
and `annotations`. Naive timestamps are treated as UTC. Additional upstream
webhook fields are ignored. Use `send_resolved: true` in Alertmanager routing.
See [Alertmanager setup](../guides/operations.md#alertmanager).

Delivery deduplicates episodes and associated jobs. Overlapping firing windows
can share a job; separate temporal groups can return a `jobs` array alongside the
single-job fields. A resolved-only notification returns
`{"accepted":false,"reason":"no firing alerts"}` after recording state. A 503
does not necessarily mean nothing was stored: inbox updates can precede a diagnosis
readiness or queue failure. Retry delivery rather than inventing a new fingerprint.
CoRCA does not expose rule editing, routing, silence management, or a public event
stream; poll the inbox and keep those controls in Alertmanager.

### Log analysis

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `POST` | `/v1/logs/analyze` | user | Raw UTF-8 bytes; required `filename`, optional `service` and `incident_start` query parameters |

```sh
curl --fail-with-body -sS "${AUTH[@]}" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @incident.jsonl \
  "$BASE_URL/v1/logs/analyze?filename=incident.jsonl" > report.json
```

This is **not multipart/form-data**. Supported extensions: `.log`, `.txt`,
`.jsonl`, `.ndjson`. Limits: 2 MiB (2,097,152 bytes), 50,000 lines, and 16,384
characters per line. JSONL uses one object per line. Timestamps without an offset
assume UTC; supported numeric timestamps use Unix seconds/milliseconds.
No monitoring connection is required. The file and report are processed in memory
and not retained by the server; save the response yourself.

The report includes `kind: "log_observations"`, `filename`, entries/lines/errors/
warnings/timestamped counts, time `range`, `notes`, `services`, `patterns`,
`timeline_interval_seconds`, `timeline`, and optional incident `comparison`.
Untimed entries do not become invented chart buckets. Patterns and counts are
observations, not causal root-cause conclusions. Comparisons count available
entries rather than equal-duration error rates.

### Accounts and audit

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `GET` | `/v1/access/session` | public | Whether individual access is enabled, current cookie user, session duration |
| `POST` | `/v1/access/login` | public | `LoginRequest`; returns user and sets cookie; 409 if accounts are disabled |
| `POST` | `/v1/access/logout` | user | Revokes the supplied browser session and deletes cookie |
| `GET` | `/v1/access/users` | account admin | Lists accounts |
| `POST` | `/v1/access/users` | account admin | `UserCreate`; HTTP 201; role defaults to responder |
| `PATCH` | `/v1/access/users/{identifier}` | account admin | `UserAccess`: role and enabled; protects the last administrator |
| `POST` | `/v1/access/users/{identifier}/password` | self or account admin | `PasswordChange`; self changes require current password; resets revoke sessions |
| `GET` | `/v1/access/audit` | account admin | `before` positive event ID, `limit` 1–200 (default 100); events and `next_before` |

Usernames contain 3–64 lowercase letters, digits, dots, underscores, or hyphens
and start with a letter/digit. New passwords contain 15–256 characters. Responders
can investigate and inspect data but cannot save setup or administer accounts.
These are workspace roles, not tenant isolation.

### Published evaluations and help

| Method | Path | Access | Request and behavior |
| --- | --- | --- | --- |
| `GET` | `/v1/diagnosis-evaluations` | user | Confirmed-outcome cohorts, sample counts, detection confusion counts, recent reviews, and unconfirmed diagnoses; optional positive `before` cursor pages the unconfirmed queue |
| `GET` | `/v1/diagnoses/{job_id}/review` | user | Current outcome review or `review: null`; 404 for an unknown diagnosis |
| `GET` | `/v1/diagnoses/{job_id}/evidence` | user | Saved chart preview, dependency edges, and linked alert metadata; `available: false` for legacy diagnoses without captured samples |
| `POST` | `/v1/diagnoses/{job_id}/review` | user | `OutcomeReview`; save or revise a finished/failed diagnosis outcome; stale revisions or unfinished jobs return 409 |
| `GET` | `/v1/evaluations` | user | `kind=diagnosis\|help\|release\|all` (default diagnosis), optional positive `before` ID; runs and `next_before` |
| `POST` | `/v1/evaluations` | admin or publisher token | `Evaluation`; HTTP 201 with ID and duplicate flag |
| `GET` | `/v1/help` | user | Help metadata and available bundled topics |
| `POST` | `/v1/help/ask` | user | `HelpQuestion`; guide or optional AI answer with sources and mode |

Outcome reviews require `revision` (0 for a first review), `outcome`
(`fault`, `healthy`, or `unconfirmed`), `root_causes` (unique exact service names;
required for a fault and empty otherwise), `evidence_kind` (`real_incident`,
`controlled_drill`, or `synthetic`), and `evidence` (10–1000 characters describing
independent confirmation; no secrets). Responses include reviewer, timestamp, and
incremented revision. Corrections and withdrawals are audited atomically. A 409
requires reading the current review before resubmitting. Responders and
administrators can review; alert and evaluation publisher tokens cannot.

Workspace evaluation metrics are derived from saved predictions and reviews,
not from pipeline reports. Cohorts separate methods, saved configurations,
analysis-window lengths, and evidence sources. Unconfirmed cases are unscored.
Matching incident times within a cohort count once, using the latest confirmed
review. Failed/insufficient-evidence cases count as unavailable and score zero
for fault cause accuracy. Detection recall and healthy false-alarm rates exclude
unavailable predictions; zero-denominator metrics are null. The summary returns
up to 20 recent reviews and 20 unconfirmed jobs per page; pass `next_before` as
`before` for earlier unconfirmed jobs. Full review records remain accessible by
diagnosis ID. Saved configurations are hashed, never returned with credentials.

Evaluation summaries include a unique `run_key`, `kind`, `name`, timezone-aware
`completed_at`, `evidence_kind`, `scope`, `case_count`, and nonempty `limitations`.
Optional metrics, gates, source revision, and HTTPS pipeline URL are validated.
Diagnosis reports may also include `dataset_id` and `cases` (up to 1000).
When cases are included, supply every declared case with a unique `case_id`,
`fault_type`, `outcome` (`fault` or `healthy`), `truth` (confirmed service names),
`predicted` (ordered candidate names), and diagnosis `status`. Optional `seconds`
is measured time to result, including queue time; omit it when unknown. Optional
`diagnosis_id` must be a local 32-character hexadecimal job ID. Fault cases need
nonempty truth; healthy cases require empty truth. Exact case labels, dataset ID,
scope, and evidence source must match before the console offers a comparison.
Per-case charts derive their values from these cases. When supplied alongside
cases, complete cause match, cause-set F1, healthy false alarms, and unavailable
rate must agree with case evidence and denominators (absolute tolerance 0.000001).
Conflicting measurements return 422. Partial results with no candidates are
unavailable; failed results cannot contribute a successful cause match or F1.
Gate statuses remain publisher-declared and are identified separately.
Adding these optional fields preserves idempotency for older summary payloads.
Rates use 0–1 values; metrics with zero samples must have a null value. Published
summaries are immutable: resending identical content for a run key is a duplicate;
conflicting content returns 409. Each page contains at most 20 runs; pass
`next_before` back as `before`. Pagination follows publication IDs, while dashboard
chronology follows recorded completion times. CoRCA displays supplied evidence rather than
executing or independently verifying CI pipelines. See
[pipeline publication](../guides/operations.md#evaluation-results-and-pipeline-publication).

Help example:

```sh
curl --fail-with-body -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
  "$BASE_URL/v1/help/ask" \
  --data '{"question":"How do I connect monitoring?","view":"integrations","history":[]}'
```

Questions contain 1–2000 characters and cannot be blank. History accepts up to six
user/assistant turns, 6000 characters each and 12,000 total. The schema lists
valid internal view names. Answers use guide mode unless optional server-side AI
is configured; provider failures can fall back to guides. Help receives basic
setup context, not the selected diagnosis contents, and does not change settings.

## Errors, limits, and retries

HTTP errors normally contain `{"detail": ...}`. Validation detail can be a list;
credential/account/help/evaluation validation uses a redacted message. Do not
depend on the exact English error text or log credential-bearing request bodies.

| Status | Meaning / client action |
| --- | --- |
| 400 | Invalid transport condition, such as remote HTTP login |
| 401 | Missing/invalid credentials or expired session; authenticate again |
| 403 | Role/token scope denied, incorrect current password, or cross-origin mutation |
| 404 | Unknown account or diagnosis ID |
| 408 | POST body not received within the request-body deadline |
| 409 | Stale revision, disabled accounts, conflicting publication, or invalid lifecycle action |
| 413 | POST body over 4 MiB or log upload over 2 MiB |
| 422 | Invalid fields, unavailable method/threshold, inadequate window, or provider/setup validation failure |
| 429 | Login/help throttling; respect `Retry-After` when supplied |
| 502 | Saved-source discovery or verification failed |
| 503 | Busy/full queue, missing diagnosis prerequisites, unavailable storage, or ranker/provider failure |

POST request bodies have a ten-second receive deadline and a 4 MiB cap; log
analysis has its smaller cap. Diagnose/log/discovery/check operations share
bounded capacity. Respect `Retry-After` when supplied and back off on temporary
failures. Authentication, authorization, and validation failures require fixing
the request rather than repeated retries. Return values do not establish that a
provider is working continuously or that a candidate is the confirmed root cause.

## Maintaining the reference

After changing backend routes or Pydantic request models, regenerate the schema
from the checkout without starting a server or opening a workspace database:

```sh
uv run --no-sync python - <<'PY'
import json
from pathlib import Path
from backend.api import create_app
Path("docs/api/openapi.json").write_text(
    json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
)
PY
uv run --no-sync python -m pytest -q tests/backend/test_api_docs.py
```

Update this guide for authorization, response semantics, examples, and behavioral
validators that OpenAPI cannot express. The snapshot describes this source
revision; use the running deployment's protected schema when integrating with a
different release. Static docs never include customer configuration or secrets.

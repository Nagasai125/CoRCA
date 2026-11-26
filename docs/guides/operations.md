# Operate the CoRCA beta

## Deployment contract

One instance, one persistent workspace, one trusted team. Use the root
`compose.yaml` for a normal beta installation. The Kubernetes manifests and
`lab.deploy` are for the dedicated research lab; they are not needed for users.
The default port is loopback-only. To share with teammates, place the service
behind your organization's private access path or HTTPS gateway.

`GET /healthz` checks that the server responds. `GET /readyz` checks workspace
storage and whether the diagnosis worker is alive. Storage failures or a stopped
worker return HTTP 503. It succeeds before telemetry setup so onboarding remains
reachable through a load balancer. Diagnosis availability is separate: `/v1/config`
lists enabled methods and whether a telemetry source is connected.

Each persistent workspace allows one application worker owner. A second instance
using the same database fails startup before recovering jobs. Run one application
process per workspace; do not add Uvicorn workers or replicas against the same file.
Opening storage for inspection does not requeue active work. Interrupted running
jobs are recovered only after the application acquires exclusive worker ownership.
The private `<database>.worker.lock` file remains after shutdown; its presence
does not mean a worker is running. Do not delete it while the application is active.
Shutdown retains ownership until active work finishes; a forced process exit
releases the operating-system lock and the next startup recovers interrupted jobs.

`CORCA_MAX_PENDING` (default 100) bounds queued plus running jobs. At capacity,
new requests receive HTTP 503 with Retry-After. Duplicate alert deliveries reuse
their existing job even when the queue is full. Queued jobs can be cancelled;
running analysis is bounded by per-round timeouts and cannot be cancelled in this beta.
Analysis uses at most three rounds. Transient telemetry failures retry up to three
attempts, 30 seconds apart. An interrupted running job is requeued on restart.

The HTTP server permits 64 concurrent connections, including idle browser
keep-alives. Expensive diagnosis, log parsing, and telemetry operations share
separate bounded admission (two slots by default). A busy analysis response is
temporary capacity backpressure; retry after active work finishes. Increasing
HTTP connection capacity does not increase analysis parallelism.

## Individual access

1. Keep the loopback/private access path while setting up accounts. If using
   Alertmanager, configure its separate service token before enabling accounts.
2. Run the first-administrator command from the deployment host:

   ```sh
   docker compose exec corca python -m backend.access bootstrap-admin \
     --db /var/lib/corca/corca.db --username your.name
   ```

   Enter a password of 15–256 characters at the private terminal prompts. There is
   no default password or public registration endpoint. This command enables
   sign-in immediately and refuses to bootstrap an existing account workspace.
   It does not restart the application or requeue running work.
3. Reload `/app`, sign in, and open **Settings → Team & access**. Add named people with the
   responder role unless they need to administer the deployment. Share initial
   passwords privately; each person can change their own password.
4. Set `CORCA_REQUIRE_ACCOUNTS=1` in `.env` and run `docker compose up -d`.
   An account-required deployment refuses to start with an old/empty database
   that has no accounts. Keep this setting on restores and rollbacks.
5. Remote sign-in requires HTTPS. The reverse proxy must preserve the public Host
   and provide the correct HTTPS scheme through a trusted proxy configuration.
   Set Uvicorn's `FORWARDED_ALLOW_IPS` to trusted proxy addresses only; do not expose
   the backend or trust arbitrary forwarded headers. Loopback HTTP is supported
   for local development.

| Capability | Responder | Administrator |
| --- | --- | --- |
| Read shared diagnoses, alerts, service coverage, and integration metadata | Yes | Yes |
| Run/cancel investigations, analyze logs, verify metrics, discover services | Yes | Yes |
| Change connection, service scope/dependencies, sensitivity, calibration | No | Yes |
| Create people, change roles, disable access, reset others' passwords | No | Yes |
| View account/configuration activity | No | Yes |
| Change own password using current password | Yes | Yes |

Sessions use random server-side tokens, with only their SHA-256 hashes stored in
SQLite. Cookies are HttpOnly, SameSite=Strict, and Secure over HTTPS. Sessions
expire after eight hours. Sign-out revokes the session; password changes/resets,
role changes, and disabling an account revoke all its sessions. New permissions
are checked on each request. Already accepted jobs continue under their saved
configuration; revocation does not undo completed requests or cancel jobs.

Passwords use salted PBKDF2-HMAC-SHA256 with 600,000 iterations. Login attempts
are limited per username (10 failed attempts) and client address (60 attempts)
per 15-minute window. An administrator cannot disable/demote their own account;
another administrator must do so, and one enabled administrator must remain.
Local accounts provide neither SSO nor MFA. This is one shared workspace, without
tenant isolation or separate per-user incident histories.

Configuration updates and their audit entries commit together. Audit records
include the named actor, time, action, configuration revision summaries, and
sensitivity changes; account administration and session activity are also recorded.
Raw queries, provider URLs, credentials, passwords, session tokens, and uploaded
log contents are excluded. Anonymous legacy mode is explicitly attributed to an
unidentified local user or shared key, never to a person. The activity log is
append-only through the API, but a database/host administrator can alter it; it is
not a tamper-proof external compliance log. It currently has no automatic retention.

To recover a forgotten password, run locally:

```sh
docker compose exec corca python -m backend.access reset-password \
  --db /var/lib/corca/corca.db --username your.name
```

The command prompts for a new password, revokes sessions, and records a local
operator action. Protect host access and backups: the database now contains password
hashes, session hashes, user identities, and activity. The restore CLI removes all
restored sessions and records the restore. Reconcile enabled accounts and role
changes against the backup date before reopening access. Old backups without
accounts must be bootstrapped locally, with the service kept private; retain
`CORCA_REQUIRE_ACCOUNTS=1` to prevent accidental anonymous startup.

## Credentials and telemetry

A fresh local workspace opens directly. Without accounts, anyone who can reach
it can view history and change settings; this mode is for a private local pilot.
Enable individual access before sharing the deployment.

For optional server/API authentication, set `CORCA_API_TOKEN` (at least 16
characters) or `CORCA_TOKEN_FILE` in a private deployment override. Without local
accounts this remains the legacy shared-key access mechanism. Once local accounts
exist, the same token is restricted to `POST /v1/alerts` for Alertmanager, and
cannot read history or change settings. Browser users sign in with their own
accounts even when a gateway injects a shared key. Rotate the service token by
updating its secret and restarting the service. Do not use a person's password as
the service token.

For private/custom endpoints set `CORCA_ALLOWED_SOURCE_HOSTS` to comma-separated
hostnames in `.env` and recreate the service. Do not include schemes or ports.
The source must be reachable from inside the container. `localhost` there means
the CoRCA container, not your monitoring server.

The normal connection form accepts provider API keys/tokens directly and verifies
metrics before saving. These secrets live in `/var/lib/corca/credentials/` in the
persistent volume, in 0600 files inside a 0700 directory. They are not encrypted
by the application; protect the host/storage and use HTTPS for remote access.
Only opaque references enter SQLite, metadata, or exports. Old files are retained
for queued jobs; replacing a connection does not invalidate its queued snapshots.

To provide a vendor secret, create a private Compose override outside version
control, for example `compose.secrets.yaml` (restrict it to its owner):

```yaml
services:
  corca:
    environment:
      CORCA_TELEMETRY_TOKEN: ${CORCA_TELEMETRY_TOKEN:?Set telemetry token}
```

Export the secret in your shell or secret manager and start with
`docker compose -f compose.yaml -f compose.secrets.yaml up -d`. Use the same
Compose files for subsequent lifecycle commands. Enter only the variable name
`CORCA_TELEMETRY_TOKEN` in the UI. Provider-specific keys use the same pattern;
see [telemetry setup](telemetry.md). Secret values are not included in the workspace database backup.
For directly saved credentials, back up the credentials directory separately or
reconnect the provider after restoration. Keep override files and `.env` private.

## Sensitivity and findings

After saving service selection and dependencies, set a manual BARO threshold on
Services. The suggested 6 is a starting heuristic, not an accuracy claim. Higher
values stop the search earlier. Manual mode also abstains on windows with no
metric anomaly above the threshold. It does not require a benchmark dataset.

Alternatively load an existing compatible calibration artifact; the administrator
must verify its environmental match. Changing source or dependencies clears the
active threshold. Saving manual sensitivity replaces calibration and enables
BARO only. Each queued job retains its original source, threshold, and threshold
mode, even if settings subsequently change. Credential references use current secrets.

`diagnosed` means the stopping rule finished; it does not prove a cause.
`partial_unexplained` and `partial_timeout` need further investigation.
`insufficient_evidence` means required telemetry was unavailable or unusable.
Export JSON from a diagnosis for support; it contains operational service names
and evidence, so review it before sharing externally.

## Alertmanager

Open **Alerts → Connect Alertmanager**. Enter the CoRCA address reachable from your
Alertmanager, copy the receiver, and merge it into your existing configuration.
Route the chosen alerts to receiver `corca`. Resolved notifications must be enabled
(`send_resolved: true`) so the inbox can update firing episodes to resolved.
The generated configuration follows the [official webhook receiver format](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config).

If optional API authentication is enabled, the generated receiver references
`/etc/alertmanager/corca-token`. Mount your deployment token at that path in
Alertmanager and satisfy any access-gateway requirements. The console does not
expose the secret or deploy/change your Alertmanager configuration for you.

The console polls every five seconds while visible. Event state is the last
reported webhook state, not an independent check of the sender or service health.
The inbox persists in SQLite; filters/search operate across retained events,
with at most 200 matches displayed. Only name, service/target, severity, summary,
reported state/times, receipt counters, and diagnosis linkage are retained; full
annotations and arbitrary webhook URLs are not retained. Treat alert summaries
as operational data and apply your workspace/backup retention policy.

**Test CoRCA receiver** (under Connection settings → Troubleshooting and advanced details) adds a clearly marked test receipt without starting a diagnosis.
It does not verify the external address, routing, credentials in Alertmanager, or
network path. Send a real firing alert from Alertmanager and verify it appears
with its diagnosis; a repeat delivery reuses the job. A resolved update retains
that link and does not cancel historical analysis. A delayed firing retry cannot
reopen the same resolved episode. A later recurrence with a new start time is a
new event.

The receiver records external receipts even before diagnosis setup is complete
or when the queue is full. Those requests still return the appropriate setup or
capacity error so the sender can retry; the inbox explains why no diagnosis was
created. Complete source/service/sensitivity setup or free queue capacity, then
let Alertmanager retry. Resolved receipts do not require a telemetry connection.

API: `GET /v1/alerts?status=all|firing|resolved&q=...`,
`POST /v1/alerts` for Alertmanager webhooks, and `POST /v1/alerts/test` for the local
receiver test. All use the deployment's configured access controls. Alert rules,
silences, routing administration, and CI job execution remain outside CoRCA.

## Backup and restore

Create a consistent online SQLite snapshot and copy it off the container:

```sh
docker compose exec corca python -m backend.admin backup --out /tmp/workspace.db
mkdir -p backups
# Read through exec so this also works when /tmp is a tmpfs mount.
umask 077
set -C
docker compose exec -T corca cat /tmp/workspace.db > backups/workspace.db
docker compose exec corca rm /tmp/workspace.db
chmod 600 backups/workspace.db
```

Use a new filename for each backup. The command refuses to overwrite files and
checks database integrity. Backup contains connection references, settings, and
job history. Back up any optional API credential and telemetry secrets separately using your
secret manager. Directly saved credentials require the matching private credential
files beside the restored database, or a fresh provider connection. Restrict backup access and apply your team's retention policy.

Restore into a new file with the application stopped. This preserves the current
database for rollback rather than overwriting it:

```sh
docker compose stop corca
docker compose run --rm --no-deps -v "$PWD/backups:/backups:ro" corca \
  python -m backend.admin restore --backup /backups/workspace.db --db /var/lib/corca/restored.db
```

Set `CORCA_DB: /var/lib/corca/restored.db` in a Compose override, then start with
that override. Verify setup, job history, and a diagnosis before retiring the old
database. Restored queued/running jobs resume, so check pending incident times
and telemetry retention. Restore refuses an existing target. Practice this on a
separate workspace before depending on it for recovery.

## Upgrade and rollback

Back up the workspace and record the current Git commit/image ID. Stop the old
instance before starting another against the volume. Check out the desired
release, build, and run `docker compose up -d --build`. Verify `/readyz`, direct workspace access,
settings, history, and a diagnosis. Keep one replica. For rollback, use the old
image/source with the pre-upgrade database backup if schema compatibility is
uncertain. Never run two API instances against the same workspace volume.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Workspace access denied | If API authentication is enabled, open the configured access gateway URL and check its injected credentials. |
| Cannot connect telemetry | Check hostname allowlist, container network access, server credential variables, region, and endpoint. |
| No services | Verify query results and grouping label in the provider; use Verify metrics and Discover services. |
| Diagnosis disabled | Save services/dependencies, then set sensitivity or load compatible calibration. |
| Waiting for data | Wait until incident window end plus 15 seconds of scrape lag. Vendor metrics need at least ten minutes per side. |
| Queue full | Wait for active jobs or cancel queued ones. Review repeated alert groups before increasing the cap. |
| Weak/partial evidence | Check incident start, metric coverage, dependencies, and sensitivity; compare with dashboards/logs. |
| Failed job | Inspect `docker compose logs --tail=100 corca`; export the job ID and sanitized configuration for support. |
| History missing after restart | Confirm the same Compose project/volume and CORCA_DB path are in use. Do not delete volumes. |

History is retained until the workspace is removed; automatic age-based deletion
is not provided. Choose volume capacity and backup retention for your team.

## Workspace navigation

Diagnoses and each Settings tab have separate shareable URLs under `/app`, including `/app?view=team` for Team & access.
Navigation and browser Back/Forward do not depend on telemetry setup. Diagnoses
shows missing prerequisites in place; its setup button navigates only when chosen.
Services restores saved service scope and dependencies after reload, without
querying the provider. Saved rows are labeled as not checked until you refresh
coverage with Discover services. Discovery can find new services outside the
current selection and preserves the previously saved selection.


## Customer help bot

**Ask CoRCA** opens the Help screen. The bundled guide knowledge is versioned with
the application in `frontend/help-topics.json`. It covers onboarding, providers,
connection failures, service scope, sensitivity, diagnosis states/results, alerts,
logs, accounts, recovery, and current limitations. Update guides whenever product
behavior changes. Review the `tests/backend/test_helpbot.py` question set for regressions.

Default **Guide answers** mode uses local keyword matching and guide excerpts. It
is explicitly labelled and is not generative AI. Short follow-ups can inherit the
last user question. Unknown questions receive a clear no-match response. This is
product help, not incident analysis or a general-purpose assistant.

Provider choice is still pending for this deployment. The implemented optional
connector supports OpenAI Responses with structured output. To enable it, select
an account-accessible model supporting Responses and strict JSON-schema output,
provide its API key on the server, and explicitly set:

- `CORCA_HELP_AI=1`
- `CORCA_HELP_MODEL` to the selected model ID (there is no implicit model choice)
- `CORCA_HELP_API_KEY_FILE` to a mounted, owner-readable secret file, or provide
  `OPENAI_API_KEY` through the deployment's secret mechanism

A private Compose override can mount the secret without putting its value in YAML:

```yaml
services:
  corca:
    environment:
      CORCA_HELP_AI: "1"
      CORCA_HELP_MODEL: "${CORCA_HELP_MODEL:?Select a compatible model}"
      CORCA_HELP_API_KEY_FILE: /run/secrets/corca-help-key
    secrets:
      - corca-help-key
secrets:
  corca-help-key:
    file: ./secrets/corca-help-key
```

Use a private directory outside version control, or an existing secret manager;
keep the file readable by the container's service identity and inaccessible to
other host users. Restart the service with your override. Verify Help says
**AI answers · OpenAI**, send representative questions, and confirm source links
and unsupported-feature answers before customer use. No real model was exercised
in the local implementation acceptance because no provider/key/model is configured.
The OpenAI HTTP contract is tested with controlled responses. Other company AI
providers are not implemented and require their own connector/acceptance.

AI requests include the question, at most six recent chat messages (12,000 total
history characters), bundled product guides, and basic setup status: screen, role,
account-mode flag, provider type, service count, and enabled methods. Source URLs,
raw queries, credentials, actual service names, log files, alerts, and diagnosis
contents are not automatically sent. Users can still type sensitive text; the UI
warns against this. Provider-side data controls still apply. `store: false` is sent;
it is not a promise of zero provider retention. See the official
[Responses conversation-state guide](https://developers.openai.com/api/docs/guides/conversation-state)
and [structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs).

There are no tools, command execution, automatic setting changes, external
retrieval, or AI-requested network destinations. All navigation/source links are
mapped to bundled guide IDs; model text is rendered as text. Source-ID validation
does not prove factual correctness; AI answers still need review. Questions and
answers are not written to the database, activity log, or browser storage. Page
memory retains the recent chat until Clear chat, reload, or session loss.

Both help endpoints use the normal account authorization. Responders can ask
questions; the Alertmanager service token cannot. Requests are limited to 2,000
question characters, two simultaneous AI calls, 20 help requests per actor per
ten minutes, and 120 per instance per ten minutes. Limits reset on process restart;
this is a single-replica beta guard, not a persistent billing quota. Provider
requests use connection/read timeouts, a checked 35-second transfer deadline,
a 1 MB response cap, 1,600 output tokens, no redirects, and no automatic retries.
An AI error/refusal/incomplete or invalid response returns a labelled relevant
guide answer; a rate/capacity failure keeps the draft for retry. Monitor provider
spend and configure its account-level budget before enabling AI for a shared team.


## Evaluation results and pipeline publication

The diagnosis dashboard shows top-1/top-3 all-cause hit rates, missed causes,
recorded median/p90 time to result, cause-rank distributions, case history, and
fault-type breakdowns. A hit requires **every** confirmed cause within the first
k predicted candidates. Failed/unavailable fault cases remain misses. Healthy
cases appear in the separate false-alarm and detection measurements. Wilson
intervals assume independent cases; these intervals do not correct sampling bias.

Publish diagnosis reports with `dataset_id` and complete per-case results to
compare repeated runs. Only runs with matching dataset, exact truth labels,
scope, and evidence source are comparable. Workspace reviews supply one current
cohort snapshot, rather than inventing historical benchmark runs. Timings absent
from old jobs remain unmeasured. Case grids page at 50 incidents; the workspace
per-case preview contains at most 1000 recent reviewed windows per cohort, while
aggregate outcome metrics use all reviews. Pipeline reports accept up to 1000
cases each. Gates use explicitly published criteria, with no default pass claim.
Target markers use check names that explicitly declare `Top-1 hit rate at least
X%`, `Top-3 hit rate at least X%`, or `Median time to result at most X s`
(also accepting `>=`/`≥` and `<=`/`≤`). Other formats retain their published
criteria without an assumed numeric marker. Published gate statuses remain
publisher-attributed; displaying a marker does not independently verify them.

Finished connected diagnoses now save a metric preview and their dependency
snapshot. In **Diagnoses**, use **Summary** for the graph, aligned evidence,
checklist, editable status draft, and candidate review. **Service map** follows
configured calls with a trace and selected-service inspector. Arrows represent
dependencies, not demonstrated causality. The map does not infer that a service
is normal merely because it was not ranked. Checking a next step saves local
browser progress only; copying a draft never sends a message.
Outcome review opens in a dialog from **Confirm**, **Rule out**, or **Review
outcome**. These controls do not save a label automatically: record independent
evidence and the actual cause set before saving. The Summary shows the first five
prioritized signals, with additional signals available below. **Review incidents**
opens outcome and detection tools beneath Evaluations; pipeline publication is
available from **Pipeline tools** in the dashboard header, under
**Evaluation history → Publish pipeline results**. Evaluation history opens by
default and lists dated reports with sample counts and declared check status.
Expand a report for exact values and provenance; use **Load earlier runs** to
retrieve older pages. Reports with per-case evidence also appear in **Evaluation
run** above the charts. Aggregate-only reports remain in history.
**Detection metrics** opens cause-set F1, detection recall, false alarms, and
unavailable counts directly. Loading earlier reports keeps the selected run and
incident; older chart history is anchored at that run. Default run order follows
completion time, so importing an old report does not make it the latest run.
Recorded completion timings include failed attempts when supplied; inspect
unavailable counts alongside speed measurements.

Evidence is fetched once separately from the polled diagnosis list. Each service
has one metric preview, sampled to at most 120 points; missing intervals are
recorded separately (up to 100 intervals per service, with a truncation flag).
The baseline band is the pre-incident interquartile range. Charts are previews,
not full-resolution metric exports. Legacy jobs retain their saved configuration
map but cannot display metric samples that were never captured. New results
record processing/query/analysis/preview durations and time to result, including
queue time; no latency is reconstructed from an old job's age.

Open **Evaluations → Diagnosis quality** to review completed or failed diagnoses.
Choose **Review outcome**, record a confirmed fault and its exact cause service
names, or confirm a healthy window. Select the evidence source and explain the
independent confirmation (postmortem, verified fix, or controlled drill record).
A diagnosis's own ranking does not establish ground truth. The same review action
is available within each finished diagnosis. Do not label a quiet window healthy
without checking it independently.

Saving a review updates workspace accuracy charts: complete cause match,
cause-set F1, anomaly detection recall, healthy false alarms, and unavailable
results. Sample counts and a detection confusion table accompany the charts.
Detection here means a diagnosis named at least one cause; it is not a separate
per-metric anomaly detector. Failed and insufficient-evidence results stay in
the unavailable count and contribute zero to fault cause match/F1. Incomplete
results with no candidates are also unavailable, rather than quiet windows. Detection
recall and false alarms use only available predictions; an empty denominator is
**Not measured**, never an assumed zero.

Cohorts separate method, saved configuration, analysis-window lengths, and
synthetic/drill/real evidence. Missing legacy configurations remain separate.
Repeated incident times within a cohort count once using the latest confirmed
review. Separate timings for the same real incident are not automatically
deduplicated: review independent windows deliberately. Configuration cohorts do
not establish independence, representative sampling, or a frozen software revision.
Unconfirmed windows never affect scores. Use recent reviews or the original
diagnosis to correct a review or choose **Unconfirmed** to withdraw it. Changes
are audited, and concurrent edits require reloading the latest revision. Shared
workspaces cannot attribute reviews to an individual; enable individual access
for named reviewers. Review notes are visible to workspace users, so omit secrets.

Completing telemetry setup or running a diagnosis does not publish a separate
pipeline evaluation report automatically. The published reports section remains
available for externally scored datasets and CI evidence. Software-release evaluations require a
published software-test summary, and AI help evaluations require reviewed answers.
These report types remain accessible through the API and CI artifacts rather than
the customer Evaluations screen.
An empty category means no report has been published, not that diagnosis failed.

Open **Evaluations** to review diagnosis accuracy, confirmed causes, and anomaly
detection. AI help and software-release categories are not part of this customer
view. Existing reports and CI publishing APIs remain available without deleting
evidence. Responders can read diagnosis evidence. Administrators can publish
prepared summaries; a separate service credential can publish only to
`POST /v1/evaluations`. Neither pipeline execution nor automatic Jenkins job
configuration is part of this feature. The console never fetches a supplied link.

Diagnosis evidence should answer customer questions using independently confirmed
incidents and healthy windows. The supported measurements distinguish:

- **Complete cause match:** fraction of fault cases with a complete exact result;
  partial predictions do not count as complete matches.
- **Healthy false alarms:** fraction of evaluable healthy cases with a predicted
  cause. Its denominator excludes unavailable healthy cases; always inspect
  unavailable results as well.
- **Cause-set F1:** mean overlap score between predicted and confirmed cause sets;
  it is not a probability or confidence estimate.
- **Unavailable results:** fraction of cases without evaluable results.

Each metric retains its own denominator. Zero-sample metrics must be null and
appear as Not measured. Synthetic/drill results are labelled and must not be
presented as real customer accuracy. Scope, sample independence, labels, build
revision, and evidence type are publisher declarations, not independently
verified certifications. The app does not supply a default accuracy cutoff or
production approval. Freeze acceptance criteria before collecting held-out cases.
Compare only compatible methods, data, service scope, topology, and thresholds.

Prepare an existing pilot case assessment, without rerunning its evaluations:

```sh
.venv/bin/python -m backend.quality pilot \
  --input results/pilot-m1/assessment-001.json --method baro \
  --out results/pilot-m1/customer-summary-001.json \
  --run-key pilot:baro:001 --name "BARO held-out incident review" \
  --scope "Describe the environment, independent cases, and frozen configuration" \
  --completed-at "2026-11-04T20:00:00Z"
```

Use the real assessment completion time. Add `--revision` with the evaluated
commit and `--pipeline-url` with its final HTTPS run URL when known. Missing
revision is displayed as Not recorded. Only aggregate measurements are retained;
raw incident IDs, ground truth, and diagnosis exports are excluded. The original
report remains in the evaluation owner's evidence store. The summary has no
automatic gate; add reviewed, predeclared gates only if they belong to that run.

Software test pipelines can prepare JUnit summaries instead:

```sh
.venv/bin/python -m pytest -q tests --junitxml=results/python-tests.xml
.venv/bin/python -m backend.quality junit \
  --input results/python-tests.xml --out results/python-quality.json \
  --run-key local:python:001 --name "Python software checks" \
  --scope "Python unit/integration checks only; not model quality or incident accuracy"
```

The converter counts actual test cases, includes skipped cases in the denominator,
and marks the declared software gate failed on failures/errors, incomplete on
skips, and passed only when all supplied tests pass. It strips test names, paths,
logs, and failure output. This report covers only that supplied XML, not every
stage in the broader pipeline. Conversion time is used unless `--completed-at`
is supplied. Both preparation commands refuse to overwrite existing output.

For automatic publication, provision a distinct token of at least 16 characters
through `CORCA_EVALUATION_TOKEN_FILE` or `CORCA_EVALUATION_TOKEN` on the server.
It must differ from the Alertmanager/legacy API key. Use named accounts and HTTPS
before sharing the deployment; legacy anonymous local mode still grants local
administration. The evaluation token cannot read reports or incidents, administer
accounts/settings, use help, or submit alerts. Rotate it by replacing the secret
and restarting the service. Set the matching token in the CI/Jenkins secret
environment, then publish:

```sh
.venv/bin/python -m backend.quality publish \
  --report results/python-quality.json --url https://corca.example.com
```

The CLI reads `CORCA_EVALUATION_TOKEN` from the pipeline environment, never from
a command-line argument. Redirects are disabled and remote publication requires
HTTPS. Authentication failures identify the publisher-token requirement, run-key
conflicts require a new key, and unavailable deployments can be retried with the
same report and key. Retries of identical evidence do not create duplicate runs.
Malformed JUnit XML fails without writing a summary or exposing test contents.
The existing GitHub workflow now prepares a Python-check summary and retains
it as artifact `corca-quality-<attempt>` even if tests fail. To publish from
trusted default-branch pushes, configure repository variable `CORCA_QUALITY_URL`
and secret `CORCA_EVALUATION_TOKEN`, and make the deployment reachable from that
runner. Publication is not enabled on pull requests. The workflow excludes Node
and container-stage results from this Python report. Artifact retention uses the
[official upload-artifact action](https://github.com/actions/upload-artifact).
No GitHub/Jenkins run was triggered or remote pipeline connected during local acceptance.

Alternatively, an administrator can upload a prepared JSON file under Quality →
Publish pipeline results. Summaries are validated, limited in shape/size, and
immutable: the same run key and exact canonical payload is idempotent; different
evidence under an existing run key returns 409. Use a new run key for reruns.
Records and audit attribution commit together. A SHA-256 fingerprint covers the
canonical summary; it is not a digital signature or proof that the publisher's
claims are true. Pipeline URLs must be final HTTPS links without embedded
credentials, query parameters, or fragments; opening them may require separate
pipeline permissions. Do not put secrets or incident identities in summary text.

AI help evaluations use the same summary schema with kind `help`, their own
question set, explicit evidence type, sample counts, and metrics such as correct
source citations, unsupported claims, unavailable answers, and response time.
Contract tests with mocked model responses are software evidence, not measured
live model answer quality. A real model/provider evaluation is still pending.
Run these outside the customer request path; publish their aggregate evidence
for review. JSON schema is available in this deployment's authenticated OpenAPI.
The console stores at most a page of 20 runs per response with earlier-run
pagination; history currently has no automatic deletion or external audit signing.


### Reading evaluation charts

Quality shows horizontal bars on fixed scales: rates use 0–100%, while cause-set
F1 uses 0–1. Each bar retains its sample count and higher/lower-is-better label.
Response time is shown separately in seconds. Missing or zero-sample evidence is
labelled Not measured and is not drawn as a zero score.

The donut shows exact counts of declared pass/fail/not-run checks. These are
acceptance checks, not individual test cases or slices of diagnostic accuracy.
Overlapping measurements and different denominators are never combined into a
pie. Without declared checks, the page explains the missing gate instead of
inventing a chart. Text values and labels are available without hover; View exact
values & sample counts opens the accessible data table. Scope, limitations, and
pipeline provenance remain attached to each run.


## Recovery and grouped-alert hardening

Workspace database access enforces owner-only permissions (0600) before SQLite
opens the file, including existing databases and local account commands. It does
not change parent directory permissions. If the service identity cannot enforce
this mode, startup/access fails instead of using an exposed database. Ensure the
state volume is owned by the configured service identity. The shared guard adds
one file open/chmod per database operation, suitable for this single-replica beta.

The restore CLI closes the restored database and deletes its newly created target
if session invalidation or the restore audit record fails. It does not remove
an existing target when overwrite is refused; the source backup stays intact.

Alertmanager groups can contain episodes beginning at different times. Existing
links are preserved, and unlinked firing episodes are grouped only into diagnosis
windows retaining at least ten aligned samples after their onsets. Distant
starts can therefore produce different diagnoses in the same notification group.
Long configured leads expand the derived window within the Incident input limits:
at most 2,500 rows and 200 columns including time. Oversized configurations are
rejected with an actionable error retained in the alert receipt. Legacy wildcard
sources have unknown column cardinality until retrieval, where final Incident
validation still applies.

A partially full queue retains links already created; sender retries queue only
missing work. Exact retries reuse the original links even after a later source
change. Resolved/stale episodes never reopen or create work. The webhook response
keeps its existing top-level job fields; when multiple jobs are affected, the
latest affected job is primary and a `jobs` array adds only IDs, states, method,
incident times, and windows. Alertmanager itself needs only the HTTP outcome.
Historical links created by the earlier implementation are not remapped
automatically; independently review old results whose windows excluded an episode
and start a fresh manual diagnosis if required.

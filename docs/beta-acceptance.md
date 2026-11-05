# Beta acceptance — October 29, 2026

This release makes the existing diagnosis service usable as a trusted-team
application. New benchmark collection is not part of application setup.

## Verified locally

- 126 tests passed with `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
  runtime/.venv/bin/python -m pytest -q tests runtime`.
- Fresh Compose build started healthy with a non-root user, read-only filesystem,
  persistent workspace volume, and loopback port binding.
- Installed `corca-serve` and `corca-admin` entry points work from outside the
  source directory in the container.
- Browser acceptance against the actual container and a local HTTP Prometheus
  fixture: connection, discovery, selected services/dependencies,
  manual sensitivity, incident submission, completed BARO result, queued-job
  cancellation, and persisted history/settings after a container restart.
- Actual worker retrieved metrics over HTTP and returned cartservice for the
  fixture incident. Manual sensitivity appeared on the result. Automated coverage
  also checks healthy-window abstention, alert deduplication at queue capacity,
  stale setup rejection, invalid sensitivity, and backup/restore recovery.
- The container's backup and restore commands preserved a diagnosis and returned
  `ok` from SQLite integrity checking. Existing backup targets are never overwritten.

The existing Starlette/httpx test-client deprecation warning remains. It does not
affect the live browser/server path.

## Deployment-specific acceptance

Local fixtures establish the software workflow. Each deployment still needs its
own reachable telemetry endpoint, credential variables, matching query labels,
confirmed dependencies, and an actual Alertmanager delivery check if alerts are
used. No customer/vendor account credentials were exercised in this release.
The application does not claim production availability or environment-calibrated
accuracy from a manual threshold.

See [operations](operations.md) for setup, recovery, and troubleshooting.

Direct-access follow-up: the sign-in screen and stored browser keys are removed.
The default deployment opens the workspace without authentication. Optional API
authentication remains covered by existing rejection/acceptance tests; direct
access and cross-origin mutation rejection have dedicated workflow coverage.

Navigation follow-up: diagnosis setup stays on Diagnoses; sidebar links support
deep links and browser history. Saved service scope survives reload/provider
outages, and discovery searches beyond the existing selection.

Four dependency-free Node tests exercise the actual console navigation/form
handlers, including the former missing-setup redirect. Run with
`node --test tests/console-navigation.test.cjs`. Browser checks confirmed separate
Diagnoses/Services/Integrations routes, Back/Forward navigation, saved Services
after reload, and a completed diagnosis in an isolated configured workspace.

## November 4, 2026 end-to-end re-verification

Fresh Linux Python 3.12 environment from `uv.lock` + `runtime/rcaeval.lock`:

- 126 pytest tests and 7 Node tests pass (new `tests/console-html.test.cjs`).
- Research pipeline: `corca-demo`, `scripts.run_eval --limit 3`, `scripts.summarize`
  on `results/re1ob-policy-v1` (reproduces BARO top-1 0.736 / CoRCA F1 0.772, CIRCA
  0.576 / 0.637), `runtime.evaluate` on the held-out 2-fault incident with frozen
  calibration, `scripts.report` and `scripts.plots`.
  `results/policy-v1-heldout` predates the top-k columns; re-run `runtime.evaluate`
  before passing it to `scripts.report`.
- Browser acceptance (`e2e/`, 24 checks, Chromium): connect Prometheus, discover 11
  Online Boutique services, enter 15 dependencies in the UI, manual sensitivity,
  verify metrics, concurrent-fault diagnosis (cartservice CPU + currencyservice
  memory both named), healthy-window abstention, future-time rejection, queued-job
  cancellation, search/status filters, shared link, JSON download, persistence after
  restart, 375px layouts, landing page. Alertmanager webhook queued one diagnosis and
  deduplicated the repeat. `corca-admin` backup/restore and overwrite refusal checked.
- Non-editable package install from the hash-pinned lock served `/`, `/app`, and a
  completed diagnosis from outside the source tree (Docker Hub was unreachable from
  the test sandbox, so the image itself was not rebuilt).

Fixed: the service-label input pattern was invalid under the browser `v` regex flag,
so Chrome ignored that validation and logged an error on every Integrations load.

Observed, not a bug: the fixture's callers (checkoutservice, frontend) also get CPU
bumps. CoRCA keeps callers' own resource metrics as candidates (`keep_resource`), so
it returned checkoutservice as a third root and frontend as unexplained
(`partial_unexplained`). This is the documented caller-resource limitation.

## Console simplification follow-up

The console uses one provider selector and one connection form. Repeated workflow
cards, dashboard counters, adapter tiles, checklists, and section navigation have
been removed. Overview shows recent diagnoses and only the next required setup
action. Optional calibration and technical result metadata remain collapsed;
missing telemetry and unexplained symptoms stay visible. Help contains a brief
workflow and operating limits. The console uses system fonts without external
font downloads.

Verified: 126 Python tests and 8 Node tests pass. Isolated Chromium checks covered
all five provider forms, HTTP fixture connection and 11-service discovery, service
and sensitivity saving, a completed BARO diagnosis, visible warnings and exports,
metrics verification, persistence after reload, and all five routes at 375px in
light and dark appearance. Live vendor accounts were not exercised.

Quick-connect follow-up: Integrations initially shows only Provider and Endpoint.
Authentication opens for providers that require it; optional Prometheus auth,
metric labels/resolution/query examples, and administrator options are disclosed
on demand. Custom metric queries remain supported, and invalid fields open their
containing disclosure before focus. Nine Node checks and isolated Chromium checks
pass, including an HTTP fixture connection/discovery using the untouched default
queries. Defaults remain examples, not automatic mappings for every environment.

## Uploaded log analysis

Logs accepts UTF-8 text and JSON-lines uploads independently of telemetry setup.
The bounded analyzer groups recognized errors and warnings, keeps supporting line
numbers/excerpts, reports service counts and timestamp coverage, and optionally
compares counts around an incident start. It does not infer causal roots or persist
uploads/reports. Input limits and inherited deployment authentication apply.

136 Python and 10 Node checks pass, including log parsing, multiline exceptions,
missing/invalid timestamps, timezone handling, access and upload limits. Isolated
Chromium checks cover text/JSONL upload, optional incident context, evidence and
JSON export, safe rendering of HTML-like log text, binary/oversize rejection, and
375px light/dark layouts without JavaScript errors.

## Controlled-pilot tooling

The operator module `runtime.pilot` adds deployment preflight, per-method assessment
of confirmed fault/healthy diagnosis exports, and an isolated SQLite recovery drill.
Reports retain unavailable cases, distinguish complete from partial exact matches,
label synthetic/drill/real evidence, and never grant production approval.

145 Python tests and 10 Node checks pass. The running local container passed its
storage snapshot/restore drill; the current workspace has zero saved jobs/settings.
Populated recovery preservation is covered separately by automated tests. Local
preflight confirms health/storage but reports missing telemetry, service scope,
dependencies, and sensitivity. Real-incident accuracy, alert delivery, sustained
load/deadlines, application rollback, and owner acceptance remain open.

## Direct provider credentials and customer connection flow

Provider selection now exposes the required API key/token fields directly;
Datadog/New Relic choose a region, Grafana supplies its instance ID, and Prometheus
optionally uses a token or basic authentication. Advanced environment references
and custom metric settings remain available. The browser verifies access/metrics
before replacing a connection and uses the verification response for discovery.
Failed connections preserve prior settings; password fields are cleared on save.

Direct secrets are stored separately from SQLite in private 0600 files inside a
0700 directory on the persistent volume. Source snapshots contain opaque refs;
metadata, JSON exports, validation responses, and logs exclude secret values.
These files are not encrypted by the application and need separate backup/host
protection. OAuth and individual CoRCA identities are not implemented.

153 Python and 11 Node checks pass. Browser checks cover all five provider forms,
region selection, an actual authenticated HTTP metrics fixture, rejected-key
rollback, automatic discovery, safe config downloads, cleared password values,
reloaded saved credentials, environment-reference fallback, and mobile light/dark
layouts. Vendor header contracts are exercised with mocked APIs; real vendor
accounts remain a deployment-specific acceptance step.

Provider-card follow-up: Integrations opens with the five provider cards, matching
the supplied reference. Choosing a card reveals its connection form; connected
state and current selection are distinct. Re-selecting a card preserves its draft.
The duplicate provider dropdown is hidden, credentials fit a compact responsive
grid, and controls are disabled during verification to avoid losing edits.

## Live Alertmanager console

Alerts now provides persisted receipts, firing/resolved filters, server-side
search, last-delivery information, severity, and linked diagnoses. The visible
page polls every five seconds and renders alert content as text. Receiver setup
generates a reachable-address configuration with resolved updates and optional
Bearer credential-file authentication. Local tests are identified separately, do
not queue a diagnosis, and do not count as external delivery.

158 Python tests and 12 Node checks pass. Tests cover persistence, duplicate
notifications, delayed firing retries after resolution, missing setup, capacity
errors, authentication, and search beyond the first 200 records. Browser checks
cover automatic updates, filters/search, linked diagnosis navigation, local tests,
receiver YAML/copy, safe HTML-like labels, reload persistence, and 375px light/dark
layouts.

An actual isolated Alertmanager 0.34.2 container sent an authenticated firing
notification, received a linked completed BARO diagnosis using fixture metrics,
and sent a resolved update retaining the same job link. Image digest and transport
evidence are in the local ignored folder `results/alertmanager-console-acceptance`.
This is controlled transport acceptance; customer routing, networking, telemetry,
and operational acceptance remain deployment-specific. Jenkins integration is
not included; read-only CI/change context is a proposed follow-on.

## Individual access and configuration accountability

Team & access adds named local accounts, administrator/responder permissions,
revocable eight-hour cookie sessions, user administration, password changes and
local recovery. There is no default account, public registration, SSO, or MFA.
Fresh local deployments retain their existing direct-access mode until the first
administrator is bootstrapped on the deployment host. Set
`CORCA_REQUIRE_ACCOUNTS=1` after bootstrap to prevent account-free startup on an
old/empty restored database. No customer account was created during development.

Administrator permission is enforced on connection, topology, sensitivity, and
calibration writes as well as user administration and audit access. Responders
retain investigations, log analysis, and shared history. Once accounts exist,
the legacy deployment token is limited to Alertmanager webhook delivery;
browser sessions cannot be used to impersonate an external webhook sender.
Successful configuration changes and their attributed audit entries commit in
one database transaction. Account changes revoke sessions. The restore CLI clears
restored sessions. Passwords, provider credentials, raw queries, and provider URLs
are excluded from the audit summaries. Host administrators can alter SQLite;
this is not a tamper-proof external audit service.

165 Python and 13 Node checks pass. New security checks exercise denied direct
API calls, service-token scope, cookie flags, remote HTTPS requirements, session
expiry/revocation/restart, role changes, last-administrator protection, password
reset/change, persistent login limits, validation redaction, audit pagination,
transaction rollback, bootstrap without requeuing live work, restore invalidation,
and required-account startup refusal. One existing Starlette/httpx deprecation
warning remains. Customer identity-provider and remote gateway acceptance are
not covered by these local-account tests.

Isolated Chromium checks additionally verified sign-in/rejection, administrator
user creation, responder controls and direct API denial, live-session revocation,
password-form disclosure, mobile light/dark layouts, and no JavaScript errors.
The rebuilt local container is healthy, retains direct local access until owner
bootstrap, and provides the packaged `runtime.access` command. Test accounts
were created only in a temporary workspace, not in the running customer workspace.


## Customer platform help

Ask CoRCA is available from workspace screens and Help. It offers suggested
questions, bounded page-memory chat, product-guide sources, screen navigation,
Clear chat, and plain-text answers. Guide answers use bundled local documentation
and are labelled as non-AI. The optional OpenAI Responses connector is explicitly
enabled by a deployment administrator; it sends guides and basic setup metadata,
not automatically attached incident contents or credentials. Provider choice and
credentials remain pending, so the deployed workspace runs in Guide answers mode.

183 Python and 13 Node checks pass. New tests cover common customer questions,
short follow-ups, unknown questions, safe prompt context, source validation,
refusal/incomplete/invalid responses, provider failures, input/response/concurrency
and rate limits, authentication, responder permissions, service-token rejection,
cross-origin rejection, and question-validation redaction. Isolated Chromium
checks cover source disclosure/navigation, chat clear/reload, 375px light/dark,
controlled AI output, HTML-like answer text, provider fallback, in-flight clear,
and draft preservation on a rate-limit response, with no JavaScript errors.
Real model factual quality, account access, latency, cost, and customer-provider
acceptance are not verified. This feature cannot execute actions or diagnose
incidents through chat.


## Customer evaluation visibility

Quality separates diagnosis evidence, AI help evaluations, and release checks.
It displays publisher-declared scope, evidence type, per-metric sample counts,
limitations, completion date, optional revision/pipeline link, immutable summary
hash, and explicit declared-gate status. No evidence means no score. Administrators
can upload prepared summaries; pipelines use a separate publish-only credential.
Existing pilot assessment and JUnit conversion commands strip incident/test
identities and do not execute evaluations. The GitHub workflow prepares/retains
Python-check evidence and optionally publishes on trusted default-branch pushes
when its deployment URL and scoped secret are configured. No remote pipeline
was connected or triggered as part of implementation.

188 Python and 13 Node checks pass. New coverage checks persistence, idempotent
publication and conflict protection, publisher scope, responder/admin permissions,
cross-origin rejection, validation/URL/secret boundaries, missing samples,
category-specific metrics, declared gates, pagination, audit-transaction rollback,
pilot aggregation, JUnit pass/fail/skip semantics, private file output, no overwrite,
and HTTPS publication. Isolated browser checks verify diagnosis-first filtering,
separate help/software evidence, samples and provenance, safe links, administrator
upload, responder read-only controls, reload, 375px light/dark, and no JS errors.
Synthetic layout reports exist only in the temporary browser-test workspace.
Real customer diagnosis evidence and live-model answer quality remain pending.

The real local 188-test JUnit report was converted and published through the
scoped CLI to an isolated server over HTTP, verifying persisted pipeline-attributed
receipt. Its aggregate release summary was also published to the running local
workspace, explicitly labelled local macOS working-tree Python evidence, not a
remote CI run, customer diagnosis evaluation, or live-model assessment. Diagnosis
and AI-quality categories remain empty. Deployed Chromium checks confirm these
states, scope/sample labels, mobile light/dark, and help-to-Quality navigation.


## Evaluation charts

Quality adds responsive fixed-scale measurement bars and a donut of exact declared
check outcomes. Rates, F1, and seconds retain distinct units. Sample counts and
better-direction labels stay visible; missing evidence produces no zero score or
empty pie. Exact values remain in a disclosure table. The existing local Python
report remains unchanged; charts do not create evaluation evidence.

14 Node checks pass, including a chart semantics check for independent units,
zero/null samples, scales, and check-count proportions. Chromium checks cover the
actual 188-test release summary, controlled mixed-metric/mixed-outcome layouts,
exact-value disclosure, empty evidence, and 375px light/dark without JavaScript
errors. Python/backend behavior is unchanged from the prior 188-test acceptance.

## Pre-push re-verification

Fresh local checks: 188 Python tests and 14 Node checks pass; the dependency lock
is current, Compose configuration is valid, and the deployed app is healthy.
The existing browser acceptance scripts were updated to use real query-string
routes for all nine workspace views and configurable isolated ports. The restart
helper now rejects an occupied application port and unrelated PID files, and
reports startup failure accurately.

An isolated real HTTP metrics/server workflow passed 29 browser checks: connection
and discovery, service/dependency/sensitivity saving, metrics verification,
concurrent-fault leads, healthy abstention, future-time rejection, queued-job
cancellation, search/filters, shared links, JSON export, restart persistence,
all nine views at 375px, and landing navigation. The synthetic concurrent case
also returned an additional suspect and unexplained symptoms, as already documented;
this is not customer-accuracy acceptance. No customer workspace settings changed.

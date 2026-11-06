# Changelog

## Unreleased

Changes since the packaged 0.2.0 beta. No new release or version tag has been published.

### Evaluation reliability and focus

- Evaluations now focuses on diagnosis accuracy, confirmed causes, anomaly
  detection, and published diagnosis history. Removed AI-help and software-release
  tabs; their existing reports and CI publishing APIs remain available.
- Historical charts follow completion time and remain anchored to the selected
  run. Refresh and pagination preserve the run, comparison, incident, and page.
- Added visible refresh and detection-metrics controls. Outcome-review queues
  remain visible after pagination and saving; incomplete timings are disclosed.
- The pilot assessor accepts current evidence bundles and legacy diagnosis
  exports while retaining the hash of the complete source file.
- Empty partial predictions count as unavailable. Summary measurements with
  per-case evidence must agree with their original cases and sample counts.
- Malformed JUnit input fails safely. Publication errors explain token, schema,
  conflict, and retry recovery; duplicate publication remains idempotent.
- CI runs all Node console and chart checks alongside Python, package-boundary,
  container-startup, and isolated-storage-recovery checks.

### Frontend clarity

- Home separates normal activity from items needing attention; completed setup
  stays optional and preserves the reader's expanded state.
- Log results collapse upload controls, retain an obvious file replacement
  action, prioritize the highest-error services, and disclose extra patterns.
  Single-bucket logs show exact counts rather than implying a time trend.
- Help and empty evaluation views remove repeated guidance. Login hides
  workspace actions, hidden search fields no longer capture keyboard shortcuts,
  and responder prerequisites explain which changes need an administrator.
- Team controls use consistent alignment without duplicate self-management
  actions. The landing page now correctly describes individual account support.
- Services now leads with the inventory, aligns dependency/sensitivity controls
  beside it, and keeps the dependency explorer in a closed disclosure.
- Duplicate setup explanations and the redundant missing-metrics column are
  removed; missing metric names remain beside affected service rows.
- Pages share a stable left edge. Diagnosis views summarize gaps and disclose
  affected services; runs without candidates keep checklist and draft optional.
- Removed the non-actionable remediation roadmap footer from diagnosis pages.

### Customer workflow fixes

- Saved evidence retains the full selected service scope, so missing isolated
  services remain visible in coverage counts.
- Explicit anomaly-threshold overrides are labeled manual, even when the
  deployment also has calibration loaded.
- Ordinary HTTP requests allow 64 concurrent connections, including browser
  keep-alives; expensive analysis retains separate bounded admission.
- Cancelled runs do not offer outcome review, and resolved alerts with no end
  time show an unknown duration instead of implying they are still active.
- Evidence downloads include the diagnosis, captured metric/dependency evidence,
  and current review; failed downloads report an error instead of saving a partial bundle.
- Cancelled and incomplete runs no longer claim that no anomaly was detected.
  Views without a candidate describe saved dependencies without implying propagation.
- Alertmanager's empty end-time sentinel no longer displays a zero-minute firing
  duration. Invalid legacy end times are also suppressed.
- Failed metric verification opens the query settings for recovery and preserves
  the existing working connection.

### Reference layout corrections

- Incident pages now follow the supplied Summary and focused Service map layouts,
  with one heading, a four-part summary, compact evidence rows, and modal outcome review.
- Evaluations use one dashboard with aligned metric cards, uncertainty/target marks,
  grouped case changes, a compact incident grid, and the declared timing limit.
  Review and pipeline tools are disclosed below the dashboard.

### Investigation and evaluation workspaces

- Diagnosis Summary and Service map views show saved dependencies, selected-service
  inspection, captured metric replay, local checklists, and editable status drafts.
- New connected diagnoses capture bounded metric previews outside the polled job
  payload, with query/analysis timings and measured time to result.
- Evaluation dashboards add comparable runs, all-cause top-k cards, stacked rank
  histories, paged incident grids, fault-type bars, duration histograms, and gates.
  Optional per-case pipeline evidence preserves older report idempotency.

### Confirmed incident evaluations

- Finished and failed diagnoses can be reviewed against independent incident
  evidence, including confirmed causes, healthy windows, and label withdrawals.
- Diagnosis quality shows cause-match/F1 bars, anomaly recall, false alarms,
  unavailable results, confusion counts, and explicit sample sizes by cohort.
- Reviews persist with reviewer, revision, and audit history; unconfirmed windows
  remain unscored. Pipeline publication remains a separate workflow.

### Evaluation and alert setup clarity

- Evaluation empty states distinguish live diagnosis results from independently
  confirmed outcomes and separately published diagnosis benchmarks.
- Diagnosis evaluation empty states link directly to diagnosis history.
- Alert setup distinguishes a ready receiver from confirmed Alertmanager delivery;
  polling no longer says live alerts before the first external alert arrives.

### Worker reliability

- Opening a workspace no longer resets running jobs. Restart recovery happens
  only after the application acquires exclusive worker ownership; a second
  instance against the same database is rejected.
- Graceful shutdown retains ownership until the worker exits, and repeated
  application lifespans reset the stop event before starting work.
- Readiness returns HTTP 503 for unavailable workspace storage or a stopped
  worker while liveness remains independently available.
- Regression coverage exercises active-job preservation, competing instances,
  restart recovery, worker/storage failures, and shutdown ownership.

### Documentation cleanup

- Added a task-based documentation index and separate current guides, API, and
  research sections. Removed the archive tree and superseded reports.
- Added API authentication/role guidance, all endpoint operations, curl workflows,
  upload limits, job lifecycle, errors, and a generated OpenAPI snapshot.
- Documentation checks now cover schema drift, endpoint coverage, example source
  validation, and repository links.

### November 5, 2026 — repository boundaries

- Explicit `frontend/` and `backend/` directories replace `runtime/web/` and
  the `runtime` server package. Imports, console commands, Docker, Compose, CI,
  assets, and test paths use the new layout; the web/API URLs are unchanged.

- Production wheel and image ship the application, engine, and console assets;
  research scripts and tests are excluded. CI checks both package and image boundaries.
- Moved local deployment, incident submission, evaluation, source example, and
  Kubernetes manifest from `backend/` to `lab/`; moved application tests into
  `tests/backend/` and lab tests into `tests/lab/`.
- Browser regressions live in `e2e/`. Research CLI commands now use
  `python -m scripts.<module>` from the checkout.
- Added `CONTRIBUTING.md` with the repository map, contributor checks, production
  versus research responsibilities, and a command migration table. Existing
  workspace state, datasets, calibration results, and experiment evidence are preserved.

### November 4, 2026 follow-up

- Provider cards, verified direct API credentials, and safe server-side credential storage.
- Uploaded log observations and a persisted, searchable Alertmanager inbox.
- Named local accounts, administrator/responder permissions, revocable sessions,
  and transactional configuration activity.
- Source-linked customer help with local guide answers and optional server-side AI.
- Immutable evaluation summaries, scoped pipeline publication, and accessible
  measurement bars/check-outcome donuts with sample counts and limitations.
- Controlled-pilot preflight, assessment, and recovery tooling; expanded acceptance
  coverage and CI report artifacts.

Current scope remains one shared workspace and one service replica. Individual
access must be enabled by first-administrator bootstrap; SSO, MFA, tenant isolation,
provider OAuth, and automatic remediation are not implemented. Live customer
accuracy, provider/AI configuration, and operational acceptance remain deployment-specific.


### Testing hardening

- Enforce private workspace DB files before SQLite access, including CLI paths.
- Remove incomplete restore targets when session invalidation/audit fails.
- Preserve alert episode links and split distant grouped alerts into covering
  diagnosis windows; keep exact and partial-capacity retries idempotent.
- Add concurrent admission/claim/delivery and administrator-demotion checks,
  storage-failure regressions, and grouped-alert/long-lead boundary coverage.

- Refreshed console with grouped task navigation, mobile context tabs, a shared
  light/dark palette, consistent icons, and restrained motion with keyboard and
  reduced-motion support. Help and setup guidance follow the new navigation.

### November 5, 2026 frontend reference update

- Applied the supplied blue/Material-style theme, bundled local typefaces, grouped
  navigation, Home attention layout, dedicated diagnosis findings, log-count charts,
  diagnosis evaluation history, and native contextual help drawer.
- Buffered alert polling with pause/resume and explicit updates keeps rows stable;
  search/filter responses cannot be overwritten by older requests.

- Added selectable log time buckets and a keyboard/touch time slider, plus a saved
  service-dependency explorer with neighbor traversal and inventory navigation.
- Fixed help suggestions clearing incident time, header search retaining status
  filters, hidden forms on direct diagnosis URLs, and evidence/focus loss during
  unrelated polling. Added a read-only Playwright regression runner.

## 0.2.0 — user-facing beta, October 29, 2026

- Standard Docker Compose startup with persistent storage, direct workspace access,
  health checks, and local-only access by default.
- BARO investigations can start with explicit manual sensitivity; existing
  calibration remains optional. Manual mode abstains on healthy windows and
  records threshold provenance in saved results.
- Bounded pending queue, retry guidance, and authenticated queued-job cancellation.
- Onboarding remains reachable before configuring telemetry or sensitivity.
- Workspace backup/restore commands with integrity checks and overwrite protection.
- Installable server/admin commands and packaged console assets.
- Product-focused landing page, getting-started instructions, and operations guide.
- CI covers the application workflow and a fresh Docker deployment.

Scope: a single-instance workspace for a trusted team with access to the deployment.
No individual identity, tenant isolation, or automated remediation.

The workspace now opens directly without API-key sign-in or browser key storage.
Server/API authentication remains an optional administrator setting.

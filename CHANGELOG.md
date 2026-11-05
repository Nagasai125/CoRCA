# Changelog

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


## November 4, 2026 follow-up

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

# Working on CoRCA

CoRCA is a self-hosted beta. A production deployment path exists; that does not
mean diagnosis accuracy, provider compatibility, or production load is certified.
Keep product changes and research experiments separate.

## Find the right place

| Path | Responsibility | Shipped in the application |
| --- | --- | --- |
| `frontend/` | Console pages, styles, interactions, fonts, help content | Yes |
| `backend/` | API, authentication, storage, adapters, operator commands | Yes |
| `corca/` | Shared diagnosis and ranking engine | Yes |
| `tests/backend/` | Application, security, storage, adapter regression tests | No |
| `tests/` | Engine and research checks; Node console checks | No |
| `tests/lab/` | Local lab deployment and evaluation checks | No |
| `e2e/` | Browser acceptance tools and synthetic provider fixtures | No |
| `lab/` | Dedicated kind cluster, fault collection, deployment, evaluation | No |
| `scripts/` | Research experiments, benchmarks, calibration, plots | No |
| `docs/` | Product operations and research evidence | No |

The console is `frontend/app.html`; it has no separate frontend build.
Keep backend imports independent of `lab`, `scripts`, `tests`, and `e2e`.
FastAPI serves the frontend and its fonts; both directories are included in the
wheel and Docker image. The diagnosis engine remains a separate shared package.
Research commands run from this checkout, not inside the production image.

## Local development

Use Python 3.12 and Node.js 22 or newer for console tests:

```sh
uv sync --frozen --extra dev --extra server
uv pip install --no-deps --require-hashes -r backend/rcaeval.lock
uv run --no-sync corca-serve
```

Open `http://localhost:8000/app`. Docker users can instead run
`docker compose up -d --build --wait`. Configuration, authentication, backups,
and deployment restrictions are in [operations](docs/guides/operations.md).
Use separate databases and ports for test servers; keep customer state intact.

## Checks before a pull request

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run --no-sync python -m pytest -q
node --test tests/*.test.cjs
uv build --wheel
uv run --no-sync python e2e/check_distribution.py dist/*.whl
```

For application-only changes, `python -m pytest -q tests/backend` is the focused
suite; CI still runs all tests. For UI changes, also run the controlled browser
check described in [e2e/README.md](e2e/README.md). These checks use fixtures;
live provider credentials and real incident evaluation require separate evidence.

Use a focused branch such as `codex/connection-status`. Explain the customer
problem, resulting behavior, checks run, and remaining limitations in the PR.
Update callers, fixtures, operator instructions, and the changelog when behavior
or commands change. Do not mix unrelated cleanup with a feature change.

Use the [documentation index](docs/README.md) when updating guides. API changes
also update the [API reference and schema](docs/api/README.md#maintaining-the-reference);
the normal Python suite checks schema drift and documentation links.

## Research and generated files

See the [research workflow](docs/research/workflow.md) and [lab guide](lab/README.md).
Never run fault-injection tools against customer infrastructure. Lab tools target
the dedicated `corca-lab` context.

Datasets (`data/`, `RE1-OB/`), outputs (`results/`), downloaded tools (`.tools/`),
environments, and backend/lab state are local and ignored. They are not source
scaffolding. Do not delete them as part of a source cleanup or commit credentials,
customer logs, databases, or backups. Use a new results directory for each changed
experiment; retain manifests and calibration provenance.

## Tool paths after the repository separation

| Previous command or path | Current command or path |
| --- | --- |
| `runtime/web/` | `frontend/` |
| `runtime/` server modules | `backend/` |
| `python -m runtime.api` | `python -m backend.api` |
| `python -m runtime.access` | `python -m backend.access` |
| `python -m runtime.admin` | `python -m backend.admin` |
| `python -m runtime.quality` | `python -m backend.quality` |
| `python -m runtime.pilot` | `python -m backend.pilot` |
| `python -m runtime.deploy` | `python -m lab.deploy` |
| `python -m runtime.client` | `python -m lab.client` |
| `python -m runtime.evaluate` | `python -m lab.evaluate` |
| `python -m runtime.export_cases` | `python -m lab.export_cases` |
| `runtime/deployment.yaml` | `lab/deployment.yaml` |
| `runtime/source.online-boutique.json` | `lab/source.online-boutique.json` |
| `corca-run` | `python -m scripts.run_eval` |
| `corca-summarize` | `python -m scripts.summarize` |
| `corca-demo` | `python -m scripts.demo_synthetic` |
| `runtime/test_*.py`, `tests/runtime/` | `tests/backend/` or `tests/lab/` |
| `scripts/check_console_browser.py` | `e2e/check_console_browser.py` |

`corca-serve`, `corca-admin`, API routes, workspace storage, and Compose commands
retain their existing interfaces. Historical experiment manifests are preserved;
moving evaluation source files changes source fingerprints, so use a new output
directory when running a new experiment.

Local ignored state moved with the backend into `backend/.state/`; the Compose
database stays at `/var/lib/corca/corca.db` on its existing volume. Update external
scripts or environment variables that explicitly name an old source or local state
path. Use the root `.venv/` for development; recreate any legacy environment rather
than relying on scripts whose absolute interpreter paths changed.

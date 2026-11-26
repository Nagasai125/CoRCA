# Frontend guide

The console lives in `frontend/app.html`; `frontend/index.html` is the landing
page. Styles and JavaScript are bundled directly in those files. FastAPI serves
the console at `/app`, the landing page at `/`, and bundled fonts at `/assets/fonts/`.
There is no separate Node application or frontend compilation step.

## Navigation and design

The customer-provided references define the light theme: `#f8fafd` page background,
white cards, `#0b57d0` actions, `#d3e3fd` selection, pill controls, compact status
badges, and bundled Roboto Flex/Google Sans Code fonts. Statuses pair text with
color. Respect keyboard focus and reduced-motion settings.

| Area | Customer task |
| --- | --- |
| Home | Find active problems, recent results, and incomplete setup |
| Alerts | Review received Alertmanager episodes and linked diagnoses |
| Diagnoses | Choose an incident time, start work, review history and evidence |
| Log analysis | Upload a file, inspect patterns and time buckets, download a report |
| Evaluations | Review published quality evidence, sample counts, and acceptance checks |
| Settings | Connect monitoring, discover services, and save dependencies and sensitivity. Open Team & access here to manage accounts and roles. |
| Ask CoRCA | Read platform guidance and optional AI answers in a drawer |

Internal query-string view names retain compatibility with existing links;
the visible label can differ from the internal view identifier. Follow existing
navigation handlers rather than replacing links with independent page reloads.

## Interaction rules

- Alert polling buffers new data behind an explicit update action. Pause stops
  automatic requests. Late responses must not replace newer filter results.
- Diagnosis polling preserves expanded evidence and keyboard focus when the
  selected result has not changed. Job lifecycle and finding status are distinct.
- Log charts expose exact counts and empty time buckets. Interactive selection
  changes the readout without changing the plotted evidence.
- Dependency exploration uses saved edges; exploring the map does not save edits
  or establish causality. Scores are relative evidence, not probabilities.
- Help uses bundled `frontend/help-topics.json` plus optional server-side AI.
  The drawer manages focus and Escape. Diagnosis contents are not attached to help.
- Setup changes require administrator access. Display empty, disconnected,
  restricted, failed, and partial-data states from actual API responses.

## Verification

Run the Node checks listed in [CONTRIBUTING](../../CONTRIBUTING.md), then the
[controlled browser checks](../../e2e/README.md). Check narrow screens, keyboard
navigation, reduced motion, loading/error states, and packaged font delivery.
Store local screenshots and verification output under ignored `results/`, outside the documentation tree.

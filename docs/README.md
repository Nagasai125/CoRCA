# CoRCA documentation

Start with the guide for your task. Current product instructions are separate
from research procedures and historical validation reports.

| Task | Read |
| --- | --- |
| Install or contribute | [Project README](../README.md), [contributor guide](../CONTRIBUTING.md) |
| Integrate with the API | [API guide and endpoint reference](api/README.md), [OpenAPI schema](api/openapi.json) |
| Operate, authenticate, back up, or troubleshoot | [Operations](guides/operations.md) |
| Connect a monitoring provider | [Telemetry integrations](guides/telemetry.md) |
| Understand or change the console | [Frontend guide](guides/frontend.md) |
| Prepare a customer pilot | [Pilot checklist](guides/pilot.md), [case template](guides/pilot-cases.example.json) |
| Understand the stack | [Architecture diagram](architecture/corca-stack-diagram.html) |
| Run research or benchmark evaluations | [Research workflow](research/workflow.md), [evaluation runbook](research/evaluation.md) |
| Prepare the defense and committee evaluation | [Committee review workflow](guides/defense.md) |
| Validate the local lab's memory gate | [Memory stability](research/memory-stability.md), [lab setup](../lab/README.md) |

## Keeping documentation current

Product behavior belongs in `guides/` or `api/`. Research procedures belong in
`research/`. Remove superseded guides instead of maintaining an archive tree.
Record release changes in the root changelog rather than adding another dated
guide. Keep one current guide per subject and update links when moving it.

API request schemas come from `backend.api.create_app().openapi()`.
The API documentation tests check the schema snapshot, route coverage, and local
documentation links. See the [API maintenance instructions](api/README.md#maintaining-the-reference).

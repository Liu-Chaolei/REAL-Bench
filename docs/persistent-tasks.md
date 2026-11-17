# Gaia2 async tasks and evidence Adapters

Gaia2 contains 1100 task definitions: the existing 1000 tasks and 100 `async`
variants. All definitions live in `data/tau2/domains/gaia2/tasks.json` and use the
ordinary loader. The `async` split selects the new variants; `base` selects the
complete collection. The original seven category splits retain their members.

Each async variant has its own ID, an immediate source task ID and a private
`scenario.async_interaction` configuration. `scenario.async_metadata` records
source category/revision, the legacy qpt ID and data readiness. The manifest's
`async_legacy_ids` maps historical qpt IDs to ordinary task IDs. There is no
active provider-specific sidecar directory. Data drafts are recorded separately
from provider capability and live measurement status.

## Provider boundary

```text
Native observer/receiver → Provider Adapter → AsyncEvidenceSource
→ shared identity/projector/scheduler/coordinator → shared evaluator
```

A tested Agent optionally exposes `get_async_evidence_source()`. The returned
port implements `enable()`, `get_capabilities()`, `drain_events()` and
`get_status()`. Ordinary tasks do not enable the port. Missing capabilities or
unready task data produce per-task unscored skips before model execution.

An Adapter passively normalizes lifecycle, operation and generated-reply events.
It does not execute task operations, select benchmark identities or grade them.
Task/call/response IDs are session-scoped. Python monotonic receive times map to
one scenario clock; provider timestamps remain audit evidence. The receiver
queues events and the orchestrator thread alone consumes them.

Canonical operation results distinguish state, result availability, error,
acceptance and confirmed effect. Qwen tool names, `task_status`, `list_all`, native
updates and internal instructions are interpreted only by the Qwen Adapter.
The common binder uses canonical task objectives, not provider raw payloads.
Qwen's JavaScript observer remains in `integrations/qwen_audio_agent/`. Other
providers require a real observer and Adapter before their async capability can
be claimed; the existing GPT Live converter alone is not that verification.

## Scoring and replay

Native reward remains operation × state/result × reply. Native operations belong
to the request active at call start; prefetch remains explicitly configurable.
Generated-text v2 is functional reply evidence; played-audio ASR is diagnostic.
A response first observed before a request cannot move into its reply window
because its final caption arrives later. Native, ordinary Gaia2 and overlay
scores remain separate (`combine_reward=false` in the migrated definitions).

Unsupported, not-reached and unavailable measurements have null native rewards.
Complete observation with an incorrect/missing required operation yields a
behavior failure, not an infrastructure skip. Reference execution verifies only
the business graph; it does not prove real async measurement coverage.

New evidence uses `persistent-task-evidence-v1` and
`persistent-task-grading-v5`. Historical Qwen evidence is verified against its
original hash and read through the legacy compatibility path; saved verdicts and
files are preserved. Replay never connects a model or backend.

See [Qwen execution](qwen-persistent-validation.md) and [evaluation usage](gaia2-async.md).

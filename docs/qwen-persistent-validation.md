# Native Qwen persistent-task integration

This implementation observes the fixed Qwen TaskManager and the original
`AgentTaskRuntime` methods. It never mounts the historical in-memory task tools
in a live environment. Live code implements the shared Adapter contract for native status/result queries and mutation requests with an
explicit final-stage business-goal overlay. Mutation references must pass
reference/replay preflight before provider connection; the full ten-scenario
benchmark is not yet verified.

## Run a real observer probe

Build a local overlay on the verified Qwen integration image:

```bash
docker build -f integrations/qwen_audio_agent/Dockerfile.persistent \
  -t tau-qwen-persistent:0.1 integrations/qwen_audio_agent
```

The base image's fixed Qwen source and dependencies are inherited; the overlay
only adds passive bridge instrumentation. Its immutable image ID is recorded by
the provider. To use another base, pass an immutable `--build-arg BASE_IMAGE=...`.

Probe the actual control connection without submitting a benchmark task:

```bash
LD_LIBRARY_PATH="$PWD/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
UV_FROZEN=1 .venv/bin/python scripts/probe_qwen_persistent.py \
  --config integrations/qwen_audio_agent/config.gaia2.persistent.example.json \
  --output data/simulations/qwen-persistent-probe/backend-probe.json
```

A successful probe proves observer readiness only. It does not prove a model
created a task, answered a status question, or passed the benchmark.

## Run the first scenario

Runtime changes require the ordinary Gaia2 reference/replay verification and
seal workflow before a standard live run. It verifies the complete dataset:

```bash
integrations/qwen_audio_agent/run_hpc.sh gaia2 verify --reference --seal --workers 16
```

Then use the ordinary runner and user speech pipeline:

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.persistent.example.json \
  --task-ids 0052_snsq4rd73gytoo0wr75bgd2nyzra07z1__async_0001_v1 \
  --max-seconds 600 --save-to qwen-persistent-first
```

Add `--dry-run` to inspect the plan. Credentials remain environment references.
The embedded async configuration is evaluator-private and is not sent to the bridge. The default
`combine_reward=false` keeps ordinary Gaia2 reward separate. Combining requires
both explicit configuration and a task reward basis containing PERSISTENT_TASK.
Changing sidecars is rejected on resume, including with auto-resume enabled.

The draft running request expires if the task completes before delivery becomes
safe. There is no synthetic delay to keep it running. Expiry and unasked requests
are retained in coverage rather than treated as successes.

## Evidence and replay

The simulation's `info.persistent_tasks` contains the private configuration,
normalized backend events, identity bindings, transitions, request release and
actual delivery times, native operations and actual played-audio reply windows.
The per-call diagnostics directory also receives `persistent_tasks.json` before
semantic grading, so judge errors do not erase evidence. Saved-trajectory grading
uses the same persistent grader as live runs and verifies deterministic replay.

For a saved sidecar artifact:

```python
import json
from tau2.persistent_tasks.backend_runtime import replay

summary = json.load(open("path/to/persistent_tasks.json"))
replayed = replay(summary)  # No models, wall clock or business tool calls.
assert replayed["releases"] == summary["releases"]
```

The grader reports operation, state/result and semantic reply checks separately,
along with expected/activated/queued/delivered/answered coverage. Nonempty speech
alone cannot earn reply credit. Evidence issues produce `evidence_unavailable`
and a null persistent reward. Incorrect model behavior with complete evidence
remains a measured failure.

## Mutation overlays

Modify/cancel requests require a `persistent-overlay-v1` overlay. It pins the
source revision and final stage, lists the exact superseded reference event IDs,
and supplies replacement references, stage criteria and deterministic authoring
checks. User/environment events cannot be replaced. Non-superseded source events
and preceding stages remain intact. Missing dependencies and invalid derived
references fail before connecting to the Agent.

Cancellation retains reference effects already completed before the observed
cancellation receipt. Native `cancelling` is not treated as `cancelled`.
Modification needs a native instruction update marked delivered; an accepted
receipt alone earns no state/result credit. Both original and derived Gaia2
outcomes are reported over the same actual trajectory. Grading v3 keeps the
native management score independent of both business outcomes. Explicit combined
scoring uses the derived business outcome and the native persistent component
once each.
An overlay must itself declare PERSISTENT_TASK in its reward basis to enable
combination. No mutation sample is marked verified merely because these APIs exist.

## Validation status

The actual observer probe succeeded with Qwen revision
`d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9` and local image
`sha256:21d06949b630c4505c19425d21a5487afa4261b70305df0155e17c16f0406626`.

Key-free tests cover native event adaptation, ambiguous identities, ordering,
terminal evidence, state expiry, operation attribution, semantic verdicts,
reply windows, deterministic replay and full streaming input delivery. The
streaming fixture includes early STOP handling without blocking scripted inputs.
Native JS tests preserve method return promises, exceptions and observer cleanup.

The first real trial exposed operation attribution extending into later caller
clarifications and permissions. That failed trial is preserved under
`data/simulations/qwen-persistent-qpt0001-20261113/`. A new ordinary caller turn
now closes the prior request's ownership; completed roles no longer absorb later
work or task restarts. Business completion callbacks are queued from workers and
applied on the orchestration thread.

The second real trial passed the native component with reward **1.0**, full
request delivery, a successful query of the real running task, and a positive
semantic reply verdict. Current-code replay agrees with the frozen
trace. Its original Gaia2 reward is **0.0**, kept separate. The audit report is
`data/simulations/qwen-persistent-qpt0001-v2-20261114/acceptance-current.json`.

All ten draft slots now exist. The two modification overlays and cancellation
overlay passed reference/replay preflight; this is separate from live acceptance.
Only cases with real, complete measurements may enter `verified_scenarios`.
Expected behavior success and measurement validity are reported separately, so a
correctly measured model failure can remain visible. Missing native states or
unasked questions do not become fabricated failure-query evidence.

To repeat a bounded acceptance batch after the first scenario has passed:

```bash
LD_LIBRARY_PATH="$PWD/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
LITELLM_LOCAL_MODEL_COST_MAP=True UV_FROZEN=1 .venv/bin/python \
  scripts/run_qwen_persistent_acceptance.py \
  --config integrations/qwen_audio_agent/config.gaia2.persistent.example.json \
  --cases qpt_0002 qpt_0003 qpt_0004 qpt_0005 qpt_0006 qpt_0007 qpt_0008 qpt_0009 qpt_0010 \
  --workers 3 --max-seconds 600 --run-prefix my-native-acceptance
```

Every attempt retains its own directory and acceptance report. Use a new prefix
for a new attempt. Association requests explicitly allow one status-list lookup
before a focused target query; additional goal operations still fail strict
matching. For a task whose real state never satisfies its trigger, current grading
reports `not_reached` with a null native reward and explicit coverage. A model
that never creates the required backend task still receives a measured failure.

Released mutation requests become the simulator's current conversational goal
through the ordinary scripted-input mechanism. The actor sees only its released
natural-language request, never reference events, expected checks or trigger data.


## Public setup requirements for association

The original association draft allowed Qwen to put all business goals in one
native task. That trace is preserved; it cannot prove two-task association. The
revised draft adds a public `initial_request_prefix` asking for two separately
tracked jobs with descriptions beginning `Email delivery` and `Toy purchase`.
The original business request follows verbatim. No source task JSON is changed,
and the benchmark creates no backend task. The real Agent still decides and
executes its native submissions. The complete prefix is part of the frozen
sidecar hash and actual delivered caller speech.

Bindings support explicit prefix and substring rules on actual native task
creation records. The final draft matches the unique job labels within the
actual descriptions, since the Agent sometimes quotes the labelling instruction
instead of putting the label at the beginning. It allows sufficient time for
both real jobs to finish. Both actual requests and both target queries passed
with management reward 1.0; prior unsuccessful drafts and traces remain intact.

The complete results and remaining two failed-state data gaps are recorded in
[the delivery report](qwen-persistent-results.md). The package remains draft:
eight scenarios have complete measurements, four pass native behavior checks,
and two failure-query candidates remain unverified. This is not a claim that
all ten samples passed acceptance.

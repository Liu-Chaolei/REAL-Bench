# Dynamic persistent-task metric

> 当前异步交互仅保留本运行时动态机制。固定 async100题及10题 Persistent Tasks 材料
> 已从活动数据删除；主文件现为1000题（base800＋两类变体各100），不再有 async split。


## Goal

The persistent-task evaluation is an active metric. It observes a real backend
task during a trial, chooses one trigger intent, generates a user request, sends
that request through the normal conversation path, and scores the agent against
the resolved intent. Fixed async task records do not provide the trigger state
or request text.

The metric has its own evidence and reward. It does not multiply the ordinary
Gaia2 reward. A trial may therefore report both the normal task reward and an
independent persistent-task metric result.

## Runtime flow

```text
active_metrics configuration
  -> bind the first eligible primary backend task
  -> sample one preferred state and operation
  -> observe normalized backend events
  -> wait for state and dwell gates
  -> resolve a deterministic fallback when the preferred state is blocked
  -> generate a request with the configured LLM
  -> fall back to the call-language template catalog when generation fails
  -> enqueue one user input
  -> record delivery, backend operations, and the reply window
  -> write evidence and calculate the independent metric score
```

Half-duplex and full-duplex orchestrators drive the same active metric manager.
The manager is called at turn or tick boundaries, and all backend event handling
and user-input enqueueing remains in the orchestration thread.

## Configuration

Active metrics live under `active_metrics`, separate from post-call
`metrics_config` and legacy `persistent_task_config`.

```yaml
active_metrics:
  persistent_task:
    enabled: true
    policy_version: persistent-task-metric-v1
    preferred_options:
      - {state: waiting, operation: query, weight: 0.10}
      - {state: queued, operation: query, weight: 0.10}
      - {state: running, operation: query, weight: 0.40}
      - {state: completed, operation: result, weight: 0.20}
      - {state: failed, operation: query, weight: 0.15}
      - {state: cancelled, operation: query, weight: 0.05}
    gates:
      min_observations_after_creation: 2
      min_dwell_seconds: 3
      min_turn_boundaries: 1
      initial_state_guard: true
      max_wait_seconds: 300
      reply_timeout_seconds: 30
      reply_max_steps: 8
    generator:
      model:
        model: qwen3.8-flash
      model_env: QWEN_MODEL
      api_base_env: QWEN_BASE_URL
      api_key_env: QWEN_API_KEY
```

The CLI accepts `--active-metrics-config`. The resolved configuration is saved
in run metadata and included in checkpoint consistency checks. Active metrics
and legacy `persistent_task_config` are mutually exclusive.

## Trigger policy

Each trial derives a reproducible seed from the run seed, task ID, trial ID and
policy version. One preferred option is sampled from the provider-supported
states and operations. The metric never resamples after a fallback.

The initial task-created event cannot trigger a request. A preferred state must
have enough post-creation observations, remain stable for the configured dwell
time, and reach a safe conversation boundary. Completed result requests also
require result availability.

Fallback is a finite mapping keyed by `(preferred_state, blocked_state)`. For
example, `running -> failed` resolves to `failed/query`, while `running ->
completed` resolves to `completed/result`. Missing mappings produce
`unresolved`. Once delivery starts, the resolved intent is frozen.

The first backend task with a valid identity and primary lifecycle evidence is
the metric binding. Later backend tasks are ignored by this metric instance.

## Request generation

The policy decides state, operation, and reference mode. The LLM only produces
the natural-language request. Its structured response must preserve the
resolved state and operation, use the call language, stay within the character
limit, and avoid internal IDs or completion claims that contradict the state.

Generation has a timeout and bounded attempts. On failure, the metric selects a
stable request from:

```text
data/tau2/metrics/persistent_task/templates/<language>.json
```

The template catalog is versioned and hashed in evidence. A missing language
catalog produces `generation_unavailable` and does not count as an agent
failure.

## Qwen realtime integration

The persistent Gaia2 Qwen configuration uses the native realtime frontend.
`speech_to_speech` is absent. The frontend reads:

```text
QWEN_AUDIO_REALTIME_PROVIDER
QWEN_AUDIO_REALTIME_MODEL
QWEN_AUDIO_REALTIME_BASE_URL
DASHSCOPE_API_KEY
```

The bridge writes a trial-local `frontend-models.json` entry for
`qwen-audio-3.1-realtime-flash`, because the base Qwen catalog does not contain
that model. The catalog entry selects the DashScope `audio` API family.

The backend and caller model references are configured independently. The
current smoke example can use either `QWEN_*` or `DEEPSEEK_*` references by
changing the environment references in the Qwen config and active metric YAML.

## Evidence and scoring

The metric stores binding, observations, state transitions, preferred and
resolved intents, generated request, fallback reason, delivery timing, backend
operations, reply text, termination reason, and policy digests in
`simulation.info.active_metrics` and the diagnostic evidence source.

The independent performance value is `completion_score`. It preserves the
existing native-result and reply success conditions. `operation_reward` remains
a log field; native-result and reply judgments are boolean details under
`evaluation_log.native_result` and `evaluation_log.reply`. No component scores
are displayed separately. A skipped reply judgment is marked not evaluated,
rather than reported as a model-judged incorrect reply.

Historical `reward` / `native_reward` totals remain readable; explicit zero and
unavailable values are preserved. Existing fixed-request grading uses the same
output shape while retaining its original judging conditions.

`query` accepts the actual state returned after delivery. `result` requires a
completed task with an available result. `modify` requires an accepted update,
and `cancel` requires a confirmed cancellation. Semantic reply grading uses the
native result and resolved intent, rather than string matching the generated
request.

`unsupported`, `generation_unavailable`, `unresolved`, `not_reached`,
`reply_unavailable`, and `evidence_unavailable` are metric coverage states. They
do not directly become an agent failure.

## Development runtime verification

The normal Gaia2 environment compares the task runtime pin with the sealed
manifest. Full reference sealing remains required when Gaia2 task semantics,
environment behavior, tool behavior, or official scoring changes.

For active metric and provider smoke iteration, the CLI and Qwen smoke launcher
support `--gaia2-allow-stale-runtime`. This skips only the runtime pin
comparison. Task artifact and resource checks still run. It does not modify the
manifest or produce a new seal.

This development switch is intended for local smoke runs. Release or official
benchmark runs use the normal verified runtime path.

## Implementation status

- Active metric configuration, manager, policy, fallback, request generator,
  template fallback, runtime evidence, and independent scoring are implemented.
- Half-duplex and full-duplex orchestration hooks are implemented.
- CLI, runner, batch, checkpoint, diagnostics, and smoke launcher plumbing is
  implemented.
- Native Qwen realtime model catalog registration is implemented in the bridge
  and persistent-image overlay.
- Focused active metric and persistent-task regression tests pass.
- Live provider acceptance remains dependent on the configured model balances
  and provider quotas.

## Verification

Run the focused checks with:

```bash
UV_FROZEN=1 .venv/bin/pytest -q \
  tests/test_metrics/test_active_persistent_task.py \
  tests/test_persistent_tasks/test_live_backend.py \
  -p no:ddtrace

UV_FROZEN=1 .venv/bin/ruff check \
  src/tau2/metrics/active \
  integrations/qwen_audio_agent/smoke.py
```

For a development smoke run, use the Qwen config, active metric config, and
`--gaia2-allow-stale-runtime`. Official runs must use a verified manifest.

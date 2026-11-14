# Backup checkout validation — 2026-11-11

This report records validation of the copied checkout at
`/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench-backup`.
All generated outputs are under `data/simulations/backup-validation-20261111/`.

## Path repair

The copied virtual environment still pointed at the original checkout. The
following local metadata was repaired:

- `.venv/bin` entry points and activation files now reference `tau2-bench-backup`.
- The editable-install `.pth` imports `backup/src`.
- The editable distribution URL points to this checkout.
- `data/tau2/domains/gaia2/manifest.json` now points at the copied Gaia2 assets.

The sealed Gaia2 task bytes, resource hash and runtime hash were unchanged. The
historical `source_root` in the immutable task-variant provenance manifest was
left intact because it is provenance, not a runtime input.

The reusable local wrapper is [`scripts/run_backup_smoke.sh`](../scripts/run_backup_smoke.sh).
It forces the Python source, data directory, temporary files, caches and native
audio libraries to remain inside this checkout.

## Offline validation

The following checks passed:

```text
tau2 check-data
309 Gaia2/voice/streaming/diagnostics tests passed
ruff check scripts/run_gaia2_series_smoke.py integrations/qwen_audio_agent/smoke.py
```

The Python preflight confirmed that `tau2` imports from `backup/src/tau2`, the
data directory is `backup/data`, Gaia2 task artifacts and resources verify, and
the computed runtime digest matches the sealed manifest.

## Live Qwen smoke attempts

The installed Docker image
`docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.3-real-metrics`
was available. A one-task, three-request Gaia2 voice run reached the Qwen
bridge, Speech-to-Speech service, Gateway and first agent response. It then
failed as an infrastructure trial while the User simulator was making its
turn-taking request:

```text
litellm.InternalServerError: OpenAIException - Connection error
```

A second run using the existing MiMo User profile reached the same runtime path
but failed with the configured relay reporting:

```text
AuthenticationError: API Key 无效或已被禁用 [RELAY_102]
```

These are provider/credential failures, not Gaia2 task failures. Both saved
runs are preserved:

- `data/simulations/backup-validation-20261111/live/`
- `data/simulations/backup-validation-20261111/live-mimo-user/`

The run logs, task logs, audio taps, bridge logs and `results.json` files are
inside those directories. No live run reached a gradable business result, so no
task accuracy claim is made.

The selected task and provider-profile audit are also stored under the same
validation directory. Secrets are not included in this report.

## DeepSeek isolation probe — 2026-11-11 15:39 UTC

The configured DeepSeek profile was tested independently from Docker, Qwen,
Speech-to-Speech and the User simulator. The profile resolves to
`https://apifusion.aispeech.com.cn/v1` and model `deepseek-flash`; the key was
present but was never written to the result files.

The sandboxed probe could not resolve the endpoint (`gaierror`/`ConnectError`).
That was a sandbox network restriction. The same probe with external network
access succeeded:

```text
DNS:              ok
GET /models:      HTTP 200; configured model present (2 models listed)
POST /chat/completions: HTTP 200
POST /responses:        HTTP 200
tau2 generate():       ok, 1.017 seconds, two-character response
```

The result records are:

- `deepseek-connectivity.json` — sandbox-restricted attempt;
- `deepseek-connectivity-escalated.json` — direct HTTP probe;
- `deepseek-litellm.json` — LiteLLM probe;
- `deepseek-tau-generate.json` — the exact tau2 `generate()` wrapper.

This isolates the earlier live-run failure: the DeepSeek endpoint and key were
reachable and valid when tested directly. The first full run's
`OpenAIException - Connection error` was transient or arose under the long
voice-run/request pattern; it was not caused by a stale backup path. The second
full run used the MiMo User profile and independently failed with its own
`RELAY_102` credential error. A full gradable Qwen trial still requires a clean
provider run; no business accuracy is inferred from either failed trial.

## Completed end-to-end retry — 2026-11-11 16:03 UTC

The persistent-terminal retry used the DeepSeek User profile and completed the
full runtime path:

```text
Qwen bridge / Speech-to-Speech: started
User simulator: DeepSeek requests succeeded
Gaia2 backend tool calls: completed
Three scenario stages: delivered
Evidence collection: complete
Simulation termination: max_steps after 663.9 seconds
```

Saved run:

`data/simulations/backup-validation-20261111/live-deepseek-retry2/`

The task reward was `0.0` because the third stage ended without the requested
one-line final answer. This is a model/task outcome, not an infrastructure
failure. The diagnostics were produced successfully:

- intent: Precision `0.9091`, Recall `1.0`, F1 `0.9524`;
- execution: evidence incomplete/not applicable for this query-only reference;
- voice: measured from the collected audio timeline;
- backend inputs, operations, state frames, audio taps and Qwen bridge logs are
  all present.

The run emitted repeated LiteLLM cost-map warnings for the custom model name and
three diagnostic calls rejected by the configured MiniMax endpoint because it
does not expose `gpt-5.5`. Those warnings/errors did not prevent the main
simulation, intent extraction or voice measurement. A production diagnostics
profile should set every diagnostic role, including execution and any inherited
judge role, to a model supported by its endpoint.

## Second multi-request task — 2026-11-11 16:33 UTC

Task `0254_qb5m2mayzadgk39jz5ddsxpq0uyb10wu` was selected because it has three
short requests: identify the most frequent email sender, reply to that person,
then confirm the contact and create a calendar call. The run reached only stage
0 before `max_steps`; stages 1 and 2 were not released. It produced a valid
trajectory and diagnostics, but no complete multi-request business result.

Saved run:

`data/simulations/backup-validation-20261111/live-task0254/`

The main cause was slow voice/user turn processing combined with the 600-second
conversation budget. Repeated `deepseek-flash` cost-map warnings are harmless;
the run did not fail from endpoint connectivity. The stage report explicitly
records `not_reached: [1, 2]` and the final reward is zero because continuous
requests were not all delivered.

## Third multi-request task — 2026-11-11 17:15 UTC

Task `0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm` exercised three dependent file
requests: find the Documents subdirectory with the most files, count the direct
files in that directory, and read the full contents of
`/Documents/news/metro_herald_586.txt`.

All three stages were released and their tool evidence was recorded. The
environment and natural-language checks all passed, giving reward `1.0`.
The run still terminated at `max_steps` after `662.64s`: the file-reading tool
completed, but the spoken answer was a summary and the User simulator generated
follow-up requests asking to read the whole file. Its final response was still
pending when the tick budget expired. The saved stage record has
`current_stage: 2`, no missing requests, and stage 2 has no stop event.

Saved run:

`data/simulations/backup-validation-20261111/live-task0251/`

Intent diagnostics were complete (precision `0.7143`, recall `0.9375`, F1
`0.8108`); execution evidence was marked incomplete because this is a
query-only scenario. The repeated custom-model cost-map warnings and the
unsupported `gpt-5.5` diagnostic retries did not affect the task reward.

## Complex execution task attempt — 2026-11-11

Selected execution task `0339_illr8uvhjagekw54zj9cx0p62adx6abu`, which has three
requests and 115 reference operations: calendar scheduling, email reply, two
group-message flows, a second calendar booking, and a factual email follow-up.
Both attempts used the new `6000` tick setting (`1200s` simulated maximum).

Neither attempt reached the conversation. The Qwen Agent WebSocket failed during
initial connection with `TimeoutError` in
`qwen_agent/discrete_time_adapter.py:connect`; no agent/user turns, tool calls,
or reward were produced. The saved attempts are:

- `data/simulations/backup-validation-20261111/live-complex-0339/`
- `data/simulations/backup-validation-20261111/live-complex-0339-retry/`

This is a frontend infrastructure/connectivity failure, unrelated to the task's
complexity or the 6000-tick limit.

## Repaired complex-task retry — 2026-11-11

The same complex execution task was rerun after repairing Qwen Docker startup
cleanup. The repaired run successfully connected to the Qwen bridge and reached
real application execution; the previous startup `TimeoutError` did not recur.

Saved run:

`data/simulations/backup-validation-20261111/live-complex-0339-fixed/`

The run used `6000` ticks / `1200s` simulated duration and reached the limit at
`6001` recorded ticks after about `1343s` wall time. Stage 0 completed and sent
its STOP. Stage 1 was released and delivered, but the Agent asked for permission
to create a new Messages group and did not receive a stage STOP before the limit;
stage 2 was not reached. The trajectory contains substantial real execution,
including 101 document reads, 4 Messages sends and 2 Chats sends. Its saved
stage summary is `current_stage: 1`, `not_reached: [2]`, termination
`max_steps`.

The smoke wrapper was interrupted while waiting for post-run diagnostic scoring;
the main simulation and trajectory were already complete. The run therefore has
no populated `simulation_index`/final reward summary, but the trajectory,
operation evidence, audio taps and diagnostic inputs are preserved.

The startup fix adds a unique Docker runtime label and recovers/removes a
container if `docker run` times out before writing its cidfile. Targeted runtime
regression tests pass (`6 passed`).

## Sequential DeepSeek prompt replay — 2026-11-11

To test whether frequent long diagnostics requests themselves hang the provider,
`scripts/replay_deepseek_stuck_prompts.py` reconstructed the two problematic
requests and sent them consecutively to the configured DeepSeek endpoint using
the OpenAI-compatible `/chat/completions` API:

1. `0339` backend intent prediction, 36,363 characters: HTTP 200 in `30.7s`.
2. `0446` repetition check, 29,943 characters: HTTP 200 in `2.9s`.

Both requests returned when sent directly and back-to-back. This rules out a
simple provider rule that long consecutive requests always hang. The original
stalls are therefore specific to the in-process LiteLLM/diagnostic path or an
intermittent provider request, rather than a deterministic DeepSeek inability to
handle the prompt sizes. The raw replay metadata is saved in
`data/simulations/backup-validation-20261111/deepseek-stuck-prompt-replay.json`;
no prompt or credential is stored there.

## LiteLLM reproduction — 2026-11-11

The same prompts were then sent through the actual tau2 path
`TauJsonModel.complete_sync -> llm_utils.generate -> LiteLLM completion`, in a
fresh process and sequentially. Both returned:

- `0339` prediction: `111.4s`;
- `0446` repetition: `14.7s`.

A second fresh reproduction first sent the original 0446 long evaluation prompt
(59,626 characters) and then an uncached repetition prompt (29,943 characters).
Both returned again: `114.3s` and `11.3s` respectively. The sequence output is
stored in `data/simulations/backup-validation-20261111/litellm-long-repro.json`.

This reproduces the same provider, model, endpoint, timeout and LiteLLM call
boundary but does not reproduce the hang. It rules out a deterministic deadlock
caused solely by prompt length, two sequential requests, or a long evaluation
immediately preceding repetition. The only recurring extra delay is LiteLLM's
attempt to fetch its remote model-cost map; it falls back locally after an SSL or
network timeout and is not the request hang.

## Diagnostics timeout retry change — 2026-11-11

`TauJsonModel` now has an outer async deadline around the worker thread in
addition to LiteLLM's provider timeout. A timeout is retried once; non-timeout
errors are still raised immediately. Synchronous diagnostics callers also retry
provider timeout exceptions once. The retry consumes a second request-budget
slot and successful output remains content-addressed. Targeted diagnostics tests
pass (`41 passed` for model/conciseness and `25 passed` for repetition).

## Retry-timeout rerun of task 0446 — 2026-11-12

The rerun used the new timeout logic and reached the live voice simulation, but
the DeepSeek distributor then returned a terminal provider error:
`No available channel for model deepseek-flash under group deepseek-sjtu`.
This is `ServiceUnavailableError`, not a timeout, so the new timeout-only retry
correctly did not repeat it. The run stopped at about 300 simulated seconds and
is saved at:

`data/simulations/backup-validation-20261111/live-complex-0446-retry-timeout/`

## Second complex execution retry — 2026-11-11

Task `0446_tqbo722zw4l86tpqfs9uqh7nbk8jpnjr` was selected with 50 reference
operations across three requests: remove saved apartments, compare crime rates,
save the lowest-crime listings, send a Messages result, then rank/filter the
saved apartments and send an email.

Saved run:

`data/simulations/backup-validation-20261111/live-complex-0446/`

The repaired Qwen startup path worked again. The main simulation reached
`6001` ticks and terminated at `max_steps` after about `1317s` wall time, while
still in stage 0. It executed substantial real work, including crime-rate
lookups, apartment searches, five removals, six saves and one Messages send;
stages 1 and 2 were not reached. The post-run diagnostics again outlived the
main simulation; GT and prediction artifacts were written, but the wrapper was
interrupted while waiting for final scoring, so no final simulation index was
committed.

## DeepSeek model-name check — 2026-11-12

The configured relay currently reports `deepseek-flash` and `deepseek-v4-pro` from
`GET /models`. The configured `deepseek-flash` succeeds through both
`/chat/completions` and `/responses`; `deepseek-v4-pro` also succeeds through
`/responses`. The legacy names `deepseek-chat` and `deepseek-reasoner` returned
503 `model_not_found/no available channel`.

The current `DEEPSEEK_MODEL=deepseek-flash` setting is therefore valid and should
not be changed for this run. The earlier error was a temporary unavailable
channel for a valid model. The direct probe is recorded in
`data/simulations/backup-validation-20261111/deepseek-model-check-20261112.json`.

## MiMo observer failure diagnosis — 2026-11-12

A minimal request using the exact configured MiMo relay, model
`mimo-v2.5-asr`, API path and WAV input contract returned:

```text
HTTP 401
RELAY_102: API Key 已停用或过期：未提供有效的 API Key
```

Therefore the 0353 failure was an invalid, expired or disabled `MIMO_API_KEY`,
not an insufficient balance response. The probe result is saved in
`data/simulations/backup-validation-20261111/mimo-asr-probe-20261112.json`;
the credential itself was not saved.

## MiMo key cross-check — 2026-11-12

The configured MiMo relay was tested with all four available audio credentials:
`MIMO_API_KEY`, `DASHSCOPE_API_KEY`, `DASHSCOPE_API_KEY2` and
`DASHSCOPE_API_KEY3`. All returned HTTP 401 `RELAY_102`.

The same `DASHSCOPE_API_KEY3` was tested against its matching DashScope Qwen ASR
endpoint with model `qwen-audio-3.1-asr-flash`. It returned HTTP 400
`ASR_RESPONSE_HAVE_NO_WORDS` for a silent probe, which means authentication and
routing succeeded and the ASR rejected the content because it contained no words.

Thus `DASHSCOPE_API_KEY3` is a valid key, but for DashScope/Qwen ASR. It cannot
be substituted into the MiMo relay configuration. To use the valid key, the
observer must be configured as `asr_provider: qwen`, model
`qwen-audio-3.1-asr-flash`, `asr_provider_env: QWEN_AUDIO_REALTIME_PROVIDER`,
`asr_base_url_env: QWEN_AUDIO_REALTIME_BASE_URL`, and
`asr_api_key_env: DASHSCOPE_API_KEY3`. The separate Qwen speech-to-speech
frontend still has its own MiMo ASR/TTS settings and would require a valid
`MIMO_API_KEY` if it remains configured for MiMo.

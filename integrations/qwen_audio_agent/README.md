> 当前 Gaia2 链路使用三文件 API 配置：`config.env` 保存供应商连接，`qw.env` 选择 Agent，`realbench.env` 选择评测与生成角色。入口示例为 `config.gaia2.unified.json`，评分示例为 `examples/real-metrics.unified.yaml`。详见 [API 配置使用说明](../../docs/api-configuration.md)。本页旧供应商专用示例通过原单文件入口兼容使用。

> Python 侧接入代码已迁至 [统一 Adapter 目录](../../src/tau2/adapters/qwen_audio_agent/README.md)。
> 本目录保留构建、部署与容器内系统材料。新的调用者配置使用
> `--user-simulation-config examples/user-simulation.gaia2.json`；旧 Qwen JSON 仍可在入口转换。


# Evaluate the complete Qwen Audio Agent with τ-Voice

`qwen_agent` connects the real Gateway, native task runtime and Qwen Code ACP
backend to τ-Voice. It is separate from `qwen`, which calls a DashScope model
directly. The Qwen checkout is archived at its clean git revision and is never
edited by the integration at runtime. The scenario-clock image includes Qwen's
optional frontend time-provider interface (source commit `02426d5`).

Supported runs are **English retail and Gaia2**, with the
normal Qwen routing and native permissions. A dedicated container, workspace,
MCP environment and fresh sessions are created for every trial. Only public
policy and tool schemas cross into that container; task instructions, reference
actions, raw business databases and evaluation artifacts stay in τ.

Use `--max-concurrency N` for parallel calls; the smoke helper defaults to `1`.
Each call has its own container, control token, loopback ports, MCP server,
workspace and application state. Five simultaneous containers and trial clocks
are covered by the migration's isolation checks.

For Gaia2, use `config.gaia2.example.json` and `smoke --domain gaia2` after data
verification. Its image must declare `gaia2` support; version `0.5.0` includes
both domains. See the [Gaia2 guide](../../docs/gaia2.md) for import and scoring.

## Build

Requires Docker and Python 3.12. From the τ-bench checkout:

```bash
python3 integrations/qwen_audio_agent/build_image.py /path/to/qwen-audio-agent
```

The build uses Qwen's own dependency lock and Qwen Code `0.26.0`. The image's
source revision, source archive hash and immutable image ID are recorded with
each simulation. The image follows the platform naming convention:
`docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.2`.
The build script rejects a dirty Qwen checkout and never copies its `.env` files.

For frontend-only updates, `--base-image <existing-image>` reuses a previously
verified runtime after checking its Qwen source revision, source archive hash,
capabilities and both dependency locks. The resulting image must retain the
base's exact filesystem layers and passes the same complete image contracts.

Platform-injected home/shared-volume mounts are supported. Before any native
preflight or model startup, the external launcher creates a private filesystem
view in user, mount and PID namespaces. It keeps the actual UID/GID, exposes
image programs/system directories read-only, and makes only one generated
`/tmp/tau-qwen-*` trial directory plus runtime devices writable. Other shared
storage and reference files do not appear in this view. Rules cover directory
roots; no per-reference-file blacklist is maintained.

Cross-directory rename, directory moves and hard links inside the private trial
root use normal kernel operations. This works on the current kernel without
Landlock's ABI 1 restriction. Startup tests actual inode-preserving rename and
hard links, hidden files, inherited access, namespace separation and dropped
privileges. Any failure aborts startup; there is no fallback to a weaker or
incompatible isolation method. Additional mounts under allowed program roots
are rejected; normal platform system-file mounts are accepted.

The Docker launch uses `seccomp=unconfined`, `apparmor=unconfined` and
`systempaths=unconfined` **for the trusted setup process** so it can create those
namespaces and a private proc filesystem. No `--privileged`, root override or
capability addition is used. Before any Agent code runs, the launcher drops all
capabilities, sets `no_new_privs`, closes external file descriptors, and installs
an inherited seccomp policy preventing namespace/mount/root changes. The private
PID namespace prevents proc-based access to the unrestricted supervisor's root.
Deployments that disallow the setup options fail clearly rather than disabling
the boundary. This confines local file access; network access is unchanged.

All workspace, prompts, Qwen config/runtime, temporary files and a fresh home
folder live below the trial root. The `HOME` environment variable retains its
original value; that path becomes an alias to the private home folder inside the
new view. No real shared-home files are mounted there. Moves between that home
alias and the workspace also preserve normal rename semantics. A small executable
wrapper restores Qwen Code's native `QWEN_HOME` and `QWEN_RUNTIME_DIR` after its
environment projection. Native permissions, routing and baseline tools remain
enabled; native `tools.executionSandbox` is not used. Qwen source and shared
`~/.qwen/settings.json` are not modified.

The supervisor removes only its generated directories and owned container.
Verbose runs preserve a private bridge trace and redacted container log before
container removal; native credential-bearing settings are not exported.

Run the evaluator in an independent **Python 3.12 venv on the same host** as
platform Docker. This keeps loopback publishing local and prevents shared user
site packages from entering the evaluation environment:

```bash
PYTHONNOUSERSITE=True UV_FROZEN=1 uv sync --python 3.12 --extra voice --extra dev
```

`run_hpc.sh` always invokes this checkout's `.venv/bin/tau2` with user-site
packages disabled and the lock frozen. Standard host setup requires PortAudio
headers/libraries for PyAudio. On this machine, compatible Ubuntu libraries were
extracted without root into the ignored `.venv/lib/tau-native`; the launcher
includes that directory when present. No system packages or shared Python user
site were modified. Recreating the venv requires normal PortAudio provisioning.

## Scenario time

For environments exposing `System__get_current_time`, the bridge injects a
`getCurrentTimeSnapshot` callback into Gateway. The existing frontend
`get_current_time` tool then reads the evaluator's `ScenarioRuntime` through the
same broker and tool execution path as the backend. Each explicit query reads
the clock afresh; no additional clock, polling or per-tick comparisons are used.
The ready metadata records `frontend_time_source` and `frontend_time_tool`.
Other environments retain normal host-time queries.

Scenario runs require the Gateway's `supportsCurrentTimeSnapshotProvider`
interface; an incompatible source revision reports this once at startup. A
failed time query returns an ordinary tool failure and never falls back to the
host date. Build and use the scenario-clock image for the dynamic Gaia2 config.

This covers the designated time-query tools. Native `schedule_reminder`, its
scheduler and arbitrary shell/code date calls still use host time. It also does
not add pause/resume to `ScenarioRuntime`: stopping ticks is distinct from
stopping that clock. See [the implementation plan](../../docs/scenario-clock-injection-plan.md).

## Configure

### Speech-to-Speech frontend

The completed one-task connectivity check is documented in
[SPEECH_TO_SPEECH_RESULTS.md](SPEECH_TO_SPEECH_RESULTS.md).
The subsequent three-task run without a duration cap, including the confirmed
user turn scheduling issue, is documented in
[UNLIMITED_THREE_RESULTS.md](UNLIMITED_THREE_RESULTS.md).
The `v1.2` evaluator scheduling fix and its one-task acceptance result are in
[TURN_TAKING_FIX_RESULTS.md](TURN_TAKING_FIX_RESULTS.md).

The current default path uses the native Qwen Audio frontend and Qwen models for
caller TTS and played-audio observer ASR. The native Qwen Code backend keeps its
separate `GAIA2_BACKEND_*` connection. `config.speech-to-speech.example.json` is
retained as a historical optional frontend profile; it is not used by the default
Qwen smoke path. Automatic listener hotwords are currently disabled; the optional
Qwen listener implementation and its A/B evidence remain documented in
[USER_ASR_HOTWORDS.md](USER_ASR_HOTWORDS.md).

The supplied speech and Gaia2 configurations use Qwen directly. Optional
`llm_fallback_api_key_env` and `llm_fallback_base_url_env` remain available for
a compatible backup using the same model.
HTTP, connection, timeout and streamed provider errors switch the current request
to that pair before any content or tool call has been delivered. Later requests
in the same call stay on the backup. If a response has already delivered content
or a tool call, it fails without replaying that request; the next request uses
the backup. Client cancellation does not trigger a switch. Both failures produce
a sanitized error, and switching is recorded in the redacted container log.
Only the frontend LLM uses this fallback; backend and evaluator credentials keep
their separate configuration. Image `0.5.1` or newer is required. Custom speech
configurations enable it with `llm_fallback_api_key_env` and
`llm_fallback_base_url_env`; omitting both retains a single endpoint.

The historical `speech_to_speech` profile remains available for compatibility,
but is not part of the current Qwen Audio Agent path. Current runs use the native
Qwen Audio frontend and Qwen ASR/TTS directly. The frontend receives PCM audio at
the negotiated Qwen rate; the current bridge reports 16 kHz input and 24 kHz
playback. Cancellation and played-audio observation remain owned by the Qwen
adapter.

The existing top-level `tts` and `asr_*` settings still describe the evaluator's
caller voice and played-audio observer. They are independent of the Agent's new
frontend. The user simulator receives only actually played audio through that
observer, never the frontend's internal recognition or future response text.

The new image contains a separately locked CPU runtime profile: native VAD,
remote OpenAI-compatible STT/LLM/TTS handlers and their dependencies. The stock
HF wheel is installed with `--no-deps`; unused local ASR/TTS/model-backend
dependencies are deliberately excluded. The integration has its own
`speech/requirements.lock` and does not change the main `uv.lock`. Silero 6.2.3,
Smart Turn v3.2, NLTK resources and the HF wheel are hash-verified during build;
the native Torch Hub cache is populated from Silero's unmodified PyPI source
distribution. Runtime has offline model caches and needs no GPU or downloads.
Public generic audio from the provider test fixtures is included only for
key-free image contracts; it contains no benchmark task content.

### Realtime frontend

The supplied `config.example.json` reads the existing, read-only configuration:
`/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env`.
It safely parses dotenv without sourcing a shell, expands `${VARIABLE}` references
and rejects missing/cyclic references in selected settings. Ambient variables
have precedence over file values; `environment` optionally maps container names
to explicit source variable names. Only Agent connection settings are forwarded.
Other judge/intent keys and original file contents never enter the container.
Saved run configuration contains variable names and the file path, never keys.

Supported automatic aliases:

| Agent setting | Config source (after explicit ambient overrides) |
| --- | --- |
| Realtime API key | `QWEN_AUDIO_REALTIME_API_KEY`, `DASHSCOPE_API_KEY` |
| Realtime endpoint | `QWEN_AUDIO_REALTIME_URL`, `QWEN_AUDIO_REALTIME_BASE_URL` |
| Realtime provider/model | `QWEN_AUDIO_REALTIME_PROVIDER`, `QWEN_AUDIO_REALTIME_MODEL` |
| Backend API key | `QWEN_CODE_API_KEY`, `GAIA2_BACKEND_API_KEY`, `OPENAI_API_KEY` |
| Backend base URL | `QWEN_CODE_API_BASE`, `GAIA2_BACKEND_API_BASE`, `OPENAI_BASE_URL` |
| Backend model | `QWEN_CODE_MODEL`, `GAIA2_BACKEND_MODEL`, `OPENAI_MODEL` |
| Backend reasoning | `QWEN_CODE_REASONING_EFFORT`, `GAIA2_BACKEND_REASONING_EFFORT` |

The configured frontend is `qwen-audio-3.1-realtime-flash`/DashScope; the backend
is Qwen Code `0.26.0` using `MiniMax-M3` with reasoning effort `low`. An `openai/`
routing prefix is removed from an explicitly mapped model name. Reasoning uses
native generation settings and the compatible endpoint's `reasoning_effort`
request parameter; the complete-system contract checks the actual request.

The supplied configuration selects **Qwen caller TTS and Qwen listener ASR**:

- Caller TTS: `qwen-audio-3.1-tts-flash`, using the Qwen/DashScope credential in
  the shared private file. The exact caller text is synthesized without text
  optimization.
- Observer ASR: `qwen-audio-3.1-asr-flash`, using `DASHSCOPE_API_KEY3` and the
  configured DashScope regional host. It consumes only actually played Agent
  audio, normalized to 16 kHz WAV. Per-task hotwords are prepared before the
  conversation starts. Recognition uses the shared asynchronous segment and
  bounded-review lifecycle (4 s maximum or 0.6 s trailing silence), with measured
  segment timestamps. Actual vocabulary, provider usage and release latency are
  saved in provenance. The active observer configuration is Qwen-only.
- Native Agent credentials: the example explicitly selects `DASHSCOPE_API_KEY3`
  for Qwen Realtime. Backend variable references stay pinned to
  `GAIA2_BACKEND_*`, separating them from evaluator credentials.

In the Realtime configuration, Qwen keys remain on the evaluator side. The
legacy Speech-to-Speech configuration may contain its historical provider
settings, but the current custom integration selects Qwen through `tts` and
`asr_provider` settings.

Unsolicited caller check-ins are disabled by default. Set
`QwenAgentConfig.user_silence_checkin_seconds` to a positive interval to enable
one after that much silence. Fresh Agent speech or text, initial requests and tool
replies retain their ordinary response timing; the effective value is saved in the
run configuration. Caller generation and synthesis add to the configured minimum.

Scenario callers retain free-text generation. An ungrounded reply is corrected
privately using its reason and the original request, with at most two additional
attempts. Rejected replies are never spoken or committed to conversation history.
Known facts and released scenario inputs are authoritative; Agent statements do
not authorize changed prices, quantities or invented deadlines. Recovery attempts
and costs are retained in `candidate_recovery` diagnostics and task logs.

The Qwen observer can review already-heard audio on a grounding conflict involving
price, quantity, time or negation. `asr_recheck_enabled` defaults to true. Its bounded
playback buffer defaults to 60 seconds; review audio is at most 20 seconds, with
up to 2 seconds of context and a 15-second timeout. The snapshot's last heard
fragment caps the audio range. One fragment group is reviewed once, without
blocking media ticks or overwriting original transcription. Review text only
informs the caller's corrected reply; native captions and future audio are never
substituted for hearing. Name-specific recovery is outside this feature.

Each trial saves `artifacts/task_<id>/sim_<uuid>/trajectory.json` before grading.
Scoring errors retain the actual UUID, duration, ticks and execution journal;
`evaluation_status=failed` and `evaluation_error` distinguish them from conversation
termination. Such runs have no score and are excluded from agent accuracy. The
existing trajectory evaluator can rescore them into a separate output directory.

The smoke helper privately loads `GAIA2_JUDGE_*` for the customer, listener
reactions, communication judge and natural-language assertion judge, preserving
the configured Responses API and reasoning effort. Assertion criteria and reward
calculation are unchanged. The resolved assertion judge is recorded in run
metadata and in each nonempty assertion evaluation. Ordinary runs retain their
default assertion judge unless `TAU2_NL_ASSERTIONS_MODEL` and, optionally,
`TAU2_NL_ASSERTIONS_REASONING_EFFORT` are set before starting the CLI.
Its derived JSON contains only credential references and
nonsecret model settings. For MiniMax's initial customer request, the final role
reminder becomes the required conversational input; its text and all task
instructions stay identical. Other model requests retain their existing roles.
Missing provider prices in LiteLLM mean printed cost totals are not billing
estimates for MiniMax or Qwen.

Official API reference:
[Qwen ASR HTTP](https://help.aliyun.com/zh/model-studio/fun-asr-flash-recorded-speech-recognition-http-api).

The launcher uses the original ACP backend constructor's `builtinMcp` parameter
to append the business server to every native session, retaining the baseline
servers. This matters because explicit ACP session environments can supersede
global MCP settings. The integration therefore pins and tests the source
revision rather than modifying the Gateway's backend implementation. Native
user-scoped MCP connectivity is checked before the voice session starts;
business-operation permissions remain under the original Agent's control.

## Run and inspect

Run three real English retail control calls with the configured services:

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --saasr-config /path/to/runtime.local.json \
  --num-tasks 3 --max-seconds 0
```

Qwen Audio Agent smoke runs use the SAASR caller renderer exclusively. Provide a
local SAASR worker configuration with `--saasr-config`; the former standard
caller TTS and μ-law transport path is rejected by the Qwen adapter.

Use `--dry-run` for a preview, `--num-tasks 1` for one call, or `--task-ids 0 1 2`
for explicit tasks. Each call uses a fresh isolated Agent. The helper retains
recordings, played-audio transcripts, bridge traces, tool trajectories and scores
under `data/simulations/qwen-smoke-<timestamp>/`. It exits nonzero for
infrastructure errors, preserves every attempt and refuses to overwrite results.
The helper defaults to `--max-seconds 1200`, which is 6000 simulation ticks at
the 0.2-second tick size. This gives Gaia2 dynamic follow-ups enough simulated
time for backend retries and later stage delivery. Pass `--max-seconds 0`
explicitly for an unlimited smoke run. Agent/user stop signals and
infrastructure failures still end a trial. Ordinary `tau2 run` defaults are
unchanged; its `--max-steps-seconds 0` explicitly disables the voice tick limit.
SAASR caller generation can run in realtime, or with
`--no-realtime-generation` using first-audio release: the scenario clock pauses
until the first playable sentence is ready, then later sentences continue in the
background.
For a shorter bounded smoke check, pass a smaller positive `--max-seconds N`;
that also sets a wallclock timeout of `N + 150` seconds.

To continue an interrupted batch, repeat its configuration, task selection, seed
and `--save-to` name with `--resume`. Completed task/trial pairs are read from
the checkpoint and skipped; unfinished pairs start fresh isolated calls. This
resumes the batch, not an interrupted voice conversation.

For ordinary CLI use, `run_hpc.sh run` accepts all tau arguments. The JSON config
contains the Qwen choices; configure evaluator LLM credentials separately or use
the smoke helper to load the existing judge service privately. Repeat the same
selected tasks and seed when comparing speech conditions.

```bash
integrations/qwen_audio_agent/run_hpc.sh view --file data/simulations/<run-name>
```

Every business MCP request becomes a normal τ `ToolCall`. Only τ executes it;
its exact `ToolMessage` is returned to the backend. Calls and results remain in
the standard tick trajectory for strict database replay and scoring. Native
delegation, permissions and task progress are diagnostic Gateway events, not
retail tool calls. Only the actual user simulator's replies can approve work.

Per-call retries and hallucination retries are disabled for this provider.
Infrastructure failures remain failed trials instead of being replaced by a
successful retry. The bridge control connection is never reconnected or replayed. Recoverable
notifications from the original Agent stay diagnostic so its own recovery can
continue; explicitly blocked voice connections and broken control transport
still fail the trial.

## Timing and transcript interpretation

Media runs on the wall clock in 20 ms frames with 200 ms evaluation ticks by
default. Backend execution and MCP waiting never suspend audio transport.
The example permits 1200 s of queued output (about 9.6 MB of telephony audio).
This is queue capacity, not a conversation-duration limit; fast provider
generation and long replies do not prematurely exhaust a small queue. Playback remains
paced. Playback receipts reflect the capped audio actually delivered to the simulator,
including cancellation of buffered audio when Qwen requests it.

The observer transcribes **only played Agent audio**, using its complete silence
clock to locate speech segments. Gateway captions and backend results cannot disclose future speech to
the user simulator. Finalized ASR fragments and their audio timestamps are stored
as `recorded_audio` evidence; the observation setup is recorded in
`agent_system_metadata`. ASR finalization can delay the simulated user's semantic
reaction. It does not change the measured physical Agent playback timeline;
comparisons must use the same observer setup and report its latency.

The Gateway does not expose a complete billable usage ledger for both models.
Agent cost is therefore recorded as unknown, rather than zero; observer ASR and
user-simulator service charges are separate evaluation costs.

These are custom full-system evaluations. Do not label them official voice-v2
scores without separately validating its released frame, configuration and
submission requirements. Model task failures are valid observations, not adapter
test failures.

## Verification

```bash
python3 integrations/qwen_audio_agent/verify_platform.py
npm ci --ignore-scripts --prefix integrations/qwen_audio_agent
node --test integrations/qwen_audio_agent/test/*.test.mjs
PYTHONNOUSERSITE=True UV_FROZEN=1 uv run pytest tests/test_voice/test_audio_native/test_qwen_agent.py
```

The key-free contracts cover media during tool waiting, one-time execution,
playback capping and interruption, hidden future captions, isolated configuration,
protocol failure cleanup and MCP discovery. They do not measure model accuracy.

The build script additionally runs all image contracts under the actual platform
UID and required setup options after Docker build. These exercise the archived
Qwen GCP SDK/parser, official MCP client, native Qwen Code connectivity and a
complete Gateway → ACP → Qwen Code → MCP → spoken response flow with local
deterministic model endpoints. No model credentials or paid services are used
for these contracts.

The normal provider suite can explicitly include the complete system:

```bash
QWEN_AGENT_TEST_ENABLED=1 TAU2_QWEN_AGENT_CONFIG=/absolute/path/to/config.json \
  PYTHONNOUSERSITE=True UV_FROZEN=1 uv run pytest tests/test_voice/test_audio_native/test_provider_suite.py -k qwen_agent
```

Live provider tests and the real five-task smoke run require the configured
services and incur their normal API costs. Diagnostics for changing requirements,
denying approval and cancelling pending work should be reviewed separately from
the frozen task scores.

Real-service results and preserved attempts: [SMOKE_RESULTS.md](SMOKE_RESULTS.md).

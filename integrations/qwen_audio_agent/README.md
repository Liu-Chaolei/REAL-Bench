# Evaluate the complete Qwen Audio Agent with τ-Voice

`qwen_agent` connects the real Gateway, native task runtime and Qwen Code ACP
backend to τ-Voice. It is separate from `qwen`, which calls a DashScope model
directly. The Qwen checkout is archived at its clean git revision and is never
edited by the integration.

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

## Configure

### Speech-to-Speech frontend

The completed one-task connectivity check is documented in
[SPEECH_TO_SPEECH_RESULTS.md](SPEECH_TO_SPEECH_RESULTS.md).
The subsequent three-task run without a duration cap, including the confirmed
user turn scheduling issue, is documented in
[UNLIMITED_THREE_RESULTS.md](UNLIMITED_THREE_RESULTS.md).
The `v1.2` evaluator scheduling fix and its one-task acceptance result are in
[TURN_TAKING_FIX_RESULTS.md](TURN_TAKING_FIX_RESULTS.md).

Use `config.speech-to-speech.example.json` to evaluate the complete Agent with
**MiMo ASR → MiMo LLM → MiMo TTS**. The frontend reads `MIMO_API_KEY`,
`MIMO_BASE_URL` and `MIMO_MODEL` from the same private dotenv file. The current
frontend model is `mimo-v2.6-pro`, using Responses streaming. The simulated caller
uses the same profile through Chat Completions, including its behavior-decision
model. `smoke.py` defaults to this speech-to-speech configuration and keeps caller
credentials separate from judge credentials. The configs store only environment
references via `user_llm_api_key_env`, `user_llm_base_url_env` and
`user_llm_model_env`; resolved secrets are not added to the generated Agent config.
The native Qwen Code backend separately retains `GAIA2_BACKEND_*` (MiniMax).
Frontend and evaluation-side observer ASR both use `mimo-v2.5-asr` in the
supplied configurations. Automatic listener hotwords are currently disabled;
the optional Qwen listener implementation and its A/B evidence remain documented
in [USER_ASR_HOTWORDS.md](USER_ASR_HOTWORDS.md).

The supplied speech and Gaia2 configurations use MiMo directly. Optional
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

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --config integrations/qwen_audio_agent/config.speech-to-speech.example.json \
  --task-ids 0 1 2 --max-seconds 0 --save-to qwen-s2s-three
```

The optional `speech_to_speech` block launches stock Hugging Face
`speech-to-speech==1.1.0` and a local audio translation service inside each
trial's existing filesystem boundary. Both are ready before the native Gateway
connects, listen on container loopback, and stop with that trial. No extra host
mount or persistent service is needed. Frontend tools, delegation, permissions
and MCP results follow the native Gateway paths. The upstream package is not
patched and no tau package, task collection or evaluation database is installed
in the image.

The selected frontend uses Responses API streaming with reasoning `low`.
Frontend ASR uses `mimo-v2.5-asr`, TTS uses
`mimo-v2.5-tts` with voice `Dean`. Frontend ASR receives completed utterances as
24 kHz WAV. The optional Qwen frontend implementation remains available to custom
configurations; the supplied configurations select MiMo for the Agent.
TTS synthesizes one sentence at a time and returns PCM audio; cancellation
closes the pending local/provider request and the stock pipeline discards stale
audio. Uploading audio is not a streaming ASR API, and real synthesis/recognition
latency is included in the call.

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

### Contextual later-stage requests

Gaia2 Qwen configs can opt into contextual rendering for the second and third
fixed scenario requests by adding this block to the Qwen Agent config:

```json
"dynamic_followup": {
  "enabled": true,
  "stages": [1, 2],
  "max_context_characters": 24000,
  "max_retries": 1
}
```

The stage numbers are internal zero-based indices: `[1, 2]` means the second
and third user tasks, while the first request remains fixed. For later requests,
the context classifier first receives only the preceding user/agent dialogue
and returns structured `status` and `affect` labels. This describes what the
caller believes happened, rather than objective tool correctness. The caller
model then receives the immutable stage request, preceding conversation, and
those labels, and produces one structured contextual utterance. A second
structured check compares the source request with the generated wording and
receives the stage's reference tool-call GT; internal IDs in that GT are
evidence for the checker and are not required in the caller's wording. Values
found only in tool results or the GT, such as answers, addresses, contact
details and database IDs, are forbidden in the generated request unless the
source request already states them. The utterance may adjust its tone and
transition wording to the caller's perceived status and affect, while
preserving the same business goal. If an earlier stage failed or remains
incomplete, the utterance moves directly to the current request and does not
reopen or repair the earlier stage. If the source request uses a cross-stage
reference such as “that same chat”, “this contact”, or “those listings”, the
generated wording must preserve the reference to the same antecedent. The
source request remains the scoring goal; unsafe or incomplete renderings fall
back to the original fixed text. If context classification is unavailable,
the runtime uses the deterministic heuristic fallback. The accepted rendering,
context labels and check result are recorded with the stage and reused when
replaying the run. The default is disabled.

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

The configured frontend is `qwen-audio-3.0-realtime-plus`/DashScope; the backend
is Qwen Code `0.26.0` using `MiniMax-M3` with reasoning effort `low`. An `openai/`
routing prefix is removed from an explicitly mapped model name. Reasoning uses
native generation settings and the compatible endpoint's `reasoning_effort`
request parameter; the complete-system contract checks the actual request.

The supplied configuration selects **MiMo caller TTS and MiMo listener ASR**:

- Caller TTS: `mimo-v2.5-tts`, using `MIMO_API_KEY` from that same private file.
  English voices are `Dean` and `Chloe`, selected by persona; no ElevenLabs voice
  IDs are needed. The exact caller text is synthesized without text optimization.
- Observer ASR: `qwen-audio-3.1-asr-flash`, using `DASHSCOPE_API_KEY3` and the
  configured DashScope regional host. It consumes only actually played Agent
  audio, normalized to 16 kHz WAV. Per-task hotwords are prepared before the
  conversation starts. Recognition uses the shared asynchronous segment and
  bounded-review lifecycle (4 s maximum or 0.6 s trailing silence), with measured
  segment timestamps. Actual vocabulary, provider usage and release latency are
  saved in provenance. Legacy MiMo/Deepgram observer configurations remain valid.
- Native Agent credentials: the example explicitly selects `DASHSCOPE_API_KEY3`
  for Qwen Realtime. Backend variable references stay pinned to
  `GAIA2_BACKEND_*`, separating them from evaluator credentials.

In the Realtime configuration, MiMo keys remain on the evaluator side. The
Speech-to-Speech configuration additionally supplies a MiMo key to its Agent
frontend. The generic defaults retain ElevenLabs/Deepgram for other runs; this
custom integration selects MiMo through `tts` and `asr_provider` settings.

The active `config.gaia2.realtime.dynamic.metrics.deepseek-user.json` profile is
the Qwen-TTS variant: caller TTS uses DashScope `qwen-audio-3.1-tts-flash` and
the English voices `Andy_v3.1`/`Abby_v3.1`, sharing `QWEN_API_KEY` and
`QWEN_BASE_URL` with the Qwen observer ASR. The legacy MiMo caller profiles
remain available when their configs explicitly select `mimo`.

The simulated caller waits at least 30 seconds of silence before starting an
unsolicited check-in. Fresh Agent speech or text, initial requests and tool replies
retain their ordinary response timing. `QwenAgentConfig.user_silence_checkin_seconds`
sets this interval (0 restores the previous pacing); the effective value is saved
in the run configuration. Caller generation and synthesis add to this minimum.

Scenario callers retain free-text generation. An ungrounded reply is corrected
privately using its reason and the original request, with at most two additional
attempts. Rejected replies are never spoken or committed to conversation history.
Known facts and released scenario inputs are authoritative; Agent statements do
not authorize changed prices, quantities or invented deadlines. Recovery attempts
and costs are retained in `candidate_recovery` diagnostics and task logs.

The MiMo observer can review already-heard audio on a grounding conflict involving
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
estimates for MiniMax/MiMo.

Official API references:
[MiMo TTS](https://mimo.mi.com/static/docs/api/audio/tts.md) and
[MiMo ASR](https://mimo.mi.com/static/docs/api/audio/Speech-Recognition.md), plus
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
integrations/qwen_audio_agent/run_hpc.sh smoke --num-tasks 3 --max-seconds 0
```

Use `--dry-run` for a preview, `--num-tasks 1` for one call, or `--task-ids 0 1 2`
for explicit tasks. Each call uses a fresh isolated Agent. The helper retains
recordings, played-audio transcripts, bridge traces, tool trajectories and scores
under `data/simulations/qwen-mimo-smoke-<timestamp>/`. It exits nonzero for
infrastructure errors, preserves every attempt and refuses to overwrite results.
The helper defaults to `--max-seconds 0`: no conversation-duration cap and no
whole-simulation wallclock timeout. Agent/user stop signals and infrastructure
failures still end a trial. Ordinary `tau2 run` defaults are unchanged; its
`--max-steps-seconds 0` explicitly disables the voice tick limit. Results record
`audio_native_config.max_steps_seconds=0` and `info.max_steps=null` for this mode.
Use a positive `--max-seconds N` for a bounded smoke check; that also sets a
wallclock timeout of `N + 150` seconds. The historical 240 s run above used such
a short connectivity budget.

To continue an interrupted batch, repeat its configuration, task selection, seed
and `--save-to` name with `--resume`. Completed task/trial pairs are read from
the checkpoint and skipped; unfinished pairs start fresh isolated calls. This
resumes the batch, not an interrupted voice conversation.

For ordinary CLI use, `run_hpc.sh run` accepts all tau arguments. The JSON config
contains the MiMo choices; configure evaluator LLM credentials separately or use
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

### Streaming Qwen observer

`config.gaia2.realtime.dynamic.metrics.deepseek-user.json` selects the streaming
observer with the following evaluation-side settings:

```json
{
  "asr_provider": "qwen",
  "asr_model": "qwen-audio-3.1-asr-flash-streaming",
  "asr_api_key_env": "QWEN_API_KEY",
  "asr_base_url_env": "QWEN_BASE_URL",
  "asr_append_seconds": 2.0,
  "playback_sample_rate": 24000
}
```

The agent's native PCM16 output is played, recorded and sent to this observer at
its original rate (24 kHz in the supplied configuration), without mu-law encoding
or resampling. `playback_sample_rate` must match the actual source events; a
mismatch raises an error rather than silently converting the audio. Caller input
retains its independently configured format. A 200 ms tick carries 9600 bytes of
24 kHz PCM16 output and 1600 bytes of 8 kHz mu-law caller input: both represent
the same duration.

The observer starts a WebSocket task when played speech begins. It buffers at
most two seconds of audio before each append. Only the audio that remains after
playback capping and cancellation is sent, so buffered future speech never enters
the ASR input. After 0.6 seconds of played silence, a playback interruption,
or shutdown, it flushes the short remaining batch and sends `finish-task`. It
receives the final results before closing the connection. Tool work without
speech does not keep an ASR connection open.

Only the contiguous prefix of sentences marked `sentence_end=true` becomes
visible. Confirmed sentences include all words; there is no three-word holdback.
Two seconds is an audio batching limit, not a guarantee of final text every two
seconds. The implementation follows the
[Qwen streaming WebSocket protocol](https://www.alibabacloud.com/help/en/model-studio/qwen-audio-asr-streaming-websocket-api).

Every tick stores the complete confirmed text and its version in
`live_transcript.snapshot`. Newly confirmed fragments retain unique IDs and
played-audio timestamps. Caller LLM history consumes each fragment once at its
actual release tick, so cumulative snapshots do not repeat previous text or
rewrite older observations. Grading continues to use the recorded fragments and
their audio alignment. Observer provenance includes session boundaries, appends,
intermediate hypotheses, confirmed versions and usage. Existing file-ASR profiles
remain available with `qwen-audio-3.1-asr-flash`.

### Fixed stage hotwords for the user-side observer

The current Gaia2 Realtime configuration enables the catalog extracted with
DeepSeek for all 1100 task IDs (including the 100 async variants):

```json
{
  "asr_hotwords": {
    "enabled": true,
    "mode": "gaia2_stage_gt",
    "weight": 50,
    "max_terms": 2000
  }
}
```

The catalog defaults to `data/gaia2_stage_hotwords/v2` under `TAU2_DATA_DIR`.
`asr_hotwords.catalog_dir` can explicitly select another catalog. This mode
requires English Gaia2 and the Qwen streaming observer. Before Agent startup,
the loader checks the task ID, source/GT fingerprint, stage-local citations,
vocabulary hashes and cumulative activation. Missing or stale entries fail
preparation. It loads the saved extraction; no extraction LLM or environment
entity scan runs during the conversation. The older `user_background` mode
retains its previous call-level extraction behavior.

Stage zero enables its own words. When the next scripted request starts actual
speech transport to the Agent, the observer enables the union of all words up to
that stage. Merely queuing/generating the next request does not activate its
words. A leading silent metadata chunk waits for speech before activation. If
an older ASR episode is still active, its audio is flushed and finished; the next
episode uses the new stage. Every queued session freezes its own vocabulary at
creation, so connection/recognition delays cannot apply future words to old
audio. Rechecks use the vocabulary associated with the original fragments.

Version 2 reviews the earlier vocabulary and adds difficult names from recorded
reference tool responses. The first 50 cumulative phrases use weight 50,
regardless of source; only subsequent additions use weight 5. Weights remain
fixed after activation. The catalog embeds the cited textual evidence and pins
each profile hash, so runtime loading needs neither the large GT dump nor a new
model request. Version 1 remains available via an explicit `catalog_dir`.

The total limit is 2000 phrases; weight 50 supports at most 50 super hotwords per request. Empty
stage vocabularies are valid. For task 0007, the active words are `Lysverket` at
stage zero, then `Lysverket` plus `Kaida Rao`, then those names plus `Åse`.

These words go only to evaluation-side observer ASR, not to the frontend or
backend Agent prompt, tools or container environment. `observer_hotwords.json`
records the selected task's stage catalog. Observer provenance also records
activation times, each ASR session's stage, actual vocabulary and its hash.
See [the catalog](../../data/gaia2_stage_hotwords/v2/README.md).

The output tap `agent_output.wav` retains native PCM. When exporting a combined
stereo recording, only the lower-rate channel is upsampled to the higher rate;
the native agent channel remains unchanged. Existing saved 8 kHz recordings
cannot recover details already removed by downsampling.

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

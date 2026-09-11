> For the trustworthy evaluation workflow, use [protocol v3](QWEN_AUDIO_PROTOCOL.md)
> and `evaluation_campaign.py`. The initial smoke instructions/results below
> describe the prototype. Current v3 uses the configured OpenAI Responses backend;
> the Ali-only backend profile remains historical prototype configuration.

# GAIA2 with Qwen Audio Agent

This variant evaluates the complete Qwen speech frontend, its task coordinator,
and OpenCode backend. External WAVs carry user instructions; official ARE 1.2.0
records app actions and grades them. Scores are labelled `gaia2-qwen-audio`, not
claimed equivalent to the original text-agent baseline.

## Inputs and setup

Use Python 3.12+ and Docker with Compose and Buildx. Harbor uses Buildx to build
the separate verifier network helper, even when Compose uses the classic builder. Install Harbor with
`uv sync --no-dev --extra huggingface`. The task images install Node 24.19.0,
OpenCode 1.18.5, and ARE 1.2.0. Initial image builds download the official
`gaia2_filesystem` snapshot; scenario Parquet files alone do not contain it.

The Qwen checkout must include the optional evaluation bridge in
`server/src/core/evaluation.mjs`. The generator stages the local source (including
uncommitted changes), excluding node_modules, runtime directories, and .env files.
Generated tasks refer to this staged directory by absolute path: regenerate after
moving the dataset or changing the Qwen checkout. Source revision and content hash
are recorded in the staged image.

Audio names follow `<audio-dir>/<config>/<source_id>.wav`. Files must be valid,
nonempty PCM WAVs. Mono 16 kHz PCM16 assets are copied unchanged; other PCM WAVs
require `ffmpeg` on the generation host. No TTS or extra speech recognition service
is called. Alternatively, pass `--audio-manifest` with JSONL rows:

```json
{"config":"search","source_id":"0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm","audio_path":"search/0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm.wav"}
```

Paths in a manifest are relative to its directory. An optional `event_id` must
match the scenario's single user event. Multiple user messages are currently
excluded even when audio exists. Missing and malformed assets are reported, never
replaced by text.

## Generate the smoke dataset

From the Harbor repository root:

```bash
uv run --no-dev python adapters/gaia2/run_adapter.py \
  --qwen-audio \
  --dataset-dir /shared/chaolei.liu/Audio-agent/gaia2 \
  --audio-dir /shared/chaolei.liu/Audio-agent/wav_export \
  --qwen-agent-dir /shared/chaolei.liu/Audio-agent/qwen-audio-agent \
  --smoke --output-dir datasets/gaia2-qwen-audio-smoke
```

Add `--preflight` to write only coverage and indexes, without packaging tasks.
`coverage.json`, `audio-index.jsonl`, `selected-ids.txt`, and `smoke-ids.txt` record
selection and exclusions. `--smoke` chooses the shortest valid audio in each
selected category (ties sorted by source ID); preserve `smoke-ids.txt` and use
`--ids-file` for fixed reproduction if more files arrive later. Use `--overwrite`
to refresh existing generated task templates after code changes.

The inspected local export contains 633 eligible WAVs: execution 88, search 137,
adaptability 151, time 142, ambiguity 115. The fixed initial smoke IDs are:

```text
0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm
0324_saa8qnpcx8r5eq5x4izam1tzn4c8z89m
0520_vs2mfw4rol4q163x0lzp2f907btnnsmk
0645_gbj8otuliajhbbuncqqie6i64yv36egn
0770_wr11p4jja84co11mmhp640n3cvz64etj
```

For the first single-task run, generate with `--configs search --smoke` into a
separate output directory and adjust the job's dataset path accordingly.

## Configure models and run

### Current mixed profile: Ali speech, GPT backend and judge

The job currently selects `~/.config/qwaudio/gaia2-evaluation-ali-speech-gpt.env`.
The speech frontend remains `qwen-audio-3.0-realtime-plus`. The backend and judge
use `gpt-6-astra` through the service configured in the supplied Codex config.
That service rejects the shorthand `gpt-6`; its listed and tested model ID is
`gpt-6-astra`. The provided Codex files are read as credential/endpoint sources,
not modified.

The requested **Light** setting is the API's **`low`** reasoning effort. The
OpenCode model options set `reasoningEffort=low`; its auxiliary `small_model`
also uses GPT-6 Astra so automatic title generation does not silently choose a
different model. The judge uses `GAIA2_JUDGE_API=responses` and
`GAIA2_JUDGE_REASONING_EFFORT=low`. Only its transport changes: official ARE
checker prompts, graph/causality checks and online validation remain in use.

Use `qwen-audio-ali-speech-gpt.env.example` to reproduce this profile. Declare
credential/base URL variables before the JSON field because dotenv interpolation
is evaluated in file order. Keep the job's `model_name` consistent with the private
backend model. GPT-6 tool calling requires the Responses API; the backend uses
OpenCode's native `@ai-sdk/openai` provider rather than the chat-only compatible
provider.

References: [OpenAI model effort labels](https://developers.openai.com/codex/models),
[GPT-6 API guidance](https://developers.openai.com/api/docs/guides/latest-model).

### Other provider profiles

For an entirely non-Ali deployment, point the job to the private profile
`~/.config/qwaudio/gaia2-evaluation-non-ali.env`. Start from
`qwen-audio-non-ali.env.example` and configure the three services independently:

- Speech: set `QWEN_AUDIO_REALTIME_PROVIDER=speech-to-speech` and
  `SPEECH_TO_SPEECH_REALTIME_URL` to a running compatible speech-to-speech server.
  Its ASR, LLM and TTS can all run locally. Optional proxy authentication uses
  `SPEECH_TO_SPEECH_AUTH_TOKEN`. Container-reachable addresses are required;
  `localhost` inside a container does not refer to the host machine.
- Backend: use an explicit OpenCode `provider/model` name. Configure custom
  endpoints/models with `OPENCODE_CONFIG_CONTENT`; the example demonstrates a
  compatible API and `${GAIA2_BACKEND_API_KEY}` interpolation in the private file.
  Explicit providers are preserved instead of being rewritten to Alibaba. Bare
  model names retain the original Alibaba default for backward compatibility.
- Judge: set `GAIA2_JUDGE_MODEL`, `GAIA2_JUDGE_ENDPOINT` and its credential.
  `GAIA2_JUDGE_PROVIDER=openai` selects the compatible protocol adapter; the
  endpoint determines the actual service. An unauthenticated local endpoint can
  use a nonempty SDK placeholder such as `EMPTY` instead of a real API key.

The wrapper does not require a DashScope key for the speech-to-speech profile
with an explicit non-Ali backend. The speech endpoint must support realtime
streaming and function calling; a text-completions API alone cannot replace it.
Other cloud speech protocols need their own frontend profile.

The legacy DashScope profile remains available in `qwen-audio.env.example` for
reproducing earlier runs. Select it explicitly with the job's `env_file` if needed.
Private configuration is read by the host wrapper and never baked into images.
Agent `extra_env` overrides the private file, which overrides host values.

```bash
DOCKER_BUILDKIT=0 PYTHONPATH=. uv run --no-dev harbor run -c adapters/gaia2/gaia2-qwen-audio.yaml
```

The command above reuses the classic-builder cache on the current host; Buildx
must still be installed for Harbor network helpers. Omit `DOCKER_BUILDKIT=0` on
hosts with a working BuildKit download path. `PYTHONPATH=.` makes the repository-local
custom agent importable from the installed Harbor console entry point.

The default job uses one concurrent trial, one attempt and zero retries. The
execution budget is 2400 seconds; startup, official oracle preprocessing and
judging have additional overhead. Backend permissions are enabled inside the
isolated container so unattended tests do not wait for human tool approvals.

## Measurement and outputs

The runner sends 20 ms audio frames at real speed and adds 1.5 seconds of silence
for VAD. It simulates output playback at the provider's sample rate and supplies
playback receipts; no microphone or speaker device is required. Only fully played
responses with frontend transcripts become ARE user-facing actions. This measures
input speech understanding and system behavior, but does not independently verify
that the synthesized output acoustically matches its transcript.

All spoken acknowledgements, progress updates and final replies are retained.
Official turn validation can stop a scenario early after an incorrect or premature
reply; this is not suppressed to improve the score. Cancelled/unplayed outputs are
saved for diagnosis, not credited as completed answers.

User text cannot be retrieved via AgentUserInterface MCP tools. The app tools keep
the official wrappers, augmentation, exception metadata and event times. Environment
notifications are delivered as environment context, not as extra user text. ARE's
clock drives business dates and reminders; network timeouts remain wall-clock based.
Every trial has independent Qwen memory, backend sessions and task state.

The ARE service retains `result.json`, official traces, `gateway.jsonl` and numbered
output WAVs. Harbor collects `/evaluation/results` from that service and grades in
a separate verifier container. A copy of the decision is stored in the agent log
folder as `gaia2_qwen_audio_result.json`.

```bash
uv run --no-dev python adapters/gaia2/summarize_audio.py jobs/gaia2-qwen-audio-smoke \
  --coverage datasets/gaia2-qwen-audio-smoke/coverage.json \
  --output /tmp/gaia2-qwen-audio-summary.json
```

Reports include selected/executed counts, infrastructure failures, per-category
results, and both selected-set and successfully-graded denominators. Missing audio
is a coverage gap; unavailable services/judge errors are infrastructure failures;
execution timeouts receive zero reward. No whole-trial automatic retry is used.

## Validation

Run `uv run pytest tests/unit/adapters/test_gaia2_audio.py` for asset, packaging,
playback, completion and text-isolation tests. Qwen adds `server/test/evaluation.test.mjs`
and retains its normal server test suite.

The integration script `tests/integration/adapters/gaia2_official_runtime.py` runs
in the pinned ARE environment. Set `GAIA2_RUNNER_PATH` to the runner source directory;
optionally set `GAIA2_SCENARIO_PATH` to a packaged scenario and `DEMO_FS_PATH` to its
filesystem snapshot. It uses mocked semantic judge responses and real ARE/MCP APIs,
so it does not call paid models. Real-model smoke validation requires the private
model configuration above.

An additional offline transport check uses the actual Qwen Gateway and an existing
WAV, with a deterministic local speech-provider mock and the backend disabled:

```bash
uv run --no-dev python adapters/gaia2/run_mock_audio_smoke.py \
  --audio /shared/chaolei.liu/Audio-agent/wav_export/search/0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm.wav \
  --output-dir /tmp/gaia2-mock-audio-smoke
```

This helper expects locally built `hearme-qwen-audio:dev` and
`hearme-gaia2-are:dev` images (override with `--qwen-image`/`--are-image`). Build the
first from the generated `.qwen-source` directory using its `Dockerfile.harbor`,
and the second from `qwen_audio_template/environment/runner`. It creates an
isolated internal Docker network, verifies the PCM hash at the provider boundary,
checks transcript delivery and playback receipts, and removes its containers and
network afterward. Its output is a transport test result, not a GAIA2 model score.

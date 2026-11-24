# Gaia2 tasks in τ-bench

> 当前 Gaia2 整题评分仅使用环境组件；旧 ACTION/NL/COMMUNICATE 不再计算。必要信息覆盖由 conciseness 的 required_coverage 单独评价，后台 reply_reward 保留。阶段交付只记日志，不因 missing_requests 直接清零。下文历史验证记录保留原口径；当前指标见 [metrics-inventory.md](metrics-inventory.md)。

The `gaia2` domain runs 800 core Gaia2 scenarios and 200 request variants through
the ordinary τ task loader, environment, user simulator, audio adapter,
orchestrator and evaluator. The complete local population contains 1,000 tasks;
fixed async variants have been removed. Async interaction is generated at runtime
through `active_metrics.persistent_task`, independently of dataset task count;
the original Parquet import contains 800.
An additional delta dataset exposes 100 AddTask, 50 Modify and 50 Cancel variants
through independent splits, bringing the loaded `all` split to 1,200 when present.
The canonical `tasks.json` retains 1,000 records. See the
[interaction variant plan and implementation](modify-cancel-addtask-plan.md) and
[data layout and commands](../data/tau2/domains/gaia2/README.md).
It supports the existing Qwen Audio Agent integration in English, including
parallel calls with independent containers, tool state and scene clocks.
ARE agents, environment loops and judges are not used. No Gaia2
Docker service is required; Qwen itself retains its normal isolated container.

These are Gaia2-derived tasks with the versioned `gaia2-tau-v1` reward definition.
They are not original Gaia2 leaderboard scores.

## User role

Gaia2 text and voice users use `gaia2-neutral-user-v1`, a neutral human who
delegates the current task to an AI agent, following ARE's default user-proxy
role. The builders select domain-owned guidelines and a neutral voice reminder;
the service-customer persona and its escalation language are not injected.
Language, grammatical gender, verbosity, spelling guidance and the existing
task-information boundaries are retained. Voice identity and timing parameters
continue to come from the selected voice configuration.

This role adds no application-access knowledge or blanket permissions, and does
not instruct the user to guide the agent's tool use. Existing stage transitions,
dynamic follow-ups and scoring retain their own behavior. The prompt is selected
at construction without new runtime checks or model calls. The role version is
included in the saved user guidelines; historical runs retain their old prompts.
See the [implementation record](gaia2-neutral-user-results.md).

## Task structure

The local canonical data now combines each original question and its two authored
continuations in `data/tau2/domains/gaia2/tasks.json`, retaining the 800 original
task IDs. The additional `mind_change` and `repetition` categories each contain
100 tasks. They rewrite one request while preserving the original final objective,
reference execution and scoring. `base` selects 800 core tasks and `all` selects
all 1,000 tasks. There is no fixed `async` split or ten-task persistent sidecar. See [request variant authoring](gaia2-variants.md).
Each private `scenario.stages` entry holds the request, event index,
evidence, outcome checks and criteria; operations remain in `scenario.events`.
Generation attempts, review logs and acceptance fingerprints are archived outside
the domain. Runtime-required resource and task integrity pins remain.

**Continuous runtime:** multi-stage tasks use the ordinary τ caller STOP signal
and the caller out-of-scope signal as the same stage-advance control. The first
two such signals release the next request through the existing TTS/audio path;
the signal on the final stage ends the call. One Qwen session, application state
and scene clock persist across the three requests. Agent STOP and infrastructure
failures retain their terminal handling.

The whole conversation is scored after it ends. ENV checks the cumulative final
business state; COMMUNICATE uses each request's actual playback window; NL checks
receive stage boundaries with the recorded conversation and application evidence.
There are no separate stage scores or runtime correctness decisions. Authoring
`checks` remain part of offline reference verification.

Run `tau2 gaia2 verify --reference --workers 16 --seal` after runtime changes.
The manifest's `verified`, `execution_ready` and series runtime version are
written by verification, not by manual edits. This authorizes a live pilot;
it does not assert model accuracy. Historical original data and authoring
material remain under `data/archives/gaia2-authoring-20261106-integrated/`.

The frozen ten-task pilot can be launched through the existing evaluator with:

```bash
.venv/bin/python scripts/run_gaia2_series_smoke.py \
  --config integrations/qwen_audio_agent/config.gaia2.deepseek.json \
  --save-to gaia2-series-pilot-deepseek-NEW
```

The DeepSeek profile maps the Agent backend, Speech-to-Speech frontend and caller
simulator to `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` and `DEEPSEEK_MODEL` from the
configured environment file. ASR and TTS remain on the configured MIMO services.
The judge profile remains independent.

The launcher runs task 0353 first and then the other nine, with one trial per task,
seed 300, concurrency one and no automatic retries. Each task has its own output
directory. A business failure continues to the next task; an infrastructure,
user-simulator or grading error stops admission and is preserved in `report.json`.
The default 1,200-second conversation budget is shared across all three requests.

## Prepare data

Install Python 3.12 with the `gaia2`, `voice` and `dev` extras. Keep `UV_FROZEN=1`
for ordinary commands. Optional Gaia2 dependencies are imported lazily so core
domains remain usable without them.

```bash
UV_FROZEN=1 uv sync --extra gaia2 --extra voice --extra dev
```

Obtain the original Parquet configurations and the separate
[`gaia2_filesystem` resource dataset](https://huggingface.co/datasets/meta-agents-research-environments/gaia2_filesystem).
The resource root must contain `demo_filesystem/`. Download it completely before
starting evaluation; the importer never substitutes empty content for a missing
required resource.

The pinned resource provenance is recorded in
[`resources.source.json`](../data/tau2/domains/gaia2/resources.source.json).
Four attachments referenced by source snapshots are absent from that published
filesystem revision. This import supplements them from the public
`bytebiss/gaia-attachments` mirror, verifying every byte against the original
`gaia-benchmark/GAIA` Git/LFS identity. The exact revisions, download paths,
original identities and destination paths are recorded in
[`resources.supplemental.json`](../data/tau2/domains/gaia2/resources.supplemental.json).
The filename containing ` (1)` is restored from the matching original JPEG.
When recreating resources, preserve those destination paths and verify the
recorded hashes before sealing. Assets are local inputs and are not committed.

```bash
tau2 gaia2 import --source /path/to/gaia2 --assets /path/to/gaia2_filesystem
tau2 gaia2 verify --reference --workers 8 --seal
tau2 check-data
```

Import converts the five configurations deterministically, preserves source IDs,
deduplicates application snapshots, validates typed arguments and dependencies,
and publishes a separate English registry extension through the language factory.
An original import's `base` contains all 800 tasks; `search`, `execution`,
`ambiguity`, `adaptability` and `time` contain 160 each. After variant authoring
and merge, `base` contains 1,000 tasks and `mind_change` and `repetition` contain
100 each; the original category membership is preserved. `mini` and `demo` are
not added to the core population. The original
Parquet files and released τ language packs and benchmark manifests remain intact.

Reference verification invokes the same application operations as live tools,
checks their resulting business state, and strictly replays every complete
journal. It makes no model calls. Sealing records the runtime, resource and task
hashes. Changed runtime code or resource bytes require verification again before
model startup. Loading also verifies the sealed task bytes and rejects substituted
task objects before model startup. Import changed task definitions into a new
output directory to preserve a sealed import; the
`--replace-incomplete` option only replaces an unsealed conversion.

## Run the complete Qwen Audio Agent

Build the integration image with the updated bridge. The source checkout must be
clean; the builder archives it without modifying Qwen's source or configuration.

```bash
python integrations/qwen_audio_agent/build_image.py /path/to/qwen-audio-agent \
  --tag docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.2
```

Use `integrations/qwen_audio_agent/config.gaia2.example.json` as the configuration
template. It uses the same MiMo ASR → configured MiniMax frontend → MiMo TTS
pipeline and native Qwen Code backend as the retail integration. The image must
declare the `gaia2` domain capability; historical retail images remain usable for
retail. Credentials stay environment-variable references.

The simulated caller independently hears the Agent through Qwen ASR with
automatically prepared per-task hotwords. Its source material is the initial
user request and personal application entities; scoring answers and reference
actions are excluded. See the
[listener ASR guide](../integrations/qwen_audio_agent/USER_ASR_HOTWORDS.md).

```bash
tau2 run --benchmark none --domain gaia2 --language en \
  --audio-native --audio-native-provider qwen_agent \
  --qwen-agent-config integrations/qwen_audio_agent/config.gaia2.example.json \
  --max-concurrency 5 --task-split-name search --num-tasks 5 \
  --max-steps-seconds 1200 \
  --save-to gaia2-search-example --verbose-logs --audio-taps
```

On this HPC host, the existing launcher supplies the native audio library path.
Its smoke helper additionally resolves the existing private user/judge profiles;
it still launches the same `tau2 run` command for either domain:

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.example.json \
  --task-ids <source-id> --max-seconds 1200 --max-concurrency 5 \
  --seed 300 --save-to gaia2-smoke-example
```

The ordinary run's timeout settings remain authoritative. The smoke helper's
`--max-seconds 0` explicitly disables its conversation and wall-clock limits.
Neither path automatically retries a failed call. Results use the existing
`data/simulations/` layout and `tau2 view`.

## Interaction, time and reward

The original initial request is synthesized verbatim through the standard caller
TTS and effects pipeline. Later replies use the same τ listening, generation,
interruption and playback logic. The caller receives only original/released user
facts, permitted self-information and actually heard agent speech. Free replies
are checked for invented facts and task changes before synthesis. A violation is
reported as a user-simulator error, separately from a valid agent task failure.
The caller may refer to agent-reported observations and ask for status, timing
or help with the same request; these do not create new personal preferences.
Infrastructure and user-simulator errors are counted separately and excluded
from agent accuracy and pass^k metrics; ordinary agent failures still count.
The scenario with two source user messages releases the second only after the
corresponding clarification is delivered. Its contents are not included early in
the user prompt.

Grounding checks only unsupported personal facts/preferences and material changes
to the requested business objective. Requests to review a draft, correct known
details, ask for status or give conditional approval are permitted. Procedural
consistency and task completion remain the responsibility of the ordinary
conversation and task evaluators.

A shared scenario clock starts with initial input delivery, excludes setup, and
advances at real speed during inference, TTS, audio playback and tool waits.
Source user events bind when their utterance starts arriving at the agent; the
completed or interrupted speech is recorded separately. Prepared TTS is not
treated as delivered input.
Known application events run from their declared dependencies and delays.
Generated entity references bind to actual operation returns. Notification waits
are bounded to 60 seconds per call; longer monitoring repeats this shared tool.
Configure the Qwen MCP timeout above that bound plus five seconds.
Notification waits
use the common asynchronous tool executor, so audio ticks continue. The system
records operation start/completion, external effects, actual speech evidence and
semantic speech-anchor decisions. Causal speech matching uses the configured τ
communication judge asynchronously; replay uses its recorded decisions without
calling a model again.
Replay restores notification arrivals and reproduces exactly the batch consumed
by each wait, preserving updates that arrived after that wait returned.

Rewards multiply the task's declared τ components:

- `ENV_ASSERTION`: target business effects, protected unrelated state and file
  paths/content. Generated IDs are compared by entity structure while preserving
  relationships and multiplicity; read markers and log timestamps do not gate
  the reward.
- `COMMUNICATE`: required answers checked against the actually delivered transcript
  with the configured semantic communication judge.
- `NL_ASSERTION`: explicit timing, conditional, prohibition, clarification, budget
  and selection requirements, using both the conversation and recorded execution
  evidence. Reference operation durations are not invented agent deadlines.

Application write count and exact reference order are not default hard scoring
requirements. Cab booking outcomes compare route, service, requested booking time
and status; stochastic quotes, distance/duration estimates and delay history are
diagnostic. Explicit fare or delay constraints use the actual returned offers and
bookings in the NL assertion. Known `Home` and property aliases resolve using
the source scenario data. These choices differ from ARE's original graph judge
and are recorded under the τ adaptation's score version.

## Validation

The [migration validation record](gaia2-validation.md) includes the complete
offline checks, concurrency verification and the live API rate-limit outcome.

```bash
make test-gaia2
make test
make test-voice
make check-all
```

Live voice/provider tests retain their existing explicit gates. Dataset reference
verification and the default tests do not measure model accuracy. A live acceptance
sample must record its source IDs, seed, audio configuration and all attempted
outcomes; infrastructure or simulator errors must not be presented as model
task-completion failures.

The application methods are adapted from Meta ARE 1.2.0 under its MIT license;
the vendored directory includes the license and upstream file hashes. Task data
is attributed to the [Meta Gaia2 dataset](https://huggingface.co/datasets/meta-agents-research-environments/gaia2),
whose dataset card states CC BY 4.0 and describes the source data's provenance.
